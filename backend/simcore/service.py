"""游戏服务层：REST API 与 MCP 共用的会话、草案、回合、廷议、谏言逻辑。

所有对外返回都基于公开视图（public_view）；错误一律抛出 GameError(code, message, status)。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from simcore.agent.debate import debate_speeches, debate_summary, decide_stances, match_memorial, persona_card, topic_entities
from simcore.agent.loop import AgentAborted
from simcore.agent.offline import is_offline
from simcore.agent.roles import AUDIENCE_VOICE, chat_availability, interpret_edict, minister_chat_messages, narrator, offline_minister_text
from simcore.agent.tools import PLAYER_TIER, SCENARIO_TIER, AgentTools
from simcore.config import load_settings
from simcore.domain.apply import OpRejected, edict_memories
from simcore.domain.command import command_table, command_table_lines
from simcore.domain.difficulty import DifficultyError, current_difficulty, public_difficulties
from simcore.domain.digest import render_digest
from simcore.domain.events import matches
from simcore.domain.memorials import MemorialError, is_visible, memorial_flag, summarize_ops, visible_memorials
from simcore.domain.models import TurnReport, WorldState
from simcore.domain.reference import ReferenceError, build_reference
from simcore.domain.rules import audience_mode, held_offices, mover_faction, player_character_id, travel_note, traveller_label
from simcore.domain.turn import GameOver, choice_option_id, final_season_notice
from simcore.domain.visibility import (
    battle_text,
    fact_line,
    format_number,
    precision_text,
    public_crisis,
    public_report,
    public_view,
    redacted_report,
    resource_display,
)
from simcore.export import build_export, compact_clauses
from simcore.llm.base import LLMProvider
from simcore.llm.factory import create_provider
from simcore.scenario.loader import build_initial_state, load_scenario, validate_scenario
from simcore.store.session import SessionStore, state_hash

ENGINE_VERSION = "0.2.0"

ERROR_STATUS = {
    "SCENARIO_NOT_FOUND": 404,
    "SESSION_NOT_FOUND": 404,
    "EDICT_NOT_FOUND": 404,
    "ENTRY_NOT_FOUND": 404,
    "UNKNOWN_ENTITY": 404,
    "UNKNOWN_CHARACTER": 404,
    "UNKNOWN_MEMORIAL": 404,
    "UNKNOWN_DIFFICULTY": 422,
    "NOT_FOUND": 404,
    "INVALID_SCENARIO": 422,
    "INVALID_SCENARIO_PATH": 400,
    "INVALID_REQUEST": 422,
    "UNKNOWN_OP": 422,
    "OP_NOT_PERMITTED": 403,
    "GAME_OVER": 409,
    "EDICT_CONFIRMED": 409,
    "EDICT_STALE": 409,
    "ABORTED": 409,
    "CHARACTER_UNAVAILABLE": 409,
    "PLAYER_CHARACTER": 422,
    "DEBUG_DISABLED": 403,
    "PROVIDER_ERROR": 502,
    "TIMEOUT": 504,
    "INTERNAL": 500,
}

ROLEPLAY_RULES = """## 扮演与数字规则（必须遵守）
1. 引擎是唯一事实来源。你只能通过 draft_edict（ops / adopted）→ confirm_edict，或 choose，提议改变世界；规则层会逐条校验，被拒的条款不会生效。
2. 叙述、扮演与奏对中出现的数字，只能引用本简报（或 audience / end_turn 返回）里给出的值；不得编造、推算或补充新数字。
3. 标为「风闻」的数值只有区间，真值未知；只能说区间或「约」「恐」，不得说成确数。
4. 只叙述已执行的事实（end_turn 返回的事实与纪事）；未发生的事不得写成已发生。
5. 扮演人物时，以 audience 返回的人设卡与该人物可知的事实摘要为限；死者、系狱者不能应答。
6. 廷议的立场由引擎给定（court_debate），你只能据此写台词，不得改变立场；廷议结论只能作为谏言（save_advice），不能直接变成操作。"""


def _role_label(state: WorldState, character: Any) -> str:
    """人物的身份标签：有官职列官职名，否则用头衔（如 高力士 骠骑大将军、知内侍省事）。"""
    offices = [state.offices[office].name for office in held_offices(character) if office in state.offices]
    return "、".join(offices) if offices else str(character.title or "")


class GameError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status if status is not None else ERROR_STATUS.get(code, 422)

    def detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def engine_root() -> Path:
    return Path(os.environ.get("SIM_GAME_ROOT", Path(__file__).resolve().parents[2])).resolve()


def scenarios_root() -> Path:
    return Path(os.environ.get("SIM_SCENARIOS_DIR", engine_root() / "scenarios")).resolve()


def sim_home() -> Path:
    return Path(os.environ.get("LLM_SIM_HOME", Path.home() / ".llm-sim"))


def session_root() -> Path:
    return sim_home() / "sessions"


ProviderFactory = Callable[[dict[str, Any], str], LLMProvider]


def default_provider_factory(scenario: dict[str, Any], kind: str) -> LLMProvider:
    role = "character_chat" if kind == "minister_chat" else kind
    return create_provider(load_settings(), role, scenario)


class GameService:
    def __init__(self, provider_factory: ProviderFactory | None = None) -> None:
        self.provider_factory = provider_factory or default_provider_factory
        self.sessions: dict[str, SessionStore] = {}
        self.session_mtimes: dict[str, float] = {}
        self.scenarios: dict[str, dict[str, Any]] = {}
        self.abort_events: dict[str, asyncio.Event] = {}

    # ---- 场景与存档 -------------------------------------------------------------

    def provider(self, scenario: dict[str, Any], kind: str) -> LLMProvider:
        return self.provider_factory(scenario, kind)

    def scenario(self, scenario_id: str) -> dict[str, Any]:
        if scenario_id not in self.scenarios:
            root = scenarios_root()
            path = (root / scenario_id).resolve()
            if root not in path.parents:
                raise GameError("INVALID_SCENARIO_PATH", "无效场景路径")
            try:
                data = load_scenario(path)
            except FileNotFoundError as exc:
                raise GameError("SCENARIO_NOT_FOUND", f"场景不存在：{scenario_id}") from exc
            self.scenarios[scenario_id] = data
        return self.scenarios[scenario_id]

    def scenario_ids(self) -> list[str]:
        root = scenarios_root()
        if not root.is_dir():
            return []
        return [
            folder.name
            for folder in sorted(root.iterdir())
            if folder.is_dir() and not folder.name.startswith(".") and (folder / "scenario.yaml").is_file()
        ]

    def list_scenarios(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for scenario_id in self.scenario_ids():
            try:
                data = self.scenario(scenario_id)
                errors = validate_scenario(data)
                definition = data["scenario"]
                theme = Path(data["root"]) / "theme.css"
                result.append({
                    "id": definition["id"],
                    "folder": scenario_id,
                    "title": definition["title"],
                    "era": definition["era"],
                    "version": definition.get("version", "0.1.0"),
                    "categories": definition.get("edict_categories", []),
                    "calendar": definition.get("calendar", {}),
                    "difficulties": public_difficulties(definition),
                    "default_difficulty": definition.get("default_difficulty"),
                    "asset_ids": [entry.get("id", "") for entry in data.get("assets", {}).get("entries", [])],
                    "theme_css": theme.read_text(encoding="utf-8") if theme.is_file() else "",
                    "valid": not errors,
                })
            except (OSError, KeyError, ValueError, GameError):
                continue
        return result

    def create_session(self, scenario_id: str, seed: int = 0, difficulty: str | None = None) -> tuple[str, SessionStore]:
        scenario = self.scenario(scenario_id)
        errors = validate_scenario(scenario)
        if errors:
            raise GameError("INVALID_SCENARIO", "场景校验未通过：" + "；".join(errors[:5]))
        try:
            initial = build_initial_state(scenario, difficulty or None)
        except DifficultyError as exc:
            raise GameError(exc.code, exc.message) from exc
        session_id = uuid.uuid4().hex
        path = session_root() / f"{session_id}.jsonl"
        session = SessionStore(path, scenario, initial, seed)
        session.header["scenario_folder"] = scenario_id
        session._write()
        self._remember(session_id, session)
        return session_id, session

    def _remember(self, session_id: str, session: SessionStore) -> None:
        self.sessions[session_id] = session
        try:
            self.session_mtimes[session_id] = session.path.stat().st_mtime
        except OSError:
            pass

    def session(self, session_id: str) -> SessionStore:
        if not session_id or any(char in session_id for char in "/\\.") or len(session_id) > 64:
            raise GameError("SESSION_NOT_FOUND", f"存档不存在：{session_id}")
        path = session_root() / f"{session_id}.jsonl"
        cached = self.sessions.get(session_id)
        if cached is not None:
            try:
                mtime = path.stat().st_mtime
            except OSError as exc:
                self.sessions.pop(session_id, None)
                raise GameError("SESSION_NOT_FOUND", f"存档不存在：{session_id}") from exc
            if mtime != self.session_mtimes.get(session_id):
                cached.reload()
                self.session_mtimes[session_id] = mtime
            return cached
        if not path.is_file():
            raise GameError("SESSION_NOT_FOUND", f"存档不存在：{session_id}")
        try:
            header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        except (OSError, ValueError, IndexError) as exc:
            raise GameError("SESSION_NOT_FOUND", f"存档无法读取：{session_id}") from exc
        folder = header.get("scenario_folder") or header["scenario"]
        scenario = self.scenario(folder)
        session = SessionStore(path, scenario)
        self._remember(session_id, session)
        return session

    def touch(self, session_id: str) -> None:
        """本进程写入存档后更新 mtime 记录，避免下次访问误判为外部修改。"""
        session = self.sessions.get(session_id)
        if session is not None:
            self._remember(session_id, session)

    def list_sessions(self) -> list[dict[str, Any]]:
        result = []
        root = session_root()
        if not root.is_dir():
            return []
        for path in sorted(root.glob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                session = self.session(path.stem)
                result.append({
                    "id": path.stem,
                    "scenario": session.header["scenario"],
                    "turn": session.head_turn(),
                    "seed": session.seed,
                    "difficulty": session.header.get("difficulty"),
                    "created": session.header.get("timestamp"),
                })
            except (OSError, KeyError, ValueError, IndexError, GameError):
                continue
        return result

    def delete_session(self, session_id: str) -> dict[str, Any]:
        session = self.session(session_id)
        self.sessions.pop(session_id, None)
        self.session_mtimes.pop(session_id, None)
        session.delete_drafts()
        session.path.unlink(missing_ok=True)
        return {"deleted": True, "id": session_id}

    def fork_session(self, session_id: str, entry_id: str | None) -> tuple[str, SessionStore]:
        source = self.session(session_id)
        if entry_id is not None and not any(item["id"] == entry_id for item in source.entries):
            raise GameError("ENTRY_NOT_FOUND", f"历史节点不存在：{entry_id}")
        fork_id = uuid.uuid4().hex
        forked = source.fork(session_root() / f"{fork_id}.jsonl", entry_id)
        if source.header.get("scenario_folder"):
            forked.header["scenario_folder"] = source.header["scenario_folder"]
            forked._write()
        self._remember(fork_id, forked)
        return fork_id, forked

    # ---- 视图 -------------------------------------------------------------------

    def definition(self, session: SessionStore) -> dict[str, Any]:
        return session.scenario.get("scenario", {}) or {}

    def public_state(self, session: SessionStore, state: WorldState | None = None) -> dict[str, Any]:
        return public_view(state or session.state, session.seed)

    def objectives(self, session: SessionStore, state: WorldState) -> list[dict[str, Any]]:
        rows = []
        for objective in self.definition(session).get("objectives", []) or []:
            if not isinstance(objective, dict):
                continue
            if objective.get("fail_conditions") not in (None, {}, []) and matches(objective.get("fail_conditions"), state):
                status = "failed"
            elif objective.get("conditions") in (None, {}, []):
                status = "pending"
            elif matches(objective.get("conditions"), state):
                status = "achieved" if state.outcome is not None else "on_track"
            else:
                status = "at_risk"
            rows.append({
                "id": objective.get("id"),
                "title": objective.get("title", objective.get("id")),
                "status": status,
                "detail": objective.get("detail", ""),
            })
        return rows

    @staticmethod
    def difficulty_info(state: WorldState) -> dict[str, Any] | None:
        row = current_difficulty(state)
        return {"id": row.get("id"), "label": row.get("label"), "description": row.get("description", "")} if row else None

    @staticmethod
    def session_warnings(session: SessionStore) -> list[dict[str, Any]]:
        changed = session.scenario_changed
        return [{"code": "scenario_changed", **changed}] if changed else []

    def view(self, session_id: str) -> dict[str, Any]:
        session = self.session(session_id)
        state = session.state
        crisis = self.definition(session).get("crisis")
        report = session.last_report()
        over = state.outcome is not None
        return {
            "session_id": session_id,
            "scenario": state.scenario_id,
            "turn": state.turn,
            "difficulty": self.difficulty_info(state),
            "scenario_changed": session.scenario_changed,
            "warnings": self.session_warnings(session),
            "state": public_view(state, session.seed),
            "calendar_label": state.calendar.label,
            "objectives": self.objectives(session, state),
            "crisis": public_crisis(crisis, state, session.seed),
            # 终局后不再列奏疏与待决事件，改给终局复盘（review）
            "memorials": [] if over else visible_memorials(session.scenario, state),
            "pending_choices": [] if over else [row.model_dump(mode="json") for row in state.pending_choices],
            "outcome": state.outcome.model_dump(mode="json") if state.outcome else None,
            "review": self.final_review(session, state) if over else None,
            "last_report": public_report(report, state, session.seed, crisis),
            "can_advance": state.outcome is None,
            # 官职—所在—所统—指挥状态总表（玩家势力），见 domain.command
            "command_table": command_table(state, self.player_character(session, state)),
            # 下一次结算即终局时的提示（game_length 时限），否则 None
            "final_notice": final_season_notice(state, self.definition(session)),
        }

    def final_review(self, session: SessionStore, state: WorldState) -> dict[str, Any] | None:
        """终局复盘：结局与判定条件、目标状态、关键数值（隐藏数值只给风闻区间）、时间线要事。"""
        if state.outcome is None:
            return None
        stats = [{"id": key, "display": resource_display(state, key, session.seed)} for key in state.resources]
        own = mover_faction(state, None)
        troops = sum(row.strength for row in state.armies.values() if own is None or row.faction == own)
        held = sum(1 for row in state.regions.values() if own is None or row.controller in (None, own))
        stats.append({"id": "_armies", "display": f"本方兵力 {format_number(troops)}"})
        stats.append({"id": "_regions", "display": f"本方控制 {held}/{len(state.regions)} 地"})
        highlights: list[tuple[int, int, str]] = []
        for entry in session.history():
            if entry["type"] != "turn":
                continue
            payload = entry["payload"]
            report = payload.get("report") or {}
            snapshot = payload.get("snapshot") or {}
            when = str((snapshot.get("calendar") or {}).get("label") or f"第{report.get('turn')}回合")
            turn = int(report.get("turn", 0) or 0)
            for event in report.get("events", []) or []:
                if event.get("internal") or event.get("outcome"):
                    continue
                highlights.append((int(event.get("importance", 0) or 0), turn, f"T{turn} {when}：{event.get('title')}"))
            for battle in report.get("battles", []) or []:
                highlights.append((2, turn, f"T{turn} {when}：{battle.get('region_name', '')}之战，{battle_text(battle, state)}"))
        top = sorted(highlights, key=lambda row: (-row[0], row[1]))[:16]
        return {
            "outcome": state.outcome.model_dump(mode="json"),
            "satisfied": list(state.outcome.satisfied),
            "objectives": self.objectives(session, state),
            "stats": stats,
            "highlights": [text for _, _, text in sorted(top, key=lambda row: row[1])],
            "turns_played": state.turn - session.initial_state.turn,
        }

    def debug_state(self, session_id: str) -> dict[str, Any]:
        if os.environ.get("SIM_DEBUG") != "1":
            raise GameError("DEBUG_DISABLED", "完整状态只在设置 SIM_DEBUG=1 时可用")
        return self.session(session_id).state.model_dump(mode="json")

    def digest(self, session_id: str, character_id: str | None = None) -> str:
        session = self.session(session_id)
        return render_digest(session.state, session.seed, character_id, session.last_report(), session.scenario)

    # ---- 诏书 -------------------------------------------------------------------

    async def draft_edict(
        self,
        session_id: str,
        sections: dict[str, str] | None = None,
        adopted: list[dict[str, str]] | None = None,
        ops: list[dict[str, Any]] | None = None,
        use_provider: bool = True,
    ) -> dict[str, Any]:
        session = self.session(session_id)
        state = session.state
        if state.outcome is not None:
            raise GameError("GAME_OVER", f"本局已终局（{state.outcome.title}），不能再下诏")
        sections = sections or {}
        provider = self.provider(session.scenario, "edict_interpreter") if use_provider and any(text.strip() for text in sections.values()) else None
        abort_event = asyncio.Event()
        self.abort_events[session_id] = abort_event
        try:
            receipt = await interpret_edict(
                sections,
                session.scenario,
                state,
                provider,
                session.seed,
                abort_event,
                adopted=adopted,
                client_ops=ops,
            )
        except AgentAborted as exc:
            raise GameError("ABORTED", "诏令处理已中止") from exc
        except TimeoutError as exc:
            raise GameError("TIMEOUT", "诏令解释超时，请稍后重试") from exc
        finally:
            self.abort_events.pop(session_id, None)
        for clause in receipt["clauses"]:
            op = clause.get("op")
            clause["summary"] = summarize_ops([op], state)[0] if isinstance(op, dict) and op else ""
        # 回执的公开摘要写进旁路文件（不改日志、不改状态），供 export_log 复盘。
        session.record_draft({
            "action": "draft",
            "edict_id": receipt["id"],
            "turn": state.turn,
            "adopted": receipt.get("adopted", []),
            "clauses": compact_clauses(receipt["clauses"]),
        })
        receipt["base_turn"] = state.turn
        receipt["base_head"] = session.head_id
        receipt["base_hash"] = state_hash(state)
        # 草案写进存档旁的 <session>.drafts.json：进程重启（每次调用都重启的 MCP）与桌面应用都能确认。
        session.save_draft(receipt, current_turn=state.turn)
        return receipt

    def abort(self, session_id: str) -> dict[str, bool]:
        self.session(session_id)
        abort_event = self.abort_events.get(session_id)
        if abort_event is None:
            abort_event = asyncio.Event()
            self.abort_events[session_id] = abort_event
        abort_event.set()
        return {"aborted": True}

    def confirm_edict(self, session_id: str, edict_id: str) -> dict[str, Any]:
        """确认草案。草案从存档旁的 drafts 文件读取；回合已推进则 EDICT_STALE；
        自拟诏以来状态有变（抉择、另一道诏书、回退等）则逐条重新校验，变化的条款带 changed / previous_status。"""
        session = self.session(session_id)
        receipt = session.load_draft(edict_id)
        if receipt is None:
            raise GameError("EDICT_NOT_FOUND", "诏令草案不存在或已过期")
        if receipt.get("confirmed"):
            raise GameError("EDICT_CONFIRMED", "诏令已经确认")
        state = session.head_state()
        if state.outcome is not None:
            raise GameError("GAME_OVER", f"本局已终局（{state.outcome.title}），不能再下诏")
        if receipt.get("base_turn") != state.turn:
            raise GameError("EDICT_STALE", "回合已推进，此草案已失效，请重新拟诏")
        # 日志头未动则状态必然未变；头变了（召对消息等不改状态的条目也会移动它）再比较状态哈希。
        state_changed = receipt.get("base_head") != session.head_id and receipt.get("base_hash") != state_hash(state)
        receipt["state_changed"] = state_changed
        receipt["changed_clauses"] = self._revalidate_draft(session, state, receipt) if state_changed else []
        clauses: list[dict[str, Any]] = receipt["clauses"]
        runnable = [clause for clause in clauses if clause["status"] == "applied" and clause.get("op")]
        results = session.append_ops([clause["op"] for clause in runnable], [self._clause_label(clause) for clause in runnable])
        memories: list[dict[str, Any]] = []
        for clause, result in zip(runnable, results, strict=True):
            if not result["ok"]:
                clause["status"] = "rejected"
                clause["reason"] = result["message"]
                clause["code"] = result["code"]
            else:
                # 任免、处置人物的条款写入当事人的记忆（延期条款在下季执行时再写，见 resolve_turn）。
                memories.extend(edict_memories(clause["op"], state))
        if memories:
            session.append_ops(memories)
        delayed: dict[Any, tuple[str, list[dict[str, Any]]]] = {}
        for index, clause in enumerate(clauses):
            if clause["status"] == "delayed" and clause.get("op"):
                group = delayed.setdefault(clause.get("group", f"clause-{index}"), (self._clause_label(clause), []))
                group[1].append(clause["op"])
        for label, ops in delayed.values():
            # 同组（同一奏疏选项 / 同一段诏令）的延期条款排成一个延期包，下季一并执行。
            session.append_pending(ops, state.turn + 1, label=label)
        flags = [
            {"op": "set_flag", "key": memorial_flag(str(row["memorial_id"])), "value": row["option_id"]}
            for row in receipt.get("adopted", [])
        ]
        if flags:
            session.append_ops(flags)
        receipt["confirmed"] = True
        session.save_draft(receipt)
        receipt["execution_rate"] = (
            sum(clause["status"] == "applied" for clause in clauses) / len(clauses) if clauses else 1
        )
        session.append_edict({
            "action": "confirm",
            "edict_id": edict_id,
            "turn": state.turn,
            "adopted": receipt.get("adopted", []),
            "clauses": compact_clauses(clauses),
            "changed_clauses": receipt.get("changed_clauses", []),
            "execution_rate": receipt["execution_rate"],
        })
        self.touch(session_id)
        receipt["state"] = public_view(session.head_state(), session.seed)
        return receipt

    @staticmethod
    def _clause_label(clause: dict[str, Any]) -> str:
        """条款来源说明，作为事实原因（op 没写 reason 时）：奏疏标题，或「诏令」。"""
        if clause.get("label"):
            return str(clause["label"])
        if clause.get("source") == "memorial":
            return str(clause.get("text", "")).split("：")[0] or "奏疏"
        return "诏令"

    def _revalidate_draft(self, session: SessionStore, state: WorldState, receipt: dict[str, Any]) -> list[int]:
        """按当前状态重演草案：已处理/过期的奏疏整组作废，applied / delayed 条款逐条重新校验（延期骰结果保留）。

        返回状态有变的条款序号（从 1 起）。原先被拒的条款保持被拒，不会因状态变化而悄悄生效。
        """
        memorials = {str(row.get("id")): row for row in session.scenario.get("memorials") or [] if isinstance(row, dict)}
        unavailable = {
            str(row["memorial_id"])
            for row in receipt.get("adopted", [])
            if str(row["memorial_id"]) not in memorials or not is_visible(memorials[str(row["memorial_id"])], state)
        }
        receipt["adopted"] = [row for row in receipt.get("adopted", []) if str(row["memorial_id"]) not in unavailable]
        tools = AgentTools(state)
        changed: list[int] = []
        for index, clause in enumerate(receipt["clauses"], start=1):
            previous = clause["status"]
            if clause.get("memorial_id") in unavailable and previous != "rejected":
                clause.update({
                    "status": "rejected",
                    "reason": "此奏疏已被处理或已过期（拟诏之后状态有变）",
                    "code": "MEMORIAL_UNAVAILABLE",
                })
            elif previous in {"applied", "delayed"} and clause.get("op"):
                tier = PLAYER_TIER if clause.get("source") in {"client", "text"} else SCENARIO_TIER
                result = tools.propose_ops([clause["op"]], tier=tier)[0]
                if result["status"] == "rejected":
                    clause.update({"status": "rejected", "reason": result.get("reason", ""), "code": result.get("code")})
            if clause["status"] != previous:
                clause["changed"] = True
                clause["previous_status"] = previous
                changed.append(index)
        return changed

    # ---- 抉择与回合 ---------------------------------------------------------------

    def choose(self, session_id: str, choice_id: str, option_id: str) -> dict[str, Any]:
        session = self.session(session_id)
        try:
            state, facts, _ = session.append_op({"op": "choose_event_option", "choice_id": choice_id, "option_id": option_id})
        except OpRejected as exc:
            raise GameError(exc.code, exc.message) from exc
        self.touch(session_id)
        report = TurnReport(turn=state.turn, facts=facts)
        public = public_report(report, state, session.seed) or {}
        return {
            "choice_id": choice_id,
            "option_id": option_id,
            "facts": public.get("facts", []),
            "pending_choices": [row.model_dump(mode="json") for row in state.pending_choices],
            "state": public_view(state, session.seed),
        }

    async def end_turn(self, session_id: str, narrate: bool = True) -> dict[str, Any]:
        session = self.session(session_id)
        if session.state.outcome is not None:
            raise GameError("GAME_OVER", f"本局已终局（{session.state.outcome.title}），不能再推进回合")
        context = self.season_context(session)
        try:
            state, report, _ = session.append_turn()
        except GameOver as exc:
            raise GameError("GAME_OVER", str(exc)) from exc
        crisis = self.definition(session).get("crisis")
        public = public_report(report, state, session.seed, crisis) or {}
        facts = [str(fact.get("text", "")) for fact in public.get("facts", [])]
        if narrate:
            try:
                memorial = await narrator(
                    redacted_report(report, state),
                    self.provider(session.scenario, "narrator"),
                    session.scenario,
                    state=state,
                    seed=session.seed,
                    context=context,
                )
            except Exception:  # noqa: BLE001 - 叙事失败不影响已结算的回合
                memorial = "；".join(facts) or "本回合暂无可记录的确定变化。"
        else:
            memorial = "；".join(facts) or "本回合暂无可记录的确定变化。"
        session.append_message("narrator", memorial, {"turn": state.turn})
        self.touch(session_id)
        return {
            "state": public_view(state, session.seed),
            "report": public,
            "memorial": memorial,
            "outcome": state.outcome.model_dump(mode="json") if state.outcome else None,
            "calendar_label": state.calendar.label,
            "final_notice": final_season_notice(state, self.definition(session)),
        }

    def season_context(self, session: SessionStore) -> dict[str, Any]:
        """本季（上次结算以来）已施行的诏令来源与抉择，供纪事归并「诏令施行」一类。"""
        events = {str(row.get("id")): row for row in session.scenario.get("events", []) or [] if isinstance(row, dict)}
        labels: list[str] = []
        plain = False
        choices: list[dict[str, str]] = []
        for payload in session.ops_since_turn():
            op = payload.get("op") or {}
            if op.get("op") == "choose_event_option":
                choice_id = str(op.get("choice_id", ""))
                event_id = choice_id.removeprefix("choice-").rsplit("-", 1)[0]
                event = events.get(event_id, {})
                options = event.get("choices") or []
                option = next(
                    (row for index, row in enumerate(options) if isinstance(row, dict) and choice_option_id(row, index) == op.get("option_id")),
                    {},
                )
                if event:
                    choices.append({
                        "title": str(event.get("title", "")),
                        "option": str(option.get("text") or option.get("label") or ""),
                    })
                continue
            source = str(payload.get("source") or "")
            if not source or op.get("op") in {"set_flag", "remember", "adjust_counter", "set_counter"}:
                continue
            if source.startswith("诏令"):
                plain = True
            elif source not in labels:
                labels.append(source)
        return {"edicts": labels, "plain_edicts": plain, "choices": [row for row in choices if row["title"] and row["option"]]}

    # ---- 人物：召对、廷议、谏言 -------------------------------------------------------

    def _character(self, state: WorldState, character_id: str) -> Any:
        character = state.characters.get(character_id)
        if character is None:
            raise GameError("UNKNOWN_CHARACTER", f"人物不存在：{character_id}")
        return character

    def player_character(self, session: SessionStore, state: WorldState | None = None) -> str | None:
        """玩家扮演的人物（场景 settings.player_character，或 settings.player_office 的在任者）。"""
        return player_character_id(state or session.head_state(), self.definition(session).get("settings") or {})

    def _reject_player(self, session: SessionStore, state: WorldState, character_id: str) -> None:
        if character_id and character_id == self.player_character(session, state):
            name = state.characters[character_id].name if character_id in state.characters else character_id
            raise GameError("PLAYER_CHARACTER", f"{name}是玩家本人，不能召对或与议")

    def audience_card(self, session_id: str, character_id: str) -> dict[str, Any]:
        session = self.session(session_id)
        state = session.head_state()
        character = self._character(state, character_id)
        self._reject_player(session, state, character_id)
        afterlife = bool(self.definition(session).get("settings", {}).get("afterlife_chat", False))
        refusal = chat_availability(character_id, state, afterlife)
        if refusal is not None:
            raise GameError("CHARACTER_UNAVAILABLE", f"{character.name}{'已故' if character.status == 'dead' else '系狱'}，无法召对")
        mode = self.audience_mode(session, state, character_id)
        return {
            "persona": persona_card(state, character_id, session.scenario),
            "digest": render_digest(state, session.seed, character_id, session.last_report(), session.scenario),
            "memories": list(character.memories[-12:]),
            # 召对方式：面见（人在御前）/ 书面奏对（远在他处）/ 遣使问对（在途）
            "mode": mode,
            "voice": (
                f"以{character.name}的身份与口吻作答：{character.speech_style or '言辞得体'}。"
                f"召对方式：{mode['label']}——{mode['note']}{AUDIENCE_VOICE[mode['mode']]}"
                "只说此人可能知道的事；数字只能引用上面事实摘要中的值，风闻区间不得说成确数；"
                "不得声称已经执行任何政令。"
            ),
        }

    def audience_mode(self, session: SessionStore, state: WorldState, character_id: str) -> dict[str, str]:
        if character_id not in state.characters:
            return {"mode": "in_person", "label": "面见", "note": ""}
        return audience_mode(state, character_id, self.definition(session).get("settings") or {})

    async def chat(self, session_id: str, character_id: str, text: str) -> dict[str, Any]:
        """非流式召对（MCP server 模式用）。"""
        session = self.session(session_id)
        self.audience_card(session_id, character_id)
        provider = self.provider(session.scenario, "minister_chat")
        content = self.offline_chat_text(session, character_id, text, provider)
        if content is None:
            messages = minister_chat_messages(character_id, session.state, text, session.scenario, session.seed, session.last_report())
            content = (await provider.chat(messages)).content
        mode = self.audience_mode(session, session.head_state(), character_id)
        session.append_message("player", text, {"character": character_id, "mode": mode["mode"]})
        session.append_message("character", content, {"character": character_id, "status": "ok", "mode": mode["mode"]})
        self.touch(session_id)
        return {"status": "ok", "character": character_id, "content": content, "mode": mode}

    @staticmethod
    def offline_chat_text(session: SessionStore, character_id: str, text: str, provider: Any) -> str | None:
        """离线 provider（mock）时的模板召对答话；在线 provider 返回 None。"""
        if not is_offline(provider):
            return None
        return offline_minister_text(character_id, session.state, text, session.scenario, session.seed, session.last_report())

    def chat_prepare(self, session_id: str, character_id: str) -> tuple[SessionStore, dict[str, str] | None]:
        session = self.session(session_id)
        state = session.head_state()
        if character_id in state.characters:
            self._reject_player(session, state, character_id)
        afterlife = bool(self.definition(session).get("settings", {}).get("afterlife_chat", False))
        return session, chat_availability(character_id, state, afterlife)

    def _debate_participants(self, session: SessionStore, state: WorldState, participants: list[str]) -> list[str]:
        unique = list(dict.fromkeys(participants))
        if not 1 <= len(unique) <= 6:
            raise GameError("INVALID_REQUEST", "廷议参与者须为 1 到 6 人")
        for character_id in unique:
            character = self._character(state, character_id)
            self._reject_player(session, state, character_id)
            if character.status != "alive":
                raise GameError("CHARACTER_UNAVAILABLE", f"{character.name}不在朝，不能与议")
        return unique

    def _debate_setup(
        self, session: SessionStore, state: WorldState, topic: str, participants: list[str], memorial_id: str | None
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        """廷议共用部分：议题（可由 memorial_id 补出）、立场与议题分析（topic_entities / 对应奏疏）。"""
        try:
            memorial = match_memorial(session.scenario, state, topic, memorial_id or None)
        except MemorialError as exc:
            raise GameError(exc.code, exc.message) from exc
        if not topic.strip() and memorial is not None:
            topic = str(memorial.get("title", memorial_id))
        if not topic.strip():
            raise GameError("INVALID_REQUEST", "廷议议题不能为空")
        unique = self._debate_participants(session, state, participants)
        stances = decide_stances(
            state, session.scenario, topic, unique, memorial_id=str(memorial["id"]) if memorial else None, seed=session.seed
        )
        options = [row for row in (memorial or {}).get("options", []) or [] if isinstance(row, dict)]
        if memorial is not None and options:
            listing = "；".join(f"`{row.get('id')}`「{row.get('label', row.get('id'))}」" for row in options)
            proposition = f"就奏疏「{memorial.get('title', memorial.get('id'))}」诸选项择一（{listing}）；各人立场附所主张的选项"
        else:
            proposition = f"「{topic}」——赞成即主张照此施行，反对即主张不行，持中即未置可否"
        analysis = {
            "topic_entities": topic_entities(state, topic),
            "memorial": {
                "id": memorial.get("id"),
                "title": memorial.get("title"),
                "options": [{"id": row.get("id"), "label": row.get("label", row.get("id"))} for row in options],
            } if memorial else None,
            "proposition": proposition,
        }
        return topic, stances, analysis

    def debate_brief(self, session_id: str, topic: str, participants: list[str], memorial_id: str | None = None) -> dict[str, Any]:
        """client 模式：只给立场与人设，由调用方模型写台词。"""
        session = self.session(session_id)
        state = session.state
        topic, stances, analysis = self._debate_setup(session, state, topic, participants, memorial_id)
        for row in stances:
            row["persona"] = persona_card(state, row["character"], session.scenario)
        return {
            "topic": topic,
            **analysis,
            "stances": stances,
            "digest": render_digest(state, session.seed, None, session.last_report(), session.scenario),
            "summary": debate_summary(stances),
            "instructions": "按 stances 的顺序为每人写一段不超过三句的发言；立场不可更改；数字只能引用 digest 中的值。",
        }

    async def debate(self, session_id: str, topic: str, participants: list[str], memorial_id: str | None = None) -> dict[str, Any]:
        session = self.session(session_id)
        state = session.state
        topic, stances, analysis = self._debate_setup(session, state, topic, participants, memorial_id)
        try:
            speeches = await debate_speeches(
                state, session.scenario, topic, stances, self.provider(session.scenario, "court_debate"), session.seed, session.last_report()
            )
        except TimeoutError as exc:
            raise GameError("TIMEOUT", "廷议生成超时") from exc
        summary = debate_summary(stances)
        session.append_message("debate", json.dumps({"topic": topic, "speeches": speeches}, ensure_ascii=False), {"topic": topic})
        self.touch(session_id)
        return {"topic": topic, **analysis, "speeches": speeches, "summary": summary}

    def add_advice(self, session_id: str, character: str, text: str, source: str = "player") -> dict[str, Any]:
        session = self.session(session_id)
        if character and character not in session.state.characters and character != "player":
            raise GameError("UNKNOWN_CHARACTER", f"人物不存在：{character}")
        if not text.strip():
            raise GameError("INVALID_REQUEST", "谏言内容不能为空")
        row = session.append_advice(character, text, source)
        self.touch(session_id)
        return row

    def list_advice(self, session_id: str) -> list[dict[str, Any]]:
        return self.session(session_id).advice()

    # ---- 时间线 -------------------------------------------------------------------

    def history(self, session_id: str) -> list[dict[str, Any]]:
        return self.session(session_id).timeline()

    def export_log(self, session_id: str, from_turn: int | None = None, to_turn: int | None = None) -> dict[str, Any]:
        """结构化复盘日志（只读，公开视图）：见 simcore.export.build_export。"""
        session = self.session(session_id)
        if from_turn is not None and to_turn is not None and from_turn > to_turn:
            raise GameError("INVALID_REQUEST", "from_turn 不能大于 to_turn")
        return build_export(session, session_id, self.definition(session).get("crisis"), from_turn, to_turn)

    def rewind(self, session_id: str, entry_id: str | None) -> dict[str, Any]:
        session = self.session(session_id)
        if entry_id is not None and not any(item["id"] == entry_id and item["type"] != "branch" for item in session.entries):
            raise GameError("ENTRY_NOT_FOUND", f"历史节点不存在：{entry_id}")
        session.rewind(entry_id)
        self.touch(session_id)
        return self.view(session_id)

    # ---- MCP 辅助：简报与操作参考 ---------------------------------------------------------

    def briefing(self, session_id: str) -> str:
        session = self.session(session_id)
        state = session.state
        view = self.view(session_id)
        definition = self.definition(session)
        lines = [f"# 《{definition.get('title', state.scenario_id)}》简报 · {state.calendar.label}（第{state.turn}回合）", f"存档 session_id：{session_id}"]
        if view["difficulty"]:
            difficulty = view["difficulty"]
            lines.append(f"难度：{difficulty['label']}（{difficulty['id']}）" + (f"——{difficulty['description']}" if difficulty.get("description") else ""))
        for warning in view["warnings"]:
            lines.append(
                f"**注意 [{warning['code']}]**：{warning['message']}"
                f"（存档时剧本 {warning.get('saved_version') or '?'}，当前 {warning.get('current_version') or '?'}）"
            )
        if state.outcome is not None:
            lines.append(f"**本局已终局：{state.outcome.title}**（{'失败' if state.outcome.kind == 'fail' else '结局'}）{state.outcome.text}")
        if view["final_notice"]:
            lines.append(f"**{view['final_notice']['text']}**")
        if view["objectives"]:
            lines.append("\n## 目标")
            status_names = {"on_track": "守住", "at_risk": "告急", "failed": "已失", "achieved": "达成", "pending": "待定"}
            lines.extend(f"- {row['title']}：{status_names.get(row['status'], row['status'])}。{row['detail']}" for row in view["objectives"])
        lines.append("\n## 国势（公开数值）")
        for key, row in state.resources.items():
            precision = f"——{precision_text(state, key)}" if row.hidden else ""
            lines.append(f"- {resource_display(state, key, session.seed)}{precision}")
        crisis = view["crisis"]
        if crisis:
            lines.append(f"\n## 危机计：{crisis['title']} {crisis['display']}（{crisis['band']}）")
            for term in crisis["terms"]:
                lines.append(f"- 分项 {term['label']}：{term['display']}")
            lines.append("（分项按当前状态计算，即下次结算的预计漂移；上季实际变化见「上季危机变化」。）")
        change = (view["last_report"] or {}).get("crisis_change")
        if change:
            lines.append("\n## 上季危机变化（结算当时的实际贡献）")
            lines.extend(f"- {line}" for line in change["lines"])
        table = view["command_table"]
        table_lines = command_table_lines(table)
        if table_lines and state.outcome is None:
            lines.append("\n## 官职与兵权（官职—所在—所统—指挥状态；遥领、出缺、无统帅加粗）")
            lines.extend(table_lines)
            if table["alerts"]:
                lines.append("提醒：" + "；".join(table["alerts"]))
        if view.get("review"):
            lines.extend(self._review_lines(view["review"]))
        travellers = self._traveller_lines(state)
        if travellers:
            lines.append("\n## 在途")
            lines.extend(f"- {line}" for line in travellers)
        if state.pending_clauses and state.outcome is None:
            lines.append("\n## 待执行的延期条款")
            for clause in state.pending_clauses:
                summary = "；".join(summarize_ops(clause.ops, state))
                lines.append(f"- {clause.label or '延期诏令'}：{summary}（第{clause.due_turn}回合结算时执行）")
        if view["memorials"]:
            lines.append("\n## 本季奏疏（可在 draft_edict 的 adopted 中采纳：{memorial_id, option_id}）")
            for item in view["memorials"]:
                where = f"（{item['region_name']}）" if item.get("region_name") else ""
                lines.append(f"### [{item['id']}] {item['title']}{where}")
                if item.get("summary"):
                    lines.append(item["summary"])
                for advisor in item["advisors"]:
                    stance = {"support": "赞成", "oppose": "反对", "neutral": "持中"}.get(advisor["stance"], advisor["stance"])
                    lines.append(f"- 谏言 {advisor['name']}（{stance}）：{advisor['line']}")
                for option in item["options"]:
                    ops = "；".join(option["ops_summary"]) or "不改变状态"
                    hint = f" 〔{option['hint']}〕" if option.get("hint") else ""
                    lines.append(f"- 选项 `{option['id']}`「{option['label']}」{hint} → {ops}")
        if view["pending_choices"]:
            lines.append("\n## 待决事件（用 choose 处置：choice_id + option_id）")
            for choice in state.pending_choices:
                options = "；".join(f"`{option.id}`「{option.text}」" for option in choice.options)
                lines.append(
                    f"- [{choice.id}] {choice.title}：{choice.text} 选项：{options}。"
                    f"默认 `{choice.default_option}`，若第{choice.deadline_turn}回合结算前未决则依默认执行。"
                )
        report = view["last_report"]
        if report:
            facts = [fact_line(fact) for fact in report.get("facts", [])]
            lines.append("\n## 上季事实")
            lines.extend(f"- {fact}" for fact in facts[:40]) if facts else lines.append("- （无）")
            if report.get("battles"):
                lines.append("战报：" + "；".join(f"{row.get('region_name', '')}：{battle_text(row, state)}" for row in report["battles"]))
            featured = [row for row in report.get("events", []) if not row.get("outcome")]
            if featured:
                lines.append("上季时事：" + "、".join(f"「{row['title']}」" for row in featured))
        player = self.player_character(session, state)
        people = [
            f"{row.name}（`{row.id}`{'，' + role if (role := _role_label(state, row)) else ''}"
            f"{'，' + note if (note := travel_note(state, row)) else ''}）"
            for row in state.characters.values()
            if row.status == "alive" and row.id != player
        ]
        lines.append("\n## 可召对人物\n" + "；".join(people))
        lines.append("\n" + ROLEPLAY_RULES)
        return "\n".join(lines)

    @staticmethod
    def _review_lines(review: dict[str, Any]) -> list[str]:
        outcome = review["outcome"]
        lines = [
            "\n## 终局复盘",
            f"结局：{outcome['title']}（{'失败' if outcome['kind'] == 'fail' else '结局'}，T{outcome['turn']}）；共历 {review['turns_played']} 回合。",
        ]
        if review["satisfied"]:
            lines.append("判定条件：" + "；".join(f"「{line}」" for line in review["satisfied"]))
        status_names = {"on_track": "守住", "at_risk": "未达", "failed": "已失", "achieved": "达成", "pending": "待定"}
        if review["objectives"]:
            lines.append("目标：" + "；".join(f"{row['title']} {status_names.get(row['status'], row['status'])}" for row in review["objectives"]))
        lines.append("关键数值：" + "；".join(row["display"] for row in review["stats"]))
        if review["highlights"]:
            lines.append("要事：")
            lines.extend(f"- {line}" for line in review["highlights"])
        lines.append("（本局已终局：不再有奏疏与待决事件；可用 history / rewind 回到更早节点另开分支。）")
        return lines

    @staticmethod
    def _traveller_lines(state: WorldState) -> list[str]:
        """在途的人物与本方军队：「某某（自A赴B，约n季后到）」。"""
        own = mover_faction(state, None)
        rows = [traveller_label(state, row) for row in state.characters.values() if row.in_transit and row.status == "alive"]
        rows.extend(
            traveller_label(state, row)
            for row in state.armies.values()
            if row.in_transit and (own is None or row.faction == own) and row.strength > 0
        )
        return rows

    def op_reference(
        self, session_id: str, op: str | None = None, entity_kind: str | None = None, ids: list[str] | None = None
    ) -> dict[str, Any]:
        """玩家可用操作与实体表（见 domain.reference.build_reference）：缺省紧凑；op / entity_kind / ids 过滤。"""
        session = self.session(session_id)
        state = session.head_state()
        try:
            return build_reference(state, op or None, entity_kind or None, ids or None, self.player_character(session, state))
        except ReferenceError as exc:
            raise GameError(exc.code, exc.message) from exc
