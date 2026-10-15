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

from simcore.agent.debate import debate_speeches, debate_summary, decide_stances, persona_card
from simcore.agent.loop import AgentAborted
from simcore.agent.roles import chat_availability, interpret_edict, minister_chat_messages, narrator
from simcore.config import load_settings
from simcore.domain.apply import OpRejected
from simcore.domain.digest import render_digest
from simcore.domain.events import matches
from simcore.domain.memorials import summarize_ops, visible_memorials
from simcore.domain.models import TurnReport, WorldState
from simcore.domain.ops import OP_NAMES, op_json_schema
from simcore.domain.rules import held_offices
from simcore.domain.turn import GameOver
from simcore.domain.visibility import fact_line, public_crisis, public_report, public_view, redacted_report, resource_display
from simcore.llm.base import LLMProvider
from simcore.llm.factory import create_provider
from simcore.scenario.loader import build_initial_state, load_scenario, validate_scenario
from simcore.store.session import SessionStore

ENGINE_VERSION = "0.2.0"

ERROR_STATUS = {
    "SCENARIO_NOT_FOUND": 404,
    "SESSION_NOT_FOUND": 404,
    "EDICT_NOT_FOUND": 404,
    "ENTRY_NOT_FOUND": 404,
    "UNKNOWN_ENTITY": 404,
    "UNKNOWN_CHARACTER": 404,
    "UNKNOWN_MEMORIAL": 404,
    "NOT_FOUND": 404,
    "INVALID_SCENARIO": 422,
    "INVALID_SCENARIO_PATH": 400,
    "INVALID_REQUEST": 422,
    "GAME_OVER": 409,
    "EDICT_CONFIRMED": 409,
    "EDICT_STALE": 409,
    "ABORTED": 409,
    "CHARACTER_UNAVAILABLE": 409,
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
        self.drafts: dict[tuple[str, str], dict[str, Any]] = {}
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
                    "asset_ids": [entry.get("id", "") for entry in data.get("assets", {}).get("entries", [])],
                    "theme_css": theme.read_text(encoding="utf-8") if theme.is_file() else "",
                    "valid": not errors,
                })
            except (OSError, KeyError, ValueError, GameError):
                continue
        return result

    def create_session(self, scenario_id: str, seed: int = 0) -> tuple[str, SessionStore]:
        scenario = self.scenario(scenario_id)
        errors = validate_scenario(scenario)
        if errors:
            raise GameError("INVALID_SCENARIO", "场景校验未通过：" + "；".join(errors[:5]))
        initial = build_initial_state(scenario)
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
                    "created": session.header.get("timestamp"),
                })
            except (OSError, KeyError, ValueError, IndexError, GameError):
                continue
        return result

    def delete_session(self, session_id: str) -> dict[str, Any]:
        session = self.session(session_id)
        self.sessions.pop(session_id, None)
        self.session_mtimes.pop(session_id, None)
        for key in [key for key in self.drafts if key[0] == session_id]:
            self.drafts.pop(key, None)
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

    def view(self, session_id: str) -> dict[str, Any]:
        session = self.session(session_id)
        state = session.state
        crisis = self.definition(session).get("crisis")
        report = session.last_report()
        return {
            "session_id": session_id,
            "scenario": state.scenario_id,
            "turn": state.turn,
            "state": public_view(state, session.seed),
            "calendar_label": state.calendar.label,
            "objectives": self.objectives(session, state),
            "crisis": public_crisis(crisis, state, session.seed),
            "memorials": visible_memorials(session.scenario, state),
            "pending_choices": [row.model_dump(mode="json") for row in state.pending_choices],
            "outcome": state.outcome.model_dump(mode="json") if state.outcome else None,
            "last_report": public_report(report, state, session.seed, crisis),
            "can_advance": state.outcome is None,
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
        receipt["base_turn"] = state.turn
        self.drafts[(session_id, receipt["id"])] = receipt
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
        session = self.session(session_id)
        receipt = self.drafts.get((session_id, edict_id))
        if receipt is None:
            raise GameError("EDICT_NOT_FOUND", "诏令草案不存在或已过期")
        if receipt.get("confirmed"):
            raise GameError("EDICT_CONFIRMED", "诏令已经确认")
        state = session.state
        if state.outcome is not None:
            raise GameError("GAME_OVER", f"本局已终局（{state.outcome.title}），不能再下诏")
        if receipt.get("base_turn") != state.turn:
            raise GameError("EDICT_STALE", "回合已推进，此草案已失效，请重新拟诏")
        for clause in receipt["clauses"]:
            if clause["status"] == "applied" and clause.get("op"):
                try:
                    session.append_op(clause["op"])
                except ValueError as exc:
                    clause["status"] = "rejected"
                    clause["reason"] = getattr(exc, "message", str(exc))
                    clause["code"] = getattr(exc, "code", "INVALID_TARGET")
            elif clause["status"] == "delayed" and clause.get("op"):
                session.append_pending([clause["op"]], session.state.turn + 1)
        for adopted in receipt.get("adopted", []):
            session.append_op({"op": "set_flag", "key": f"memorial:{adopted['memorial_id']}", "value": adopted["option_id"]})
        self.touch(session_id)
        receipt["confirmed"] = True
        receipt["state"] = public_view(session.state, session.seed)
        return receipt

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
        try:
            state, report, _ = session.append_turn()
        except GameOver as exc:
            raise GameError("GAME_OVER", str(exc)) from exc
        crisis = self.definition(session).get("crisis")
        public = public_report(report, state, session.seed, crisis) or {}
        facts = [str(fact.get("text", "")) for fact in public.get("facts", [])]
        if narrate:
            try:
                memorial = await narrator(redacted_report(report, state), self.provider(session.scenario, "narrator"), session.scenario)
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
        }

    # ---- 人物：召对、廷议、谏言 -------------------------------------------------------

    def _character(self, state: WorldState, character_id: str) -> Any:
        character = state.characters.get(character_id)
        if character is None:
            raise GameError("UNKNOWN_CHARACTER", f"人物不存在：{character_id}")
        return character

    def audience_card(self, session_id: str, character_id: str) -> dict[str, Any]:
        session = self.session(session_id)
        state = session.state
        character = self._character(state, character_id)
        afterlife = bool(self.definition(session).get("settings", {}).get("afterlife_chat", False))
        refusal = chat_availability(character_id, state, afterlife)
        if refusal is not None:
            raise GameError("CHARACTER_UNAVAILABLE", f"{character.name}{'已故' if character.status == 'dead' else '系狱'}，无法召对")
        return {
            "persona": persona_card(state, character_id),
            "digest": render_digest(state, session.seed, character_id, session.last_report(), session.scenario),
            "memories": list(character.memories[-12:]),
            "voice": (
                f"以{character.name}的身份与口吻作答：{character.speech_style or '言辞得体'}。"
                "只说此人可能知道的事；数字只能引用上面事实摘要中的值，风闻区间不得说成确数；"
                "不得声称已经执行任何政令。"
            ),
        }

    async def chat(self, session_id: str, character_id: str, text: str) -> dict[str, Any]:
        """非流式召对（MCP server 模式用）。"""
        session = self.session(session_id)
        self.audience_card(session_id, character_id)
        messages = minister_chat_messages(character_id, session.state, text, session.scenario, session.seed, session.last_report())
        result = await self.provider(session.scenario, "minister_chat").chat(messages)
        session.append_message("player", text, {"character": character_id})
        session.append_message("character", result.content, {"character": character_id, "status": "ok"})
        self.touch(session_id)
        return {"status": "ok", "character": character_id, "content": result.content}

    def chat_prepare(self, session_id: str, character_id: str) -> tuple[SessionStore, dict[str, str] | None]:
        session = self.session(session_id)
        state = session.state
        afterlife = bool(self.definition(session).get("settings", {}).get("afterlife_chat", False))
        return session, chat_availability(character_id, state, afterlife)

    def _debate_participants(self, state: WorldState, participants: list[str]) -> list[str]:
        unique = list(dict.fromkeys(participants))
        if not 1 <= len(unique) <= 6:
            raise GameError("INVALID_REQUEST", "廷议参与者须为 1 到 6 人")
        for character_id in unique:
            character = self._character(state, character_id)
            if character.status != "alive":
                raise GameError("CHARACTER_UNAVAILABLE", f"{character.name}不在朝，不能与议")
        return unique

    def debate_brief(self, session_id: str, topic: str, participants: list[str]) -> dict[str, Any]:
        """client 模式：只给立场与人设，由调用方模型写台词。"""
        session = self.session(session_id)
        state = session.state
        unique = self._debate_participants(state, participants)
        stances = decide_stances(state, session.scenario, topic, unique)
        for row in stances:
            row["persona"] = persona_card(state, row["character"])
        return {
            "topic": topic,
            "stances": stances,
            "digest": render_digest(state, session.seed, None, session.last_report(), session.scenario),
            "summary": debate_summary(stances),
            "instructions": "按 stances 的顺序为每人写一段不超过三句的发言；立场不可更改；数字只能引用 digest 中的值。",
        }

    async def debate(self, session_id: str, topic: str, participants: list[str]) -> dict[str, Any]:
        session = self.session(session_id)
        state = session.state
        if not topic.strip():
            raise GameError("INVALID_REQUEST", "廷议议题不能为空")
        unique = self._debate_participants(state, participants)
        stances = decide_stances(state, session.scenario, topic, unique)
        try:
            speeches = await debate_speeches(
                state, session.scenario, topic, stances, self.provider(session.scenario, "court_debate"), session.seed, session.last_report()
            )
        except TimeoutError as exc:
            raise GameError("TIMEOUT", "廷议生成超时") from exc
        summary = debate_summary(stances)
        session.append_message("debate", json.dumps({"topic": topic, "speeches": speeches}, ensure_ascii=False), {"topic": topic})
        self.touch(session_id)
        return {"topic": topic, "speeches": speeches, "summary": summary}

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
        if state.outcome is not None:
            lines.append(f"**本局已终局：{state.outcome.title}**（{'失败' if state.outcome.kind == 'fail' else '结局'}）{state.outcome.text}")
        if view["objectives"]:
            lines.append("\n## 目标")
            status_names = {"on_track": "守住", "at_risk": "告急", "failed": "已失", "achieved": "达成", "pending": "待定"}
            lines.extend(f"- {row['title']}：{status_names.get(row['status'], row['status'])}。{row['detail']}" for row in view["objectives"])
        lines.append("\n## 国势（公开数值）")
        lines.extend(f"- {resource_display(state, key, session.seed)}" for key in state.resources)
        crisis = view["crisis"]
        if crisis:
            value = crisis["value_or_estimate"]
            value_text = f"约{value[0]}–{value[1]}（风闻）" if isinstance(value, list) else str(value)
            lines.append(f"\n## 危机计：{crisis['title']} {value_text}（{crisis['band']}）")
            for term in crisis["terms"]:
                term_value = term["value"]
                text = f"约{term_value[0]}–{term_value[1]}（风闻）" if isinstance(term_value, list) else str(term_value)
                lines.append(f"- 分项 {term['label']}：{text}")
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
        if state.pending_choices:
            lines.append("\n## 待决事件（用 choose 处置：choice_id + option_id）")
            for choice in state.pending_choices:
                options = "；".join(f"`{option.id}`「{option.text}」" for option in choice.options)
                lines.append(
                    f"- [{choice.id}] {choice.title}：{choice.text} 选项：{options}。"
                    f"默认 `{choice.default_option}`，若第{choice.deadline_turn}回合结算前未决则依默认执行。"
                )
        report = view["last_report"]
        if report:
            facts = [fact_line(fact) for fact in report.get("facts", []) if fact.get("kind") not in {"flag", "counter"}]
            lines.append("\n## 上季事实")
            lines.extend(f"- {fact}" for fact in facts[:40]) if facts else lines.append("- （无）")
            if report.get("battles"):
                lines.append("战报：" + "；".join(f"{row['region_name']}：{row['result']}" for row in report["battles"]))
        people = [
            f"{row.name}（{row.id}{'，' + '、'.join(state.offices[o].name for o in held_offices(row) if o in state.offices) if held_offices(row) else ''}）"
            for row in state.characters.values()
            if row.status == "alive"
        ]
        lines.append("\n## 可召对人物\n" + "；".join(people))
        lines.append("\n" + ROLEPLAY_RULES)
        return "\n".join(lines)

    def op_reference(self, session_id: str) -> dict[str, Any]:
        session = self.session(session_id)
        state = session.state
        return {
            "ops": list(OP_NAMES),
            "schema": op_json_schema(),
            "entities": {
                "resources": [{"id": row.id, "name": row.name, "hidden": row.hidden, "unit": row.unit} for row in state.resources.values()],
                "characters": [
                    {"id": row.id, "name": row.name, "status": row.status, "offices": held_offices(row), "region": row.region, "bloc": row.bloc}
                    for row in state.characters.values()
                ],
                "offices": [{"id": row.id, "name": row.name, "holder": row.holder, "requires": row.requires} for row in state.offices.values()],
                "regions": [
                    {"id": row.id, "name": row.name, "controller": row.controller, "adjacency": row.adjacency, "stats": list(row.stats)}
                    for row in state.regions.values()
                ],
                "armies": [
                    {
                        "id": row.id,
                        "name": row.name,
                        "faction": row.faction,
                        "region": row.region,
                        "strength": row.strength,
                        "commander": row.commander,
                        "in_transit": bool(row.in_transit),
                    }
                    for row in state.armies.values()
                ],
                "factions": [{"id": row.id, "name": row.name} for row in state.factions.values()],
                "ledger": [{"id": row.id, "name": row.name} for row in state.ledger],
                "standing_orders": [{"id": row.id, "text": row.text} for row in state.standing_orders],
                "pending_choices": [{"id": row.id, "options": [option.id for option in row.options]} for row in state.pending_choices],
            },
            "rules": {
                "max_concurrent_offices": state.rules.get("max_concurrent_offices", 1),
                "notes": [
                    "每条 op 是一个 JSON 对象，op 字段为操作名；未知字段会被拒绝。",
                    "move_character / move_army 的目标必须与所在地相邻；行程按邻接表的回合数计。",
                    "appoint 前须满足官职 requires；已达兼职上限时先 dismiss。",
                    "被拒条款返回稳定错误码：UNKNOWN_ENTITY、INVALID_TARGET、OFFICE_OCCUPIED、OFFICE_HELD、OFFICE_REQUIREMENT、"
                    "CHARACTER_UNAVAILABLE、OUT_OF_RANGE、INSUFFICIENT_RESOURCE、CONDITION_FAILED、GAME_OVER。",
                ],
            },
        }
