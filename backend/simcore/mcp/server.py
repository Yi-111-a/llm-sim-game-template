"""MCP 服务：让外部模型（Claude Code、Codex 等）通过 stdio 游玩任何场景。

- 存档目录与桌面应用相同：LLM_SIM_HOME；场景目录：SIM_SCENARIOS_DIR。
- SIM_MCP_MODE=client（默认）：调用方模型负责扮演与解释诏令，诏书只接受 ops / adopted；
  SIM_MCP_MODE=server：由服务端配置的 provider 解释诏令文本、生成召对与廷议台词。
- 所有工具只返回公开视图；错误一律为「错误 [CODE]：中文说明」，不抛堆栈。
"""

from __future__ import annotations

import functools
import json
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from simcore.agent.debate import ENTITY_KIND_LABELS
from simcore.domain.apply import OpRejected
from simcore.domain.rules import delay_phrase
from simcore.domain.visibility import battle_breakdown, battle_text, fact_line, format_number
from simcore.mcp.meta import DESCRIPTIONS as DESCRIPTIONS
from simcore.mcp.meta import INSTRUCTIONS
from simcore.service import ENGINE_VERSION, ENVOY_SENT, ROLEPLAY_RULES, GameError, GameService

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

ToolFn = Callable[..., Awaitable[str]]


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1)


def resolve_mode(mode: str | None) -> str:
    mode = (mode or os.environ.get("SIM_MCP_MODE", "client")).strip().lower()
    return mode if mode in {"client", "server"} else "client"


def tool_error_text(exc: BaseException) -> str:
    """工具错误一律转为「错误 [CODE]：中文说明」，不抛堆栈。"""
    if isinstance(exc, (GameError, OpRejected)):
        return f"错误 [{exc.code}]：{exc.message}"
    return f"错误 [INTERNAL]：{type(exc).__name__}：{exc}"


def build_server(service: GameService | None = None, mode: str | None = None) -> MCPServer:
    """官方 MCP SDK 版服务（进程内测试、其他传输用）。stdio 启动走 simcore.mcp.stdio 的轻量实现。"""
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    server = MCPServer(name="tianbao", instructions=INSTRUCTIONS, version=ENGINE_VERSION, log_level="WARNING")

    def wrap(fn: ToolFn) -> ToolFn:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> str:
            try:
                return await fn(*args, **kwargs)
            except ToolError:
                raise
            except Exception as exc:  # noqa: BLE001 - 工具错误一律转为可读说明
                raise ToolError(tool_error_text(exc)) from None

        return wrapper

    for name, fn in make_tools(service, mode).items():
        server.add_tool(wrap(fn), name=name, description=DESCRIPTIONS[name])
    return server


def make_tools(service: GameService | None = None, mode: str | None = None) -> dict[str, ToolFn]:
    """全部工具函数（名称 → async 函数，返回文本；出错抛 GameError / OpRejected 等）。"""
    service = service or GameService()
    mode = resolve_mode(mode)
    server_mode = mode == "server"
    tools: dict[str, ToolFn] = {}

    def register(fn: ToolFn) -> ToolFn:
        tools[fn.__name__] = fn
        return fn

    @register
    async def list_scenarios() -> str:
        rows = [
            {
                "id": row["folder"],
                "title": row["title"],
                "era": row["era"],
                "version": row["version"],
                "valid": row["valid"],
                "difficulties": row.get("difficulties", []),
            }
            for row in service.list_scenarios()
        ]
        return _dump(rows)

    @register
    async def new_game(scenario: str, seed: int = 0, difficulty: str = "") -> str:
        session_id, _ = service.create_session(scenario, seed, difficulty or None)
        return f"已开新局 session_id={session_id}（mode={mode}）\n\n" + service.briefing(session_id)

    @register
    async def list_saves() -> str:
        return _dump(service.list_sessions())

    @register
    async def load_game(session_id: str) -> str:
        return service.briefing(session_id)

    @register
    async def briefing(session_id: str) -> str:
        return service.briefing(session_id)

    @register
    async def op_reference(session_id: str, op: str = "", entity_kind: str = "", ids: list[str] | None = None) -> str:
        return _dump(service.op_reference(session_id, op or None, entity_kind or None, ids or None))

    @register
    async def audience(session_id: str, character: str, question: str = "") -> str:
        # 朝会日程：已满 AGENDA_EXHAUSTED；远地召对（next_season）只发使者，回奏于下季送达
        started = service.begin_audience(session_id, character, question, via=mode)
        how = started["mode"]
        agenda = started["agenda"]["text"]
        if started["deferred"]:
            card = service.audience_card(session_id, character)
            name = card["persona"]["name"]
            service.record_call(
                session_id, "audience", character=character, name=name, mode=how["mode"], mode_label=how["label"], deferred=True
            )
            follow = (
                "下季结算时由服务端生成回奏，见下季 briefing「回奏」一节"
                if server_mode
                else "下季 briefing「回奏」一节给出此人的人设卡与事实摘要，届时由你扮演其书面回奏（本季不得代其作答）"
            )
            asked = f"，所问「{question.strip()}」" if question.strip() else ""
            return f"# 召对方式：{how['label']}——{how['note']}\n{ENVOY_SENT}：{name}{asked}。{follow}。\n朝会日程（本季）：{agenda}"
        card = service.audience_card(session_id, character)
        if server_mode and question.strip():
            reply = await service.chat(session_id, character, question)
            label = "" if how["mode"] == "in_person" else f"（{how['label']}）"
            return f"{card['persona']['name']}{label}：{reply['content']}\n（朝会日程：{agenda}）"
        persona = card["persona"]
        service.record_call(session_id, "audience", character=character, name=persona["name"], mode=how["mode"], mode_label=how["label"])
        return (
            f"# 召对方式：{how['label']}——{how['note']}（朝会日程：{agenda}）\n"
            + service.persona_text(persona)
            + "\n\n"
            f"# 此人可知的事实摘要（StateDigest；【记忆】为此人记得的事）\n{card['digest']}\n\n"
            f"# 口吻要求\n{card['voice']}\n\n{ROLEPLAY_RULES}"
        )

    @register
    async def court_debate(
        session_id: str,
        participants: list[str],
        topic: str = "",
        memorial_id: str = "",
        option_id: str = "",
        written: list[str] | None = None,
    ) -> str:
        if server_mode:
            result = await service.debate(session_id, topic, participants, memorial_id or None, option_id or None, written or [])
            lines = [f"# 廷议：{result['topic']}", *_debate_context(result)]
            lines.extend(
                f"- {row['name']}（{row['stance']}{'；' + row['position'] if row.get('target_option_id') else ''}）：{row['text']}"
                for row in result["speeches"]
            )
            lines.append(result["summary"])
            lines.extend(_written_lines(result.get("written")))
            return "\n".join(lines)
        brief = service.debate_brief(session_id, topic, participants, memorial_id or None, option_id or None, written or [])
        service.record_call(
            session_id,
            "debate",
            topic=brief["topic"],
            memorial_id=(brief.get("memorial") or {}).get("id"),
            option_id=(brief.get("proposal") or {}).get("id"),
            participants=[row["character"] for row in brief["stances"]],
        )
        lines = [f"# 廷议：{brief['topic']}", *_debate_context(brief), brief["instructions"], "", "## 立场（引擎给定，不可更改）"]
        for row in brief["stances"]:
            persona = row["persona"]
            if row.get("target_option_id"):
                target = f"{row['position']}（{row.get('target_reason') or row['reason']}）。"
            else:
                target = f"{row['position']}。" if row.get("position") else ""
            lines.append(
                f"- {row['name']}（{row['character']}）：{row['stance_label']}（{row['stance']}）——{row['reason']}。{target}"
                f"官职：{'、'.join(persona['offices']) or '无'}；人物：{persona['persona']}；言谈：{persona['speech_style']}"
            )
        if not brief["stances"]:
            lines.append("- （无人在京当场与议）")
        lines.extend(["", "## 事实摘要", brief["digest"], "", brief["summary"]])
        lines.extend(_written_lines(brief.get("written")))
        return "\n".join(lines)

    @register
    async def policy_cards(session_id: str) -> str:
        rows = service.policy_cards(session_id)
        if not rows:
            return "本剧本没有政令卡。"
        lines = ["# 政令卡（draft_edict 的 cards: [{card_id, params: {参数名: 可选值 id 或数}}]；展开为玩家层条款，照常校验）"]
        for row in rows:
            state = "可用" if row["available"] else f"不可用：{row['unavailable_reason']}"
            lines.append(f"## [{row['id']}] {row['title']}（{row['category']}；{state}）")
            if row.get("summary"):
                lines.append(row["summary"])
            if row.get("cost_hint"):
                lines.append(f"代价：{row['cost_hint']}")
            for param in row["params"]:
                if param.get("kind") == "amount":
                    lines.append(f"- 参数 `{param['name']}`「{param['label']}」：数，{param.get('min')}～{param.get('max')}")
                else:
                    options = "；".join(
                        f"`{item['id']}` {item['name']}" + (f"（{item['note']}）" if item.get("note") else "") for item in param.get("options", [])[:20]
                    )
                    more = f"……（共 {len(param['options'])} 项）" if len(param.get("options", [])) > 20 else ""
                    lines.append(f"- 参数 `{param['name']}`「{param['label']}」：{options or '（无可选值）'}{more}")
        return "\n".join(lines)

    @register
    async def save_advice(session_id: str, character: str, text: str) -> str:
        row = service.add_advice(session_id, character, text, "mcp")
        return f"已存入谏言库：{row['id']}（第{row['turn']}回合，{row['character']}）"

    @register
    async def draft_edict(
        session_id: str,
        sections: dict[str, str] | None = None,
        adopted: list[dict[str, str]] | None = None,
        ops: list[dict[str, Any]] | None = None,
        cards: list[dict[str, Any]] | None = None,
    ) -> str:
        receipt = await service.draft_edict(session_id, sections or {}, adopted or [], ops or [], use_provider=server_mode, cards=cards or [])
        lines = [
            f"诏书草案 edict_id={receipt['id']}",
            f"（未生效：须调用 confirm_edict，edict_id 填 {receipt['id']}；未确认的草案会使 end_turn 被拒）",
        ]
        lines.extend(_clause_lines(receipt["clauses"], confirmed=False))
        if not receipt["clauses"]:
            lines.append("（草案为空）")
        return "\n".join(lines)

    @register
    async def confirm_edict(session_id: str, edict_id: str) -> str:
        receipt = service.confirm_edict(session_id, edict_id)
        lines = [f"诏书 {edict_id} 已确认。"]
        if receipt.get("changed_clauses"):
            numbers = "、".join(str(index) for index in receipt["changed_clauses"])
            lines.append(f"注意：拟诏之后局势有变，已按当前状态重新校验；第 {numbers} 条的结果与草案不同（见下）。")
        lines.extend(_clause_lines(receipt["clauses"], confirmed=True))
        if receipt.get("adopted"):
            lines.append("已处理奏疏：" + "、".join(f"{row['memorial_id']}→{row['option_id']}" for row in receipt["adopted"]))
        lines.extend(_new_memorial_lines(receipt.get("new_memorials")))
        return "\n".join(lines)

    @register
    async def choose(session_id: str, choice_id: str, option_id: str) -> str:
        result = service.choose(session_id, choice_id, option_id)
        facts = "；".join(fact_line(fact) for fact in result["facts"])
        return "\n".join([f"已处置 {choice_id} → {option_id}。{facts}", *_new_memorial_lines(result.get("new_memorials"))])

    @register
    async def end_turn(session_id: str, discard_drafts: bool = False) -> str:
        result = await service.end_turn(session_id, narrate=server_mode, discard_drafts=discard_drafts)
        return _turn_text(result, show_memorial=server_mode)

    @register
    async def history(session_id: str) -> str:
        rows = service.history(session_id)
        lines = ["id | 回合 | 类型 | 说明 | 父节点"]
        for row in rows:
            marker = " ←当前" if row["head"] else (" *" if row["on_path"] else "")
            lines.append(f"{row['id']} | {row['turn']} | {row['type']} | {row['label']} | {row['parentId'] or '-'}{marker}")
        if len(lines) == 1:
            lines.append("（尚无记录）")
        return "\n".join(lines)

    @register
    async def export_log(session_id: str, from_turn: int | None = None, to_turn: int | None = None) -> str:
        return _dump(service.export_log(session_id, from_turn, to_turn))

    @register
    async def rewind(session_id: str, entry_id: str | None = None) -> str:
        view = service.rewind(session_id, entry_id)
        return f"已回到 {entry_id or '开局'}：{view['calendar_label']}（第{view['turn']}回合）。之后的操作将另开分支。"

    return tools


def timing_text(clause: dict[str, Any], confirmed: bool = False) -> str:
    """条款何时生效。草案（未确认）：「可行（确认后本季即行）」/「确认后延至下季结算时执行（第 n 回合）」；
    确认后：「已行」/「已延至下季结算时执行（第 n 回合）」。"""
    status = clause.get("status")
    if status == "applied":
        return "已行" if confirmed else "可行（确认后本季即行）"
    if status == "delayed":
        return ("已" if confirmed else "确认后") + delay_phrase(clause.get("due_turn"))
    return ""


STATUS_TAGS = {
    "rejected": "rejected 被拒",
    "narrative_only": "narrative_only 仅记录",
}


def _written_lines(rows: Any) -> list[str]:
    if not rows:
        return []
    return ["## 书面议状（不在京者，不参与当场辩论）", *(f"- {row['name']}（{row['whereabouts']}）：{row['note']}" for row in rows)]


DELAY_KIND_LABELS = {"courier": "驿程", "bureaucratic": "有司稽迟", "autonomy": "将领自主"}


def _new_memorial_lines(rows: Any) -> list[str]:
    if not rows:
        return []
    listing = "、".join(f"[{row['id']}] {row['title']}" for row in rows)
    return [f"本季新出奏疏：{listing}（局势有变而出；可在 end_turn 前继续 draft_edict 采纳，详见 briefing）"]


def _clause_lines(clauses: list[dict[str, Any]], confirmed: bool = False) -> list[str]:
    lines = []
    for index, clause in enumerate(clauses, start=1):
        status = clause["status"]
        tag = f"{status} {timing_text(clause, confirmed)}" if status in {"applied", "delayed"} else STATUS_TAGS.get(status, status)
        source = clause.get("source", "text")
        what = clause.get("summary") or clause.get("text") or ""
        reason = f"；原因：{clause.get('reason')}" if clause.get("reason") else ""
        if status == "delayed":
            kind = DELAY_KIND_LABELS.get(str(clause.get("delay_kind")), "")
            reason += f"；延期原因{'（' + kind + '）' if kind else ''}：{clause.get('delay_reason') or '有司稽迟'}"
        if clause.get("autonomy_risk") and status in {"applied", "delayed"}:
            reason += f"；{clause.get('autonomy_note') or '将领自主风险：' + str(clause['autonomy_risk'])}"
        if clause.get("source") == "card" and clause.get("cost_hint"):
            reason += f"；代价：{clause['cost_hint']}"
        code = f" [{clause['code']}]" if clause.get("code") and status == "rejected" else ""
        changed = f"（草案时为 {clause.get('previous_status')}）" if clause.get("changed") else ""
        lines.append(f"{index}. [{tag}]{code} ({source}) {what}{reason}{changed}")
    return lines


def _debate_context(result: dict[str, Any]) -> list[str]:
    lines = [f"议题命题：{result['proposition']}"] if result.get("proposition") else []
    if result.get("memorial"):
        lines.append(f"对应奏疏：[{result['memorial']['id']}] {result['memorial']['title']}（谏言人按奏疏中的立场）")
    entities = result.get("topic_entities") or []
    if entities:
        lines.append("议题涉及：" + "、".join(f"{row['name']}（{ENTITY_KIND_LABELS.get(row['kind'], row['kind'])}）" for row in entities))
    return lines


def _turn_text(result: dict[str, Any], show_memorial: bool = True) -> str:
    """回合结果文本。终局只在末尾的「终局」段出现一次；事实逐条去重；client 模式的纪事就是事实拼接，不再重复。"""
    report = result["report"]
    lines = [f"# 已结算至 {result['calendar_label']}（第{report.get('turn')}回合）"]
    facts = list(dict.fromkeys(fact_line(fact) for fact in report.get("facts", []) if fact.get("kind") != "outcome"))
    lines.append("## 事实")
    lines.extend(f"- {fact}" for fact in facts) if facts else lines.append("- （无）")
    reconciliation = report.get("reconciliation") or []
    if reconciliation:
        lines.append("## 对账（期初 → 诏令 → 延期执行 → 经常收支 → 事件/选择 → 期末）")
        for row in reconciliation:
            lines.append(f"- {row['display']}")
            if row.get("regular_display"):
                lines.append(f"  - {row['regular_display']}")
        shown = {row["resource"] for row in reconciliation}
        hidden = [
            row.get("name", key)
            for key, row in result["state"]["resources"].items()
            if row.get("hidden") and key not in shown and any(line.get("resource") == key for line in report.get("ledger_lines", []))
        ]
        if hidden:
            lines.append("- " + "、".join(f"{name}：数值不明" for name in hidden) + "（风闻，不入对账）")
    else:
        totals: dict[str, float | None] = {}
        for line in report.get("ledger_lines", []):
            resource = str(line.get("resource"))
            amount = line.get("amount")
            if amount is None or totals.get(resource, 0) is None:
                totals[resource] = None
            else:
                totals[resource] = float(totals.get(resource) or 0) + float(amount)
        if totals:
            lines.append("## 账目合计")
            state_resources = result["state"]["resources"]
            for resource, amount in totals.items():
                name = state_resources.get(resource, {}).get("name", resource)
                lines.append(f"- {name}：{'数值不明' if amount is None else format_number(amount, signed=True)}")
    if report.get("battles"):
        lines.append("## 战报")
        for battle in report["battles"]:
            detail = battle_breakdown(battle)
            if detail:
                lines.append(f"- {battle.get('region_name', '')}：{battle_text(battle)}{detail}")
                continue
            losses = _losses(battle.get("losses"), result["state"].get("armies", {}))
            lines.append(f"- {battle.get('region_name', '')}：{battle_text(battle)}损失 {losses}")
    shown = [row for row in report.get("events", []) if not row.get("outcome")]
    if shown:
        lines.append("## 触发事件：" + "、".join(f"「{row.get('title') or row.get('id')}」" for row in shown))
    if report.get("choices_opened"):
        lines.append("## 新的待决事件")
        for choice in report["choices_opened"]:
            options = "；".join(f"`{option['id']}`「{option['text']}」" for option in choice["options"])
            lines.append(
                f"- [{choice['id']}] {choice['title']}：{options}"
                f"（默认 `{choice['default_option']}`，第{choice['deadline_turn']}回合结算前未决则依默认）"
            )
    crisis = report.get("crisis") or {}
    if crisis:
        lines.append(f"## 危机计 {crisis.get('title')}：{crisis.get('display')}（{crisis.get('band')}）")
        lines.append("分项（按结算后状态计算，即下次结算的预计漂移）：")
        for term in crisis.get("terms", []):
            lines.append(f"- {term.get('label')}：{term.get('display')}")
    change = report.get("crisis_change")
    if change:
        lines.append("## 本季危机变化（结算当时的实际贡献）")
        lines.extend(f"- {line}" for line in change.get("lines", []))
    if report.get("warnings"):
        lines.append("## 提示：" + "；".join(dict.fromkeys(report["warnings"])))
    if show_memorial and result.get("memorial"):
        lines.append(f"## 纪事\n{result['memorial']}")
    if result.get("discarded_drafts"):
        lines.append("## 已放弃的草案（未施行）：" + "、".join(result["discarded_drafts"]))
    if result.get("replies"):
        lines.append("## 回奏送达（上季遣使所问）")
        lines.extend(f"- {row['name']}：{row['text']}" for row in result["replies"])
    if result.get("written_opinions"):
        lines.append("## 书面议状送达（已入谏言库）")
        lines.extend(f"- {row['name']}：{row['text']}" for row in result["written_opinions"])
    if result.get("final_notice"):
        lines.append(f"## 注意：{result['final_notice']['text']}")
    outcome = result.get("outcome")
    if outcome:
        lines.append(f"## 终局：{outcome['title']}（{'失败' if outcome['kind'] == 'fail' else '结局'}）\n{outcome.get('text', '')}")
        if outcome.get("satisfied"):
            lines.append("判定条件：" + "；".join(f"「{line}」" for line in outcome["satisfied"]))
        lines.append("（复盘见 briefing；此后不能再推进回合。）")
    return "\n".join(lines)


def _losses(losses: Any, armies: dict[str, Any]) -> str:
    """战报损失：{军队 id: 数} 换成军队名，数字至多一位小数。"""
    if isinstance(losses, dict):
        parts = []
        for army_id, value in losses.items():
            name = armies.get(army_id, {}).get("name", army_id) if isinstance(armies.get(army_id), dict) else army_id
            parts.append(f"{name} {format_number(value) if isinstance(value, (int, float)) else value}")
        return "，".join(parts) or "无"
    if isinstance(losses, (int, float)) and not isinstance(losses, bool):
        return format_number(losses)
    return str(losses)


def main() -> None:
    from simcore.mcp.stdio import run

    run()


if __name__ == "__main__":
    main()
