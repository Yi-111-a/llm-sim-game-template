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
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from simcore.agent.debate import ENTITY_KIND_LABELS
from simcore.domain.apply import OpRejected
from simcore.domain.visibility import battle_text, fact_line, format_number
from simcore.service import ENGINE_VERSION, ROLEPLAY_RULES, GameError, GameService

INSTRUCTIONS = """这是一个历史模拟策略游戏的引擎（This MCP server runs a deterministic history-simulation strategy game）。
你扮演君主（the player is the ruler），引擎是唯一事实来源。
推荐流程 / Typical loop:
1. list_scenarios → new_game(scenario, seed) 得到 session_id 与开局简报（或 list_saves → load_game 继续存档）。
2. 每季先读 briefing(session_id)：目标、公开国势、危机计分项、本季奏疏（含选项与 ops 摘要）、待决事件、上季事实。
3. 需要时 audience 召见人物（你按返回的人设卡与事实摘要扮演），court_debate 举行廷议（立场由引擎给定），save_advice 把有用谏言存入谏言库。
4. 用 draft_edict 拟诏：adopted 采纳奏疏选项，ops 直接给出 StateOp（格式见 op_reference）；看回执中每条 applied / rejected / delayed，再 confirm_edict。
5. 用 choose 处置待决事件；然后 end_turn 结算本季，读取事实、战报、危机分项与终局。
6. 终局后（outcome 非空）不能再推进；可用 history / rewind 回到更早的节点另开分支。
数字只能引用简报与工具返回中给出的值；「风闻」数值只有区间。"""

DESCRIPTIONS: dict[str, str] = {
    "list_scenarios": "列出可玩的场景（id、标题、时代、是否通过校验）。List available scenarios. 无参数。",
    "new_game": (
        "新开一局：scenario 为 list_scenarios 返回的 id，seed 为随机种子（同种子可复现），difficulty 为难度档 id"
        "（见 list_scenarios 的 difficulties；留空用场景默认）。返回 session_id 与开局简报。"
        "Start a new game from a scenario id with a seed and optional difficulty; returns the session_id and the opening briefing."
    ),
    "list_saves": "列出全部存档（session_id、场景、回合）。List saved sessions (shared with the desktop app via LLM_SIM_HOME).",
    "load_game": "读取存档并返回当前简报。Load a saved session by session_id and return its briefing.",
    "briefing": (
        "当季简报：历法、目标状态、公开资源（隐藏资源只给风闻区间）、危机计及分项、本季奏疏（含选项与 ops 摘要）、待决事件、上季事实，"
        "末尾附扮演与数字规则。每季行动前先读它。"
        "Seasonal briefing for the ruler, ending with the role-play / number rules you must follow."
    ),
    "op_reference": (
        "玩家可直接下达的操作（permitted_ops：一行字段说明 + 示例）与实体 id 表（每类至多 12 行，附总数）。"
        "其余操作（调整资源/地区/关系、旗标等）只能经奏疏选项或事件产生，客户端提交会得到 OP_NOT_PERMITTED。"
        "过滤：op=<操作名> 看完整 JSON Schema；entity_kind=characters|offices|regions|armies|resources|factions|ledger|standing_orders|pending_choices"
        " 看完整表；ids=[...] 只看这些 id。Player-permitted ops with examples and entity ids; filter with op / entity_kind / ids."
    ),
    "audience": (
        "召对某位人物：character 为人物 id。client 模式返回人设卡 + 该人物可知的事实摘要 + 记忆 + 口吻要求，由你扮演作答；"
        "server 模式需同时给 question，返回服务端模型的回答。死者或系狱者会被拒绝。"
        "Audience with a character: returns a persona card and what they know (client mode) or their reply (server mode)."
    ),
    "court_debate": (
        "廷议：topic 为议题，participants 为 1–6 个人物 id，memorial_id 可选（就某条本季奏疏廷议；给了它 topic 可留空）。"
        "议题对应奏疏时用奏疏中的谏言立场，否则引擎按议题点名的人物/势力/官职/地区/军队与各人的亲疏、派系利益与忠诚给出立场"
        "（support/oppose/neutral），并列出 topic_entities；client 模式返回立场 + 人设，由你写台词；server 模式返回各人台词。"
        "廷议结论只能存为谏言，不直接改变状态。Court debate: engine-decided, topic-dependent stances; you (client mode) write the speeches."
    ),
    "save_advice": "把谏言存入谏言库（不改变世界状态）：character 为进言人 id，text 为谏言原文。Save a piece of advice to the advice log.",
    "draft_edict": (
        "拟诏书草案（不改变状态）。sections 为 {类别: 诏令文本}（client 模式仅作记录，server 模式由模型解释）；"
        "adopted 为 [{memorial_id, option_id}] 采纳奏疏选项；ops 为 StateOp 列表（见 op_reference）。"
        "回执逐条给出 applied / rejected（原因与错误码）/ delayed（下季执行），并给出 edict_id。"
        "Draft an edict; returns a per-clause receipt and an edict_id for confirm_edict."
    ),
    "confirm_edict": "确认诏书草案，使 applied 条款生效、delayed 条款排入下季，并记下已采纳的奏疏。Confirm a drafted edict by edict_id.",
    "choose": "处置待决事件：choice_id 与 option_id 见 briefing 的「待决事件」。Resolve a pending event choice.",
    "end_turn": (
        "结算本季并进入下一季：返回事实、账目汇总、战报、触发事件、新的待决事件、危机分项与终局（若有）。终局后再调用会报 GAME_OVER。"
        "Resolve the season and return the TurnReport."
    ),
    "history": "存档时间线（含全部分支）：每个节点的 id、类型、回合、说明、父节点，标出当前所在。List the session timeline.",
    "export_log": (
        "结构化复盘日志（只读）：按季列出采纳的奏疏选项、客户端 ops、拟诏与确认回执、延期条款、事件抉择、召对与廷议、"
        "end_turn 的事实 / 事件 / 对账（含经常收支明细）/ 危机计与本季危机变化拆分、终局。from_turn / to_turn 按下诏所在季筛选。"
        "隐藏数值只给风闻区间。Structured per-season log export (public view only)."
    ),
    "rewind": "回到时间线上的某个节点（entry_id；留空回到开局），之后的操作另开分支，原分支保留。Rewind to an entry id.",
}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1)


def build_server(service: GameService | None = None, mode: str | None = None) -> MCPServer:
    service = service or GameService()
    mode = (mode or os.environ.get("SIM_MCP_MODE", "client")).strip().lower()
    if mode not in {"client", "server"}:
        mode = "client"
    server_mode = mode == "server"
    server = MCPServer(name="tianbao", instructions=INSTRUCTIONS, version=ENGINE_VERSION, log_level="WARNING")

    def register(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> str:
            try:
                return await fn(*args, **kwargs)
            except GameError as exc:
                raise ToolError(f"错误 [{exc.code}]：{exc.message}") from None
            except OpRejected as exc:
                raise ToolError(f"错误 [{exc.code}]：{exc.message}") from None
            except ToolError:
                raise
            except Exception as exc:  # noqa: BLE001 - 工具错误一律转为可读说明
                raise ToolError(f"错误 [INTERNAL]：{type(exc).__name__}：{exc}") from None

        server.add_tool(wrapper, name=fn.__name__, description=DESCRIPTIONS[fn.__name__])
        return wrapper

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
        card = service.audience_card(session_id, character)
        mode = card["mode"]
        if server_mode and question.strip():
            reply = await service.chat(session_id, character, question)
            label = "" if mode["mode"] == "in_person" else f"（{mode['label']}）"
            return f"{card['persona']['name']}{label}：{reply['content']}"
        persona = card["persona"]
        return (
            f"# 召对方式：{mode['label']}——{mode['note']}\n"
            f"# 人设卡：{persona['name']}（{persona['character']}）\n"
            f"官职：{'、'.join(persona['offices']) or '无'}；派系：{persona['bloc'] or '无'}；头衔：{persona['title'] or '无'}\n"
            f"人物：{persona['persona']}\n言谈：{persona['speech_style']}\n所知：{'、'.join(persona['knowledge']) or '—'}\n"
            + (f"开场白：{persona['greeting']}\n" if persona.get("greeting") else "")
            + (f"示例台词：{' / '.join(persona['examples'])}\n" if persona.get("examples") else "")
            + "\n"
            f"# 此人可知的事实摘要（StateDigest；【记忆】为此人记得的事）\n{card['digest']}\n\n"
            f"# 口吻要求\n{card['voice']}\n\n{ROLEPLAY_RULES}"
        )

    @register
    async def court_debate(session_id: str, participants: list[str], topic: str = "", memorial_id: str = "") -> str:
        if server_mode:
            result = await service.debate(session_id, topic, participants, memorial_id or None)
            lines = [f"# 廷议：{result['topic']}", *_debate_context(result)]
            lines.extend(
                f"- {row['name']}（{row['stance']}{'；' + row['position'] if row.get('target_option_id') else ''}）：{row['text']}"
                for row in result["speeches"]
            )
            lines.append(result["summary"])
            return "\n".join(lines)
        brief = service.debate_brief(session_id, topic, participants, memorial_id or None)
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
        lines.extend(["", "## 事实摘要", brief["digest"], "", brief["summary"]])
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
    ) -> str:
        receipt = await service.draft_edict(session_id, sections or {}, adopted or [], ops or [], use_provider=server_mode)
        lines = [f"诏书草案 edict_id={receipt['id']}（未生效；确认请调用 confirm_edict）"]
        lines.extend(_clause_lines(receipt["clauses"]))
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
        lines.extend(_clause_lines(receipt["clauses"]))
        if receipt.get("adopted"):
            lines.append("已处理奏疏：" + "、".join(f"{row['memorial_id']}→{row['option_id']}" for row in receipt["adopted"]))
        return "\n".join(lines)

    @register
    async def choose(session_id: str, choice_id: str, option_id: str) -> str:
        result = service.choose(session_id, choice_id, option_id)
        facts = "；".join(fact_line(fact) for fact in result["facts"])
        return f"已处置 {choice_id} → {option_id}。{facts}"

    @register
    async def end_turn(session_id: str) -> str:
        result = await service.end_turn(session_id, narrate=server_mode)
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

    return server


STATUS_TAGS = {
    "applied": "applied 可执行",
    "rejected": "rejected 被拒",
    "delayed": "delayed 下季执行",
    "narrative_only": "narrative_only 仅记录",
}


def _clause_lines(clauses: list[dict[str, Any]]) -> list[str]:
    lines = []
    for index, clause in enumerate(clauses, start=1):
        status = clause["status"]
        tag = STATUS_TAGS.get(status, status)
        source = clause.get("source", "text")
        what = clause.get("summary") or clause.get("text") or ""
        reason = f"；原因：{clause.get('reason')}" if clause.get("reason") else ""
        if status == "delayed":
            due = clause.get("due_turn")
            reason += f"；延期原因：{clause.get('delay_reason') or '有司稽迟'}" + (f"；预计第{due}回合结算时执行" if due is not None else "")
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
    build_server().run("stdio")


if __name__ == "__main__":
    main()
