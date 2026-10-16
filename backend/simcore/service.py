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
from simcore.agent.tools import PLAYER_TIER, SCENARIO_TIER, AgentTools, cascade_delays
from simcore.config import load_settings
from simcore.domain.apply import OpRejected, apply, edict_memories
from simcore.domain.cards import card_flag, cards_view
from simcore.domain.command import command_table, command_table_lines
from simcore.domain.court import (
    agenda_counts,
    agenda_key,
    agenda_refusal,
    agenda_view,
    not_present_message,
    presence,
    present_list,
    remote_reply_mode,
)
from simcore.domain.describe import describe_condition
from simcore.domain.difficulty import DifficultyError, current_difficulty, public_difficulties
from simcore.domain.digest import render_digest
from simcore.domain.events import matches
from simcore.domain.memorials import MemorialError, is_visible, memorial_flag, summarize_ops, visible_memorials
from simcore.domain.military import MILITARY_ORDERS, armies_overview, roll_autonomy
from simcore.domain.models import TurnReport, WorldState, clone
from simcore.domain.reference import ReferenceError, build_reference
from simcore.domain.rules import (
    audience_mode,
    delay_phrase,
    entity_name,
    find_route,
    held_offices,
    mover_faction,
    office_army,
    player_character_id,
    stat_legend,
    travel_note,
    turn_label,
    turn_unit_label,
)
from simcore.domain.turn import GameOver, choice_option_id, final_season_notice, upkeep_label, upkeep_rows
from simcore.domain.visibility import (
    battle_breakdown,
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
from simcore.scenario.loader import build_initial_state, load_scenario, tutorial_steps, validate_scenario
from simcore.store.session import SessionStore, state_hash
from simcore.version import ENGINE_VERSION as ENGINE_VERSION  # 兼容旧的导入位置

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
    "DRAFT_UNCONFIRMED": 409,
    "UNKNOWN_OPTION": 404,
    "ABORTED": 409,
    "CHARACTER_UNAVAILABLE": 409,
    "PLAYER_CHARACTER": 422,
    "DEBUG_DISABLED": 403,
    "AGENDA_EXHAUSTED": 409,
    "NOT_PRESENT": 409,
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

ENVOY_SENT = "使者已发，回奏于下季送达"
WRITTEN_SOURCE = "书面议状"


def _role_label(state: WorldState, character: Any) -> str:
    """人物的身份标签：有官职列官职名，否则用头衔（如 高力士 骠骑大将军、知内侍省事）。"""
    offices = [state.offices[office].name for office in held_offices(character) if office in state.offices]
    if offices:
        return "、".join(offices)
    title = str(character.title or "")
    # 头衔即某一官职之名而此人已不在其位（罢官、致仕）：注明旧衔，免得读成现任
    if title and any(row.name == title for row in state.offices.values()):
        return f"旧衔{title}，今无官职"
    return title


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
    from simcore.llm.factory import create_provider  # 延迟导入：provider / httpx 只在真要调模型时加载

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
            "pending_choices": [] if over else [choice_view(row, state) for row in state.pending_choices],
            "outcome": state.outcome.model_dump(mode="json") if state.outcome else None,
            "review": self.final_review(session, state) if over else None,
            "last_report": public_report(report, state, session.seed, crisis),
            "can_advance": state.outcome is None,
            # 官职—所在—所统—指挥状态总表（玩家势力），见 domain.command
            "command_table": command_table(state, self.player_character(session, state)),
            # 下一次结算即终局时的提示（game_length 时限），否则 None
            "final_notice": final_season_notice(state, self.definition(session)),
            # 第 9 轮：朝会日程、回奏、书面议状、诏令解释模式（offline|llm）、新手引导
            "agenda": agenda_view(state, self.settings(session)),
            "replies": self.replies(session, state),
            "written_opinions": [row for row in session.advice() if row.get("source") == WRITTEN_SOURCE and row.get("turn") == state.turn],
            "provider_mode": self.provider_mode(session),
            "tutorial": self.tutorial(session, state),
        }

    # ---- 第 9 轮：朝会日程、回奏、在场、政令卡、军令、新手引导 ---------------------------------

    def settings(self, session: SessionStore) -> dict[str, Any]:
        settings = self.definition(session).get("settings")
        return settings if isinstance(settings, dict) else {}

    def provider_mode(self, session: SessionStore) -> str:
        """诏令文本由谁解释：offline（离线 mock：御笔只记入史册）或 llm。"""
        try:
            return "offline" if is_offline(self.provider(session.scenario, "edict_interpreter")) else "llm"
        except Exception:  # noqa: BLE001 - 设置有误时按离线提示
            return "offline"

    def tutorial(self, session: SessionStore, state: WorldState) -> dict[str, Any]:
        """新手引导：当季步骤（剧本 tutorial，按 step 排序）；会话标记 tutorial=false 时关闭。"""
        enabled = (session.header.get("session_flags") or {}).get("tutorial", True) is not False
        steps = tutorial_steps(session.scenario)
        rows = [
            {
                "turn": row.get("turn"),
                "step": row.get("step", index + 1),
                "title": row.get("title", ""),
                "text": row.get("text", ""),
                "target": row.get("target"),
            }
            for index, row in enumerate(steps)
            if isinstance(row, dict) and row.get("turn") == state.turn
        ]
        rows.sort(key=lambda row: int(row["step"]) if isinstance(row["step"], (int, float)) else 0)
        return {"enabled": enabled, "steps": rows if enabled and state.outcome is None else [], "total": len(steps)}

    def set_session_flags(self, session_id: str, tutorial: bool | None = None) -> dict[str, Any]:
        """会话标记（存档头 session_flags，不进状态哈希）：目前只有 tutorial（新手引导开关）。"""
        session = self.session(session_id)
        flags = dict(session.header.get("session_flags") or {})
        if tutorial is not None:
            flags["tutorial"] = bool(tutorial)
        session.header["session_flags"] = flags
        session._write()
        self.touch(session_id)
        return {"session_flags": flags, "tutorial": self.tutorial(session, session.head_state())}

    def present(self, session_id: str) -> dict[str, Any]:
        session = self.session(session_id)
        return present_list(session.head_state(), self.settings(session))

    def policy_cards(self, session_id: str) -> list[dict[str, Any]]:
        session = self.session(session_id)
        return cards_view(session.head_state(), session.scenario)

    def armies(self, session_id: str) -> dict[str, Any]:
        session = self.session(session_id)
        state = session.head_state()
        return {
            "armies": armies_overview(state, cards_view(state, session.scenario)),
            "regions": [{"id": row.id, "name": row.name, "controller": row.controller} for row in state.regions.values()],
            "turn_unit": turn_unit_label(state),
        }

    def _count_agenda(self, session: SessionStore, kind: str) -> None:
        """计一次召对 / 廷议（只有配置了上限才计；终局后不计）。已满时 AGENDA_EXHAUSTED。"""
        state = session.head_state()
        settings = self.settings(session)
        refusal = agenda_refusal(state, kind, settings)
        if refusal:
            raise GameError("AGENDA_EXHAUSTED", refusal)
        if agenda_counts(state, kind, settings) and state.outcome is None:
            session.append_op({"op": "adjust_counter", "key": agenda_key(kind), "delta": 1}, source="朝会日程")

    def begin_audience(self, session_id: str, character_id: str, question: str = "", via: str = "rest") -> dict[str, Any]:
        """召对开始：校验（玩家本人、在世）、朝会日程（已满 AGENDA_EXHAUSTED，否则计一次）；
        远地召对在 remote_reply=next_season 时记下问话，回奏于下季送达（deferred=True）。"""
        session = self.session(session_id)
        state = session.head_state()
        character = self._character(state, character_id)
        self._reject_player(session, state, character_id)
        afterlife = bool(self.settings(session).get("afterlife_chat", False))
        if chat_availability(character_id, state, afterlife) is not None:
            raise GameError("CHARACTER_UNAVAILABLE", f"{character.name}{'已故' if character.status == 'dead' else '系狱'}，无法召对")
        mode = self.audience_mode(session, state, character_id)
        deferred = mode["mode"] != "in_person" and remote_reply_mode(state, self.settings(session)) == "next_season"
        self._count_agenda(session, "audience")
        if deferred:
            session.append_message(
                "envoy",
                question,
                {"character": character_id, "mode": mode["mode"], "mode_label": mode["label"], "turn": state.turn, "via": via},
            )
        self.touch(session_id)
        return {"mode": mode, "deferred": deferred, "agenda": agenda_view(session.head_state(), self.settings(session))}

    def replies(self, session: SessionStore, state: WorldState) -> list[dict[str, Any]]:
        """本季送达的回奏：服务端 / REST 的遣使问对在结算时生成（text）；client 模式的由调用方扮演（text 为 null，roleplay=True）。"""
        rows: list[dict[str, Any]] = []
        for message in session.messages():
            meta = message.get("metadata") or {}
            character_id = str(meta.get("character", ""))
            name = state.characters[character_id].name if character_id in state.characters else character_id
            base = {"character": character_id, "name": name, "mode": meta.get("mode"), "mode_label": meta.get("mode_label"), "turn": state.turn}
            if message.get("role") == "reply" and meta.get("turn") == state.turn:
                rows.append({
                    **base,
                    "question": meta.get("question", ""),
                    "asked_turn": meta.get("asked_turn"),
                    "text": message.get("content", ""),
                    "roleplay": False,
                    "via": meta.get("via"),
                })
            elif message.get("role") == "envoy" and meta.get("via") == "client" and meta.get("turn") == state.turn - 1:
                rows.append({**base, "question": message.get("content", ""), "asked_turn": meta.get("turn"), "text": None, "roleplay": True, "via": "client"})
        return rows

    async def _deliver_dispatches(self, session: SessionStore, asked_turn: int, state: WorldState) -> dict[str, list[dict[str, Any]]]:
        """结算后：上季遣使问对的回奏（非 client 的由 provider / 离线模板生成）与书面议状（记入谏言库，来源「书面议状」）。"""
        replies: list[dict[str, Any]] = []
        written: list[dict[str, Any]] = []
        report = session.last_report()
        for message in session.messages():
            meta = message.get("metadata") or {}
            if meta.get("turn") != asked_turn or message.get("role") not in {"envoy", "written_opinion"}:
                continue
            character_id = str(meta.get("character", ""))
            if character_id not in state.characters:
                continue
            character = state.characters[character_id]
            if message.get("role") == "written_opinion":
                row = session.append_advice(character_id, str(message.get("content", "")), WRITTEN_SOURCE)
                written.append({**row, "name": character.name})
                continue
            if meta.get("via") == "client":
                continue
            question = str(message.get("content", ""))
            if chat_availability(character_id, state, False) is not None:
                text = f"{character.name}已不能回奏（{'已故' if character.status == 'dead' else '系狱'}）。"
            else:
                try:
                    provider = self.provider(session.scenario, "minister_chat")
                    if is_offline(provider):
                        text = offline_minister_text(character_id, state, question, session.scenario, session.seed, report)
                    else:
                        messages = minister_chat_messages(character_id, state, question, session.scenario, session.seed, report)
                        text = (await provider.chat(messages)).content
                except Exception:  # noqa: BLE001 - 回奏生成失败时用离线模板，不影响已结算的回合
                    text = offline_minister_text(character_id, state, question, session.scenario, session.seed, report)
            meta_out = {
                "character": character_id,
                "mode": meta.get("mode"),
                "mode_label": meta.get("mode_label"),
                "turn": state.turn,
                "asked_turn": asked_turn,
                "question": question,
                "via": meta.get("via"),
            }
            session.append_message("reply", text, meta_out)
            replies.append({**meta_out, "name": character.name, "text": text})
        return {"replies": replies, "written_opinions": written}

    REVIEW_LIMIT = 24

    def final_review(self, session: SessionStore, state: WorldState) -> dict[str, Any] | None:
        """终局复盘：结局与判定条件、目标状态、关键数值（隐藏数值只给风闻区间）、时间线要事。

        要事按优先级取（至多 REVIEW_LIMIT 条，按时间排列）：
        3 — 使结局条件成立的事（旗标由某事件设下、阈值条件由某事件 / 诏令越过，取最后一次越过）、终局之季的事件、终局本身；
        2 — 剧本 importance ≥ 2 的事件、战事、事件抉择（含逾期默认处置）、统帅任免（带兵的官职、易帅）与统帅到任；
        0/1 — 其余可见事件。优先级 3 的全部保留；截断时 omitted 记被略去的条数。
        """
        if state.outcome is None:
            return None
        stats = [{"id": key, "display": resource_display(state, key, session.seed)} for key in state.resources]
        own = mover_faction(state, None)
        troops = sum(row.strength for row in state.armies.values() if own is None or row.faction == own)
        held = sum(1 for row in state.regions.values() if own is None or row.controller in (None, own))
        stats.append({"id": "_armies", "display": f"本方兵力 {format_number(troops)}"})
        stats.append({"id": "_regions", "display": f"本方控制 {held}/{len(state.regions)} 地"})
        rows = self._review_rows(session, state)
        keep = sorted(rows, key=lambda row: (-row[0], row[1], row[2]))
        chosen = [row for row in keep if row[0] >= 3]
        chosen.extend(row for row in keep if row[0] < 3)
        chosen = chosen[: max(self.REVIEW_LIMIT, sum(1 for row in rows if row[0] >= 3))]
        return {
            "outcome": state.outcome.model_dump(mode="json"),
            "outcome_when": turn_label(state, state.outcome.turn),
            "satisfied": list(state.outcome.satisfied),
            "objectives": self.objectives(session, state),
            "stats": stats,
            "highlights": [row[3] for row in sorted(chosen, key=lambda row: (row[1], row[2]))],
            "omitted": len(rows) - len(chosen),
            "turns_played": state.turn - session.initial_state.turn,
        }

    def _review_rows(self, session: SessionStore, state: WorldState) -> list[tuple[int, int, int, str]]:
        """复盘候选要事 [(优先级, 回合, 顺序, 文本)]，见 final_review。"""
        assert state.outcome is not None
        definition = self.definition(session)
        ending = next(
            (row for row in definition.get("endings", []) or [] if isinstance(row, dict) and str(row.get("id")) == state.outcome.id),
            {},
        )
        leaves = _condition_leaves(ending.get("conditions"))
        flag_keys = {str(leaf.get("key")) for leaf in leaves if leaf.get("kind") == "flag" and leaf.get("key")}
        thresholds = [leaf for leaf in leaves if leaf.get("kind") in {"resource", "counter", "army_strength", "region_stat", "relation"}]
        rows: list[tuple[int, int, int, str]] = []
        crossings: dict[int, tuple[int, int, str]] = {}
        before: WorldState = session.initial_state
        turn, label = before.turn, before.calendar.label
        order = 0

        def add(priority: int, text: str, at: int | None = None) -> None:
            nonlocal order
            order += 1
            when = at if at is not None else turn
            rows.append((priority, when, order, text))

        for entry in session.history():
            kind = entry["type"]
            payload = entry.get("payload") or {}
            after = session._snapshot(entry["id"])
            if kind == "op":
                op = payload.get("op") or {}
                name = op.get("op")
                source = str(payload.get("source") or "诏令")
                if name == "choose_event_option":
                    choice = next((row for row in before.pending_choices if row.id == op.get("choice_id")), None)
                    option = next((row for row in choice.options if row.id == op.get("option_id")), None) if choice else None
                    if choice is not None:
                        add(2, f"{label}（第{turn}回合）：抉择「{choice.title}」→ {option.text if option else op.get('option_id')}")
                elif name == "appoint" and office_army(before, str(op.get("office"))):
                    army = before.armies[str(office_army(before, str(op.get("office"))))]
                    add(2, f"{label}（第{turn}回合）：{source}——{entity_name(before, op.get('character'))}任{entity_name(before, op.get('office'))}，统{army.name}")  # noqa: E501
                elif name == "set_army_commander" and op.get("character") and before.armies.get(str(op.get("army"))) is not None:
                    if before.armies[str(op.get("army"))].commander != op.get("character"):
                        add(2, f"{label}（第{turn}回合）：{source}——{entity_name(before, op.get('army'))}由{entity_name(before, op.get('character'))}统领")
                elif name == "detach":
                    strength = format_number(float(op.get("strength", 0) or 0))
                    add(1, f"{label}（第{turn}回合）：{source}——{entity_name(before, op.get('army'))}分兵 {strength} 赴{entity_name(before, op.get('to_region'))}")  # noqa: E501
                self._note_crossings(thresholds, before, after, turn, f"{label}（第{turn}回合）：{source}", crossings, order)
            elif kind == "turn":
                report = payload.get("report") or {}
                snapshot_label = str((after.calendar.label if after else "") or f"第{report.get('turn')}回合")
                at = int(report.get("turn", after.turn) or after.turn)
                final_turn = state.outcome.turn == at
                facts = report.get("facts", []) or []
                flagged: set[str] = set()
                pending_flags: list[str] = []
                for fact in facts:
                    fact_kind = fact.get("kind")
                    if fact_kind == "flag" and str(fact.get("key")) in flag_keys:
                        pending_flags.append(str(fact.get("key")))
                    elif fact_kind == "event":
                        if pending_flags:
                            flagged.add(str(fact.get("entity")))
                        pending_flags = []
                    elif fact_kind == "choice_default":
                        add(2, f"{snapshot_label}（第{at}回合）：{fact.get('text')}", at)
                    elif fact_kind in {"appointment", "army_commander"} and not fact.get("internal") and "统" in str(fact.get("text", "")):
                        add(2, f"{snapshot_label}（第{at}回合）：{fact.get('text')}", at)
                    elif fact_kind == "arrival":
                        who = str(fact.get("entity"))
                        commands = [row.name for row in after.armies.values() if row.commander == who and row.region == fact.get("after")]
                        if who in after.characters and commands:
                            add(2, f"{snapshot_label}（第{at}回合）：{fact.get('text')}，到军统{'、'.join(commands)}", at)
                for event in report.get("events", []) or []:
                    if event.get("internal"):
                        continue
                    if event.get("outcome"):
                        add(3, f"{snapshot_label}（第{at}回合）：终局「{event.get('title')}」", at)
                        continue
                    importance = int(event.get("importance", 0) or 0)
                    if final_turn or str(event.get("id")) in flagged:
                        priority = 3
                    else:
                        priority = 2 if importance >= 2 else importance
                    add(priority, f"{snapshot_label}（第{at}回合）：{event.get('title')}", at)
                for battle in report.get("battles", []) or []:
                    add(2, f"{snapshot_label}（第{at}回合）：{battle.get('region_name', '')}之战，{battle_text(battle, state)}", at)
                self._note_crossings(thresholds, before, after, at, f"{snapshot_label}（第{at}回合）", crossings, order, report)
                turn, label = after.turn, after.calendar.label
            before = after
        for priority, at, text in crossings.values():
            order += 1
            rows.append((priority, at, order, text))
        return rows

    @staticmethod
    def _note_crossings(
        thresholds: list[dict[str, Any]],
        before: WorldState,
        after: WorldState,
        turn: int,
        prefix: str,
        crossings: dict[int, tuple[int, int, str]],
        order: int,
        report: dict[str, Any] | None = None,
    ) -> None:
        """阈值条件（如「禄山兵柄 ≤ 10」）由不成立变为成立：记下这一步（事件 / 诏令）；同一条件只留最后一次越过。"""
        for index, leaf in enumerate(thresholds):
            try:
                if matches(leaf, before, turn) or not matches(leaf, after, turn):
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            cause = ""
            if report is not None:
                key = str(leaf.get("id") or leaf.get("key") or leaf.get("army") or "")
                causes = [
                    str(fact.get("cause") or "")
                    for fact in report.get("facts", []) or []
                    if fact.get("entity") == key and fact.get("cause")
                ]
                cause = causes[-1] if causes else "结算"
            text = f"{prefix}：{cause + '——' if cause else ''}{describe_condition(leaf, after)}（结局条件由此成立）"
            crossings[index] = (3, turn, text)

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
        cards: list[dict[str, Any]] | None = None,
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
                cards=cards,
            )
        except AgentAborted as exc:
            raise GameError("ABORTED", "诏令处理已中止") from exc
        except TimeoutError as exc:
            raise GameError("TIMEOUT", "诏令解释超时，请稍后重试") from exc
        finally:
            self.abort_events.pop(session_id, None)
        # 摘要按条款先后累积预演：先分兵、后调余部时，后一条写的是分兵之后的兵额（延期条款本季不预演）
        preview = clone(state)
        for clause in receipt["clauses"]:
            op = clause.get("op")
            # 延期（或未获执行）的条款尚未生效：任命在任所者写「令达后即日视事」（只有已执行的条款才写「即日视事」）
            pending = clause.get("status") != "applied"
            clause["summary"] = summarize_ops([op], preview, pending=pending)[0] if isinstance(op, dict) and op else ""
            if isinstance(op, dict) and op:
                clause["summary"] += self.deadline_note(session, preview, op)
                if clause.get("status") == "applied":
                    try:
                        preview, _ = apply(op, preview, inplace=True)
                    except ValueError:
                        pass
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

    def deadline_note(self, session: SessionStore, state: WorldState, op: dict[str, Any]) -> str:
        """调动（move_army / march / move_character / detach）预计抵达晚于终局时限时的提醒；否则空串。"""
        if op.get("op") not in {"move_army", "march", "move_character", "detach"} or state.outcome is not None:
            return ""
        notice_length = self.definition(session).get("game_length") or {}
        if notice_length.get("last_turn") is None:
            return ""
        last_turn = int(notice_length["last_turn"])
        overtime = notice_length.get("overtime_flag")
        if overtime and state.flags.get(str(overtime)) not in (None, False) and notice_length.get("overtime_last_turn") is not None:
            last_turn = int(notice_length["overtime_last_turn"])
        mover = state.characters.get(str(op.get("character"))) if op.get("op") == "move_character" else state.armies.get(str(op.get("army")))
        if mover is None or mover.in_transit or mover.region == str(op.get("to_region")):
            return ""
        found = find_route(state, mover.region, str(op.get("to_region")), getattr(mover, "faction", None))
        if found is None:
            return ""
        eta = state.turn + max(1, found[1])
        return f"（预计{turn_label(state, eta)}抵达，晚于终局时限 {turn_label(state, last_turn)}，本局内到不了）" if eta > last_turn else ""

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
            pending = self.unconfirmed_drafts(session, session.head_state(), include_superseded=True)
            listing = "、".join(row["edict_id"] for row in pending)
            hint = f"本季待确认的草案：{listing}" if listing else "本季没有待确认的草案"
            raise GameError("EDICT_NOT_FOUND", f"诏令草案不存在或已过期：{edict_id}。{hint}（edict_id 只取 edict- 开头的编号本身）")
        if receipt.get("confirmed"):
            raise GameError("EDICT_CONFIRMED", "诏令已经确认")
        state = session.head_state()
        if state.outcome is not None:
            raise GameError("GAME_OVER", f"本局已终局（{state.outcome.title}），不能再下诏")
        if receipt.get("discarded"):
            raise GameError("EDICT_STALE", "此草案已在结算时放弃（discard_drafts），请重新拟诏")
        if receipt.get("base_turn") != state.turn:
            raise GameError("EDICT_STALE", "回合已推进，此草案已失效，请重新拟诏")
        # 日志头未动则状态必然未变；头变了（召对消息等不改状态的条目也会移动它）再比较状态哈希。
        state_changed = receipt.get("base_head") != session.head_id and receipt.get("base_hash") != state_hash(state)
        receipt["state_changed"] = state_changed
        receipt["changed_clauses"] = self._revalidate_draft(session, state, receipt) if state_changed else []
        clauses: list[dict[str, Any]] = receipt["clauses"]
        # 将领自主：有统帅之军的军令按种子随机判定拒命或擅动，命中者改为下季结算时执行（见 domain.military）
        autonomy: list[dict[str, Any]] = []
        for clause in clauses:
            op = clause.get("op")
            if clause["status"] in {"applied", "delayed"} and isinstance(op, dict) and op.get("op") in MILITARY_ORDERS:
                roll = roll_autonomy(state, op, session.seed)
                if roll is not None:
                    clause.update({
                        "status": "delayed",
                        "delay_kind": "autonomy",
                        "delay_reason": roll["label"],
                        "due_turn": state.turn + 1,
                        "autonomy": roll["result"],
                    })
                    autonomy.append(roll)
        if autonomy:
            cascade_delays(state, clauses)  # 军令被将领延宕后，倚赖它的条款随之延期
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
            if clause["status"] == "delayed" and clause.get("op") and clause.get("delay_kind") != "autonomy":
                group = delayed.setdefault(clause.get("group", f"clause-{index}"), (self._clause_label(clause), []))
                group[1].append(clause["op"])
        for label, ops in delayed.values():
            # 同组（同一奏疏选项 / 同一段诏令）的延期条款排成一个延期包，下季一并执行。
            session.append_pending(ops, state.turn + 1, label=label)
        for roll in autonomy:
            session.append_pending(roll["ops"], state.turn + 1, source="autonomy", label=roll["label"])
        flags = [
            {"op": "set_flag", "key": memorial_flag(str(row["memorial_id"])), "value": row["option_id"]}
            for row in receipt.get("adopted", [])
        ]
        used_cards = dict.fromkeys(
            str(clause["card_id"])
            for clause in clauses
            if clause.get("source") == "card" and clause.get("card_id") and clause["status"] in {"applied", "delayed"}
        )
        flags.extend({"op": "set_flag", "key": card_flag(card_id), "value": state.turn} for card_id in used_cards)
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
        receipt["new_memorials"] = self.new_memorials(session, state, session.head_state())
        receipt["state"] = public_view(session.head_state(), session.seed)
        return receipt

    def new_memorials(self, session: SessionStore, before: WorldState, after: WorldState) -> list[dict[str, Any]]:
        """本季因状态变化（确认诏书、抉择）新出现的奏疏：[{id, title}]。"""
        if after.outcome is not None:
            return []
        seen = {str(row["id"]) for row in visible_memorials(session.scenario, before)}
        return [{"id": row["id"], "title": row["title"]} for row in visible_memorials(session.scenario, after) if str(row["id"]) not in seen]

    def unconfirmed_drafts(self, session: SessionStore, state: WorldState, include_superseded: bool = False) -> list[dict[str, Any]]:
        """本季拟下、尚未确认（也未放弃）且仍有可执行条款的草案：[{edict_id, summary}]（按拟诏先后）。

        - 回合已推进的草案（EDICT_STALE）不算；全部条款被拒 / 仅记录的草案不算；
        - 只含奏疏条款、而这些奏疏都已另行处理的草案不算；
        - 「已被取代」：此后又有本季草案被确认（拟诏—改拟—确认改拟稿的常见流程），旧稿不算（include_superseded 时仍列出，
          供 EDICT_NOT_FOUND 提示可确认的编号）。
        """
        drafts = list(session.drafts().items())
        memorials = {str(row.get("id")): row for row in session.scenario.get("memorials") or [] if isinstance(row, dict)}
        rows: list[dict[str, Any]] = []
        for position, (edict_id, receipt) in enumerate(drafts):
            if receipt.get("confirmed") or receipt.get("discarded") or receipt.get("base_turn") != state.turn:
                continue
            live = [row for row in receipt.get("clauses") or [] if row.get("status") in {"applied", "delayed"} and row.get("op")]
            if not live:
                continue
            if all(row.get("source") == "memorial" for row in live) and not any(
                str(row.get("memorial_id")) in memorials and is_visible(memorials[str(row.get("memorial_id"))], state) for row in live
            ):
                continue
            superseded = any(
                later.get("confirmed") and later.get("base_turn") == state.turn for _, later in drafts[position + 1:]
            )
            if superseded and not include_superseded:
                continue
            summary = "；".join(dict.fromkeys(str(row.get("summary") or row.get("text") or "") for row in live if row.get("summary") or row.get("text")))
            rows.append({"edict_id": edict_id, "summary": summary[:160]})
        return rows

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
                tier = PLAYER_TIER if clause.get("source") in {"client", "text", "card"} else SCENARIO_TIER
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
        before = session.head_state()
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
            "new_memorials": self.new_memorials(session, before, state),
            "state": public_view(state, session.seed),
        }

    async def end_turn(self, session_id: str, narrate: bool = True, discard_drafts: bool = False) -> dict[str, Any]:
        """结算本季。本季有拟而未确认的草案时拒绝（DRAFT_UNCONFIRMED，列出编号），以免本季诏令悄然遗失；
        discard_drafts=True 时把这些草案标为已放弃再结算（返回 discarded_drafts）。"""
        session = self.session(session_id)
        if session.state.outcome is not None:
            raise GameError("GAME_OVER", f"本局已终局（{session.state.outcome.title}），不能再推进回合")
        pending = self.unconfirmed_drafts(session, session.head_state())
        if pending and not discard_drafts:
            listing = "；".join(f"{row['edict_id']}（{row['summary'] or '…'}）" for row in pending)
            raise GameError(
                "DRAFT_UNCONFIRMED",
                f"本季尚有未确认的诏书草案：{listing}。草案不确认便不施行——请先 confirm_edict；"
                "若确要放弃这些草案，以 discard_drafts: true 再结算。",
            )
        for row in pending:
            receipt = session.load_draft(row["edict_id"])
            if receipt is not None:
                receipt["discarded"] = True
                session.save_draft(receipt)
        context = self.season_context(session)
        asked_turn = session.head_state().turn
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
        delivered = await self._deliver_dispatches(session, asked_turn, state)
        self.touch(session_id)
        return {
            "replies": delivered["replies"],
            "written_opinions": delivered["written_opinions"],
            "state": public_view(state, session.seed),
            "report": public,
            "memorial": memorial,
            "outcome": state.outcome.model_dump(mode="json") if state.outcome else None,
            "calendar_label": state.calendar.label,
            # 刚结算完的那一季（事实、事件都发生在这一季里），供「某季结算毕，进入某季」的标题
            "settled_label": turn_label(state, state.turn - 1, short=True) if state.turn > 0 else "",
            "final_notice": final_season_notice(state, self.definition(session)),
            "discarded_drafts": [row["edict_id"] for row in pending],
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
        """玩家本人不能召对或与议；他国、敌方之人（势力不同于玩家势力）也不能。"""
        if character_id and character_id == self.player_character(session, state):
            name = state.characters[character_id].name if character_id in state.characters else character_id
            raise GameError("PLAYER_CHARACTER", f"{name}是玩家本人，不能召对或与议")
        character = state.characters.get(character_id)
        own = state.rules.get("player_faction")
        if character is not None and own and character.faction and character.faction != own:
            faction = state.factions.get(character.faction)
            raise GameError("CHARACTER_UNAVAILABLE", f"{character.name}属{faction.name if faction else character.faction}，非本朝臣属，不能召对或与议")

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
        """非流式召对（MCP server 模式用；朝会日程由调用方先以 begin_audience 计数）。"""
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

    def _debate_participants(
        self, session: SessionStore, state: WorldState, participants: list[str], written: list[str] | None = None
    ) -> tuple[list[str], list[str]]:
        """当场与议者（须在京且不在途，否则 NOT_PRESENT）与递书面议状者（须不在京）。"""
        unique = list(dict.fromkeys(participants))
        remote = [row for row in dict.fromkeys(written or []) if row not in unique]
        if len(unique) > 6 or len(remote) > 6 or not (unique or remote):
            raise GameError("INVALID_REQUEST", "廷议参与者须为 1 到 6 人（书面议状另计，至多 6 人）")
        settings = self.settings(session)
        for character_id in [*unique, *remote]:
            character = self._character(state, character_id)
            self._reject_player(session, state, character_id)
            if character.status != "alive":
                raise GameError("CHARACTER_UNAVAILABLE", f"{character.name}不在朝，不能与议")
        for character_id in unique:
            if not presence(state, character_id, settings)["present"]:
                raise GameError("NOT_PRESENT", not_present_message(state, character_id, settings))
        for character_id in remote:
            if presence(state, character_id, settings)["present"]:
                name = state.characters[character_id].name
                raise GameError("INVALID_REQUEST", f"{name}在京，可列入 participants 当场与议，不必递书面议状")
        return unique, remote

    def record_call(self, session_id: str, kind: str, **fields: Any) -> None:
        """client 模式召对 / 廷议的轻量记录（export_log 用；不含台词，不改日志与状态）。"""
        session = self.session(session_id)
        session.record_call({"kind": kind, "turn": session.head_state().turn, "via": "client", **fields})

    def _debate_setup(
        self,
        session: SessionStore,
        state: WorldState,
        topic: str,
        participants: list[str],
        memorial_id: str | None,
        option_id: str | None = None,
        written: list[str] | None = None,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        """廷议共用部分：议题（可由 memorial_id 补出）、立场与议题分析（topic_entities / 对应奏疏）。

        written 为不在京、递书面议状者：其立场与理由按同一规则算出，记入日志，下季结算后作为谏言（来源「书面议状」）送达，
        不参与当场辩论。校验通过后计一次廷议（朝会日程，已满 AGENDA_EXHAUSTED）。"""
        try:
            memorial = match_memorial(session.scenario, state, topic, memorial_id or None)
        except MemorialError as exc:
            raise GameError(exc.code, exc.message) from exc
        if not topic.strip() and memorial is not None:
            topic = str(memorial.get("title", memorial_id))
        if not topic.strip():
            raise GameError("INVALID_REQUEST", "廷议议题不能为空")
        unique, remote = self._debate_participants(session, state, participants, written)
        options = [row for row in (memorial or {}).get("options", []) or [] if isinstance(row, dict)]
        if option_id:
            if memorial is None:
                raise GameError("INVALID_REQUEST", "指定所议之案（option_id）须同时给出 memorial_id")
            if option_id not in {str(row.get("id")) for row in options}:
                ids = "、".join(str(row.get("id")) for row in options)
                raise GameError("UNKNOWN_OPTION", f"奏疏「{memorial.get('title', memorial.get('id'))}」没有选项：{option_id}（可选：{ids}）")
        stances = decide_stances(
            state,
            session.scenario,
            topic,
            [*unique, *remote],
            memorial_id=str(memorial["id"]) if memorial else None,
            seed=session.seed,
            proposal_option=option_id or None,
        )
        written_stances = [row for row in stances if row["character"] in remote]
        head_row = stances[0] if stances else {}
        stances = [row for row in stances if row["character"] in unique]
        proposal: dict[str, Any] | None = None
        if memorial is not None and options:
            listing = "；".join(f"`{row.get('id')}`「{row.get('label', row.get('id'))}」" for row in options)
            head = head_row
            if head.get("proposal_id"):
                proposal = {"id": head["proposal_id"], "label": head.get("proposal_label"), "basis": head.get("proposal_basis", "")}
                proposition = (
                    f"就奏疏「{memorial.get('title', memorial.get('id'))}」诸选项择一（{listing}）；所议之案为「{proposal['label']}」"
                    f"（`{proposal['id']}`，{proposal['basis']}）：赞成即主此案，反对即主他案；各人立场附所主张的选项"
                )
            else:
                proposition = (
                    f"就奏疏「{memorial.get('title', memorial.get('id'))}」诸选项择一（{listing}）；诸案并陈，未有定案"
                    "（可用 option_id 指定所议之案）：各人只列所主张的选项，不以首个选项为案"
                )
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
            "proposal": proposal,
            "written": [self._written_notice(state, row) for row in written_stances],
        }
        self._count_agenda(session, "debate")
        for row in written_stances:
            session.append_message(
                "written_opinion",
                self._written_text(state, topic, row),
                {"character": row["character"], "topic": topic, "turn": state.turn, "memorial_id": (memorial or {}).get("id")},
            )
        self.touch(session.path.stem)
        return topic, stances, analysis

    @staticmethod
    def _written_notice(state: WorldState, row: dict[str, Any]) -> dict[str, Any]:
        """当场回执只说书面议状已发、下季送达；立场与理由届时才见（不提前外露）。"""
        character = state.characters[row["character"]]
        where = travel_note(state, character) if character.in_transit else (
            state.regions[character.region].name if character.region in state.regions else character.region
        )
        return {"character": row["character"], "name": character.name, "whereabouts": where, "note": "书面议状已发，其议于下季送达（入谏言库）"}

    @staticmethod
    def _written_text(state: WorldState, topic: str, row: dict[str, Any]) -> str:
        character = state.characters[row["character"]]
        where = "途中" if character.in_transit else (state.regions[character.region].name if character.region in state.regions else "外镇")
        position = f"；{row['position']}" if row.get("position") and row.get("target_option_id") else ""
        return f"〔书面议状·自{where}驿递〕就「{topic}」{row.get('stance_label', row.get('stance'))}：{row.get('reason', '')}{position}"

    def debate_brief(
        self,
        session_id: str,
        topic: str,
        participants: list[str],
        memorial_id: str | None = None,
        option_id: str | None = None,
        written: list[str] | None = None,
    ) -> dict[str, Any]:
        """client 模式：只给立场与人设，由调用方模型写台词。"""
        session = self.session(session_id)
        state = session.state
        topic, stances, analysis = self._debate_setup(session, state, topic, participants, memorial_id, option_id, written)
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

    async def debate(
        self,
        session_id: str,
        topic: str,
        participants: list[str],
        memorial_id: str | None = None,
        option_id: str | None = None,
        written: list[str] | None = None,
    ) -> dict[str, Any]:
        session = self.session(session_id)
        state = session.state
        topic, stances, analysis = self._debate_setup(session, state, topic, participants, memorial_id, option_id, written)
        try:
            speeches = await debate_speeches(
                state, session.scenario, topic, stances, self.provider(session.scenario, "court_debate"), session.seed, session.last_report()
            ) if stances else []
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
        agenda = view["agenda"]
        if state.outcome is None and (agenda["audience"]["limit"] is not None or agenda["debate"]["limit"] is not None):
            remote = "远地召对（书面奏对 / 遣使问对）不当场回话，回奏于下季送达" if agenda["remote_reply"] == "next_season" else "远地召对当场书面作答"
            lines.append(f"朝会日程（本季）：{agenda['text']}（每季结算后重置；{remote}）")
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
        if any(row.hidden for row in state.resources.values()):
            lines.append("- 风闻区间每季按真值重新估测，前后两季的区间不宜直接相比来看趋势；趋势看「上季危机变化」。")
        legend = stat_legend(state)
        if legend:
            lines.append(f"- 地区指标量程：{legend}（到上下限即止，结算事实注明「已至上限 / 下限」）")
        own = state.rules.get("player_faction")
        if own in state.factions and state.factions[own].relations:
            alive = {row.controller for row in state.regions.values()} | {row.faction for row in state.armies.values() if row.strength > 0}
            ties = [
                f"{state.factions[other].name} {format_number(value)}"
                for other, value in state.factions[own].relations.items()
                if other in state.factions and other != own and other in alive  # 已亡之国（无地无兵）不列
            ]
            if ties:
                lines.append(f"- 诸国关系（{state.factions[own].name}对各方，正为亲睦、负为敌对）：" + "；".join(ties))
        upkeep = upkeep_rows(state, (definition.get("formulas") or {}).get("upkeep") or [])
        if upkeep and state.outcome is None:
            lines.append("\n## 养兵经常开支（按现兵额，每季结算时计入经常收支；增兵即增此项）")
            for row in upkeep:
                name = state.resources[row["resource"]].name
                lines.append(f"- {upkeep_label(row)}：本季约 {name} −{format_number(row['cost'])}")
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
        waits = self._pending_waits(state)
        if waits:
            lines.append("\n## 待办与在途（引擎已知的排期：延期条款、在途人军、工程、常设令、账目、待决事件期限）")
            lines.extend(f"- {line}" for line in waits)
        if view["memorials"]:
            lines.append("\n## 本季奏疏（可在 draft_edict 的 adopted 中采纳：{memorial_id, option_id}）")
            for item in view["memorials"]:
                where = f"（{item['region_name']}）" if item.get("region_name") else ""
                if item.get("arrival"):
                    late = ""
                    if item.get("arrived_late") and item.get("origin_turn") is not None:
                        late = f"；远地晚到一季，原发于{turn_label(state, item['origin_turn'])}"
                    where = f"（{item['arrival']}{late}）"
                lines.append(f"### [{item['id']}] {item['title']}{where}")
                if item.get("summary"):
                    lines.append(item["summary"])
                for advisor in item["advisors"]:
                    stance = {"support": "赞成", "oppose": "反对", "neutral": "持中"}.get(advisor["stance"], advisor["stance"])
                    lines.append(f"- 谏言 {advisor['name']}（{stance}）：{advisor['line']}")
                for option in item["options"]:
                    ops = "；".join(option["ops_summary"]) or "不改变状态"
                    ops += "".join(self.deadline_note(session, state, raw) for raw in option.get("ops", []) if isinstance(raw, dict))
                    hint = f" 〔{option['hint']}〕" if option.get("hint") else ""
                    lines.append(f"- 选项 `{option['id']}`「{option['label']}」{hint} → {ops}")
        if view["pending_choices"]:
            lines.append("\n## 待决事件（用 choose 处置：choice_id + option_id）")
            for choice in state.pending_choices:
                options = "；".join(
                    f"`{option.id}`「{option.text}」" + (f"〔{'；'.join(summary)}〕" if (summary := summarize_ops(option.effects, state)) else "")
                    for option in choice.options
                )
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
                lines.append("战报：")
                lines.extend(f"- {row.get('region_name', '')}：{battle_text(row, state)}{battle_breakdown(row)}" for row in report["battles"])
            featured = [row for row in report.get("events", []) if not row.get("outcome")]
            if featured:
                lines.append("上季时事：" + "、".join(f"「{row['title']}」" + ("（远地军报晚到）" if row.get("arrived_late") else "") for row in featured))
        lines.extend(self._dispatch_lines(session, state, view))
        cards = [row for row in cards_view(state, session.scenario) if row["available"]] if state.outcome is None else []
        if cards:
            lines.append("\n## 政令卡（policy_cards 查看参数与可选值；draft_edict 的 cards: [{card_id, params}] 使用）")
            lines.append("；".join(f"[{row['id']}] {row['title']}（{row['category']}）" for row in cards))
        player = self.player_character(session, state)
        own = state.rules.get("player_faction")  # 他国、敌方之人不能召对（见 _reject_player），不列
        people = [
            f"{row.name}（`{row.id}`{'，' + role if (role := _role_label(state, row)) else ''}"
            f"{'，' + note if (note := travel_note(state, row)) else ''}）"
            for row in state.characters.values()
            if row.status == "alive" and row.id != player and (not own or not row.faction or row.faction == own)
        ]
        lines.append("\n## 可召对人物\n" + "；".join(people))
        lines.append("\n" + ROLEPLAY_RULES)
        return "\n".join(lines)

    def _dispatch_lines(self, session: SessionStore, state: WorldState, view: dict[str, Any]) -> list[str]:
        """简报「回奏」：上季遣使所问今已送达（client 模式附人设卡与事实摘要，由调用方扮演）；书面议状（已入谏言库）。"""
        lines: list[str] = []
        replies = view.get("replies") or []
        if replies:
            lines.append("\n## 回奏（上季遣使 / 书面所问，今已送达）")
            for row in replies:
                question = f"所问「{row['question']}」" if row.get("question") else "所问（未具问话）"
                if not row.get("roleplay"):
                    lines.append(f"- {row['name']}（{row.get('mode_label') or '遣使问对'}，{question}）：{row.get('text', '')}")
                    continue
                character_id = str(row["character"])
                if character_id not in state.characters or state.characters[character_id].status in {"dead", "imprisoned"}:
                    lines.append(f"- {row['name']}（{question}）：其人已不能回奏。")
                    continue
                card = self.audience_card(session.path.stem, character_id)
                lines.append(f"### {row['name']}的回奏（{row.get('mode_label') or '遣使问对'}，{question}）——请按下列人设卡与事实摘要扮演此人的书面回奏")
                lines.append(self.persona_text(card["persona"]))
                lines.append(f"此人可知的事实摘要（StateDigest）：\n{card['digest']}")
                lines.append(f"口吻要求：{card['voice']}（此为书面回奏，非当面应对。）")
        written = view.get("written_opinions") or []
        if written:
            lines.append("\n## 书面议状（上季廷议时不在京者所递，今已送达，已入谏言库）")
            for row in written:
                who = state.characters[row["character"]].name if row["character"] in state.characters else row["character"]
                lines.append(f"- {who}：{row['text']}")
        return lines

    @staticmethod
    def persona_text(persona: dict[str, Any]) -> str:
        """人设卡的文字版（MCP audience 与简报「回奏」共用）。"""
        return (
            f"# 人设卡：{persona['name']}（{persona['character']}）\n"
            f"官职：{'、'.join(persona['offices']) or '无'}；派系：{persona['bloc'] or '无'}；头衔：{persona['title'] or '无'}"
            + (f"（旧衔：{persona['former_title']}，已非现职）" if persona.get("former_title") else "")
            + "\n"
            f"人物：{persona['persona']}\n言谈：{persona['speech_style']}\n所知：{'、'.join(persona['knowledge']) or '—'}\n"
            + (f"开场白：{persona['greeting']}\n" if persona.get("greeting") else "")
            + (f"示例台词：{' / '.join(persona['examples'])}\n" if persona.get("examples") else "")
        ).rstrip("\n")

    @staticmethod
    def _review_lines(review: dict[str, Any]) -> list[str]:
        outcome = review["outcome"]
        lines = [
            "\n## 终局复盘",
            f"结局：{outcome['title']}（{'失败' if outcome['kind'] == 'fail' else '结局'}，{review['outcome_when']}）；共历 {review['turns_played']} 回合。",
        ]
        if review["satisfied"]:
            lines.append("判定条件：" + "；".join(f"「{line}」" for line in review["satisfied"]))
        status_names = {"on_track": "守住", "at_risk": "未达", "failed": "已失", "achieved": "达成", "pending": "待定"}
        if review["objectives"]:
            lines.append("目标：" + "；".join(f"{row['title']} {status_names.get(row['status'], row['status'])}" for row in review["objectives"]))
        lines.append("关键数值：" + "；".join(row["display"] for row in review["stats"]))
        if review["highlights"]:
            lines.append("要事（使结局条件成立之事、终局之季、抉择、统帅任免与到任、兵事；按时间）：")
            lines.extend(f"- {line}" for line in review["highlights"])
            if review.get("omitted"):
                lines.append(f"（另有 {review['omitted']} 条从略，完整时间线见 history / export_log。）")
        lines.append("（本局已终局：不再有奏疏与待决事件；可用 history / rewind 回到更早节点另开分支。）")
        return lines

    @staticmethod
    def _traveller_lines(state: WorldState) -> list[str]:
        """在途的人物与本方军队：「某某（自A赴B，约n季后到；预计某年某季（第n回合）抵达）」。"""
        own = mover_faction(state, None)
        movers: list[Any] = [row for row in state.characters.values() if row.in_transit and row.status == "alive"]
        movers.extend(row for row in state.armies.values() if row.in_transit and (own is None or row.faction == own) and row.strength > 0)
        rows = []
        for row in movers:
            eta = row.in_transit.get("eta_turn") if row.in_transit else None
            when = f"；预计{turn_label(state, eta)}抵达" if eta is not None else ""
            rows.append(f"{row.name}（{travel_note(state, row)}{when}）")
        return rows

    @staticmethod
    def _pending_waits(state: WorldState) -> list[str]:
        """简报「待办与在途」：引擎已知、将在某回合发生的事，每条带到期回合。"""
        if state.outcome is not None:
            return []
        rows: list[str] = []
        for clause in state.pending_clauses:
            summary = "；".join(summarize_ops(clause.ops, state, pending=True))
            rows.append(f"延期条款「{clause.label or '延期诏令'}」：{summary}——{delay_phrase(clause.due_turn)}")
        rows.extend(f"在途：{line}" for line in GameService._traveller_lines(state))
        for project in state.projects:
            left = max(1, project.turns - project.progress)
            eta = "资金不足已停工，竣工时间待定" if project.stalled else f"预计{turn_label(state, state.turn + left)}竣工"
            rows.append(f"工程「{project.name}」：进度 {project.progress}/{project.turns}，{eta}")
        for order in state.standing_orders:
            if order.until_turn is not None:
                rows.append(f"常设令「{order.text}」：有效至{turn_label(state, order.until_turn)}")
        for entry in state.ledger:
            if entry.kind == "recurring" and entry.turns is not None:
                rows.append(f"经常账目「{entry.name}」：尚余 {entry.turns} {turn_unit_label(state)}，期满自动终止")
        for choice in state.pending_choices:
            rows.append(f"待决事件「{choice.title}」：{turn_label(state, choice.deadline_turn)}结算前未决则依默认处置")
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


def _condition_leaves(conditions: Any) -> list[dict[str, Any]]:
    """条件树的叶子（all / any 展开；not 包着的叶子不算阈值越过，跳过）。"""
    if isinstance(conditions, list):
        return [leaf for item in conditions for leaf in _condition_leaves(item)]
    if not isinstance(conditions, dict):
        return []
    for key in ("all", "any"):
        if key in conditions:
            return _condition_leaves(conditions[key])
    if "not" in conditions:
        return []
    return [conditions] if conditions.get("kind") else []


def choice_view(choice: Any, state: WorldState) -> dict[str, Any]:
    """待决事件的公开视图：每个选项另带 summary（效果摘要，与奏疏选项同一写法；风闻数值只写方向）。"""
    row = choice.model_dump(mode="json")
    for option, raw in zip(row.get("options") or [], choice.options, strict=False):
        option["summary"] = summarize_ops(raw.effects, state)
    return row
