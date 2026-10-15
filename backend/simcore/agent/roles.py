from __future__ import annotations

import asyncio
import json
import random
import re
import uuid
from typing import Any

from simcore.agent.loop import guarded_chat
from simcore.agent.tools import AgentTools
from simcore.domain.digest import render_digest
from simcore.domain.memorials import MemorialError, adopted_ops
from simcore.domain.models import TurnReport, WorldState
from simcore.domain.visibility import resource_display
from simcore.llm.base import LLMProvider, ToolCallingUnsupported
from simcore.prompts.loader import render_prompt

TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "propose_ops",
            "description": "提出待规则引擎检查的状态操作；不能直接改写世界状态。",
            "parameters": {
                "type": "object",
                "properties": {"ops": {"type": "array", "items": {"type": "object"}}},
                "required": ["ops"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_ops",
            "description": "提交最终的类型化状态操作。",
            "parameters": {
                "type": "object",
                "properties": {"ops": {"type": "array", "items": {"type": "object"}}},
                "required": ["ops"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_turn",
            "description": "结束当前角色回合，不再调用工具。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


async def interpret_edict(
    sections: dict[str, str],
    scenario: dict[str, Any],
    state: WorldState,
    provider: LLMProvider | None,
    seed: int,
    abort_event: asyncio.Event | None = None,
    adopted: list[dict[str, str]] | None = None,
    client_ops: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """生成诏书草案回执（不改变状态）。

    处理顺序：采纳的奏疏选项（source=memorial）→ 客户端模型直接给出的 ops（source=client）
    → 各类别诏令文本经 LLM 解释（source=text）。所有条款按顺序累积预演，并按同一种子 RNG 判定延期：
    每个奏疏选项、每条客户端 op、每段诏令文本各掷一次（同组条款带相同 group，整组一起执行或一起延期）。
    provider 为 None（MCP client 模式）时，文本只记为 narrative_only。
    """
    tools = AgentTools(state)
    clauses: list[dict[str, Any]] = []
    rng = random.Random(seed + state.turn)
    formulas = scenario.get("scenario", {}).get("formulas", {})
    execution = formulas.get("execution", {}) if isinstance(formulas, dict) else {}
    deadline = asyncio.get_running_loop().time() + 120
    accepted_memorials: list[dict[str, str]] = []

    group_counter = 0

    def add_clauses(receipt: list[dict[str, Any]], base: dict[str, Any]) -> None:
        """一组条款（一个奏疏选项 / 一条客户端 op / 一段诏令文本）只掷一次延期骰，组内 ops 同进同退。"""
        nonlocal group_counter
        group_counter += 1
        delayed = any(item["status"] == "applied" for item in receipt) and rng.random() < _delay_probability(execution, state)
        for item in receipt:
            status = item["status"]
            if status == "applied" and delayed:
                status = "delayed"
            clauses.append({
                **base,
                "group": group_counter,
                "status": status,
                "op": item.get("op"),
                "reason": item.get("reason", ""),
                "code": item.get("code"),
            })

    memorial_items = {str(row.get("id")): row for row in scenario.get("memorials") or [] if isinstance(row, dict)}
    for choice in adopted or []:
        memorial_id = str(choice.get("memorial_id", ""))
        option_id = str(choice.get("option_id", ""))
        item = memorial_items.get(memorial_id, {})
        option: dict[str, Any] = next((row for row in item.get("options", []) or [] if row.get("id") == option_id), {})
        base = {
            "category": "memorial",
            "text": f"{item.get('title', memorial_id)}：{option.get('label', option_id)}",
            "source": "memorial",
            "memorial_id": memorial_id,
            "option_id": option_id,
        }
        if any(row["memorial_id"] == memorial_id for row in accepted_memorials):
            clauses.append({**base, "status": "rejected", "op": None, "reason": "同一奏疏只能采纳一个选项", "code": "DUPLICATE_MEMORIAL"})
            continue
        try:
            operations = adopted_ops(scenario, state, memorial_id, option_id)
        except MemorialError as exc:
            clauses.append({**base, "status": "rejected", "op": None, "reason": exc.message, "code": exc.code})
            continue
        accepted_memorials.append({"memorial_id": memorial_id, "option_id": option_id})
        if not operations:
            clauses.append({**base, "status": "narrative_only", "op": None, "reason": "此选项不改变状态", "code": None})
            continue
        add_clauses(tools.propose_ops(operations), base)
    for client_op in client_ops or []:
        add_clauses(tools.propose_ops([client_op]), {"category": "client", "text": "", "source": "client"})

    for index, (category, text) in enumerate(sections.items()):
        if index >= 12:
            clauses.append({
                "category": category,
                "text": text,
                "status": "needs_clarification",
                "ops": [],
                "reason": "单次诏令处理达到 12 步上限。",
            })
            continue
        if not text.strip():
            continue
        if provider is None:
            clauses.append({
                "category": category,
                "text": text,
                "status": "narrative_only",
                "ops": [],
                "reason": "客户端模式下诏令文本不经解释，仅作记录；请以 ops 提交可执行条款。",
                "source": "text",
            })
            continue
        prompt = (
            render_prompt(
                "edict_interpreter",
                {
                    "entity_context": _entity_context(scenario),
                    "state_summary": render_digest(tools.state, seed, scenario=scenario),
                    "category": category,
                    "text": text,
                },
                scenario.get("root"),
            )
        )
        messages = [
            {"role": "system", "content": "将自然语言诏令拆分为类型化操作；不确定时不猜测。"},
            {"role": "user", "content": prompt},
        ]
        try:
            result = await guarded_chat(provider, messages, TOOL_SCHEMA, deadline, abort_event=abort_event)
        except ToolCallingUnsupported:
            fallback_messages = [
                {
                    "role": "system",
                    "content": '仅返回 JSON 对象，格式为 {"ops":[...]}；只提出类型化操作，不解释。',
                },
                {"role": "user", "content": prompt},
            ]
            result = await guarded_chat(
                provider,
                fallback_messages,
                None,
                deadline,
                abort_event=abort_event,
                response_format={"type": "json_object"},
            )
        proposed: list[dict[str, Any]] = []
        for call in result.tool_calls:
            if call.name in {"propose_ops", "submit_ops"}:
                receipt = tools.propose_ops(call.arguments.get("ops", []))
                proposed.extend(receipt)
        if not result.tool_calls and result.content:
            try:
                operations = _parse_ops_json(result.content)
                proposed.extend(tools.propose_ops(operations))
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                proposed.append({
                    "op": {},
                    "status": "rejected",
                    "reason": f"JSON 模式输出无效：{exc}",
                    "code": "INVALID_TARGET",
                })
        if not proposed:
            clauses.append({"category": category, "text": text, "status": "narrative_only", "ops": [], "reason": "", "source": "text"})
            continue
        add_clauses(proposed, {"category": category, "text": text, "source": "text"})
    return {
        "id": f"edict-{uuid.uuid4().hex[:12]}",
        "turn": state.turn,
        "adopted": accepted_memorials,
        "clauses": clauses,
        "execution_rate": (
            sum(clause["status"] == "applied" for clause in clauses) / len(clauses) if clauses else 1
        ),
        "confirmed": False,
    }


def _parse_ops_json(content: str) -> list[dict[str, Any]]:
    stripped = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content, flags=re.IGNORECASE)
    payload = json.loads(stripped)
    if isinstance(payload, dict):
        operations = payload.get("ops", [])
    else:
        operations = payload
    if not isinstance(operations, list) or not all(isinstance(item, dict) for item in operations):
        raise ValueError("需要 ops 对象数组")
    return operations


async def narrator(
    report: TurnReport, provider: LLMProvider, scenario: dict[str, Any] | None = None
) -> str:
    facts = [fact.text for fact in report.facts]
    if not facts:
        return "本回合暂无可记录的确定变化。"
    prompt = render_prompt("narrator", {"facts": facts}, (scenario or {}).get("root"))
    result = await provider.chat([
        {"role": "system", "content": "只根据已应用事实写简短纪要，不添加事实、数字或推断。"},
        {"role": "user", "content": prompt},
    ])
    if any(not _number_allowed(number, report) for number in _NUMBER.findall(result.content)):
        return "；".join(facts)
    return result.content if result.content else "；".join(facts)


_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")


def _number_allowed(text: str, report: TurnReport) -> bool:
    """按数值比较纪要中的数字：100 与 100.0 等价，事实文本中的数字和前后差值也视为已知。"""
    allowed: set[float] = set()
    for fact in report.facts:
        allowed.update(float(value) for value in _NUMBER.findall(fact.text))
        values = [value for value in (fact.before, fact.after) if isinstance(value, (int, float)) and not isinstance(value, bool)]
        allowed.update(float(value) for value in values)
        if len(values) == 2:
            delta = float(values[1]) - float(values[0])
            allowed.update({delta, abs(delta)})
    number = float(text)
    return any(abs(number - value) < 1e-6 for value in allowed)


def chat_availability(character_id: str, state: WorldState, afterlife_chat: bool = False) -> dict[str, str] | None:
    """人物不能应答时返回 {status, content}；可以应答时返回 None。"""
    character = state.characters.get(character_id)
    if character is None:
        return {"status": "not_found", "content": "未找到此人物。"}
    if character.status in {"dead", "imprisoned"} and not afterlife_chat:
        return {"status": "refused", "content": "此人当前无法应答。"}
    return None


def minister_chat_messages(
    character_id: str,
    state: WorldState,
    user_text: str,
    scenario: dict[str, Any] | None = None,
    seed: int = 0,
    report: TurnReport | dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """召对提示词：人设 + 该人物可知的 StateDigest + 该人物记忆。"""
    character = state.characters[character_id]
    digest = render_digest(state, seed, character_id, report, scenario)
    return [
        {
            "role": "system",
            "content": render_prompt(
                "minister_chat",
                {
                    "name": character.name,
                    "persona": character.persona,
                    "speech_style": character.speech_style,
                    "calendar": state.calendar.label,
                    "turn": state.turn,
                    "resources": "；".join(resource_display(state, key, seed) for key in state.resources),
                    "digest": digest,
                    "memories": character.memories[-12:],
                    "user_text": user_text,
                },
                (scenario or {}).get("root"),
            ),
        },
        {"role": "user", "content": user_text},
    ]


async def minister_chat(
    character_id: str,
    state: WorldState,
    provider: LLMProvider,
    afterlife_chat: bool = False,
    user_text: str = "请就当前朝局谨慎回应。",
    scenario: dict[str, Any] | None = None,
    seed: int = 0,
    report: TurnReport | dict[str, Any] | None = None,
) -> dict[str, Any]:
    refusal = chat_availability(character_id, state, afterlife_chat)
    if refusal is not None:
        return refusal
    result = await provider.chat(minister_chat_messages(character_id, state, user_text, scenario, seed, report))
    return {"status": "ok", "content": result.content}


async def chronicler(
    entries: list[str], provider: LLMProvider, scenario: dict[str, Any] | None = None
) -> str:
    result = await provider.chat([
        {"role": "system", "content": render_prompt(
            "chronicler", {"entries": "\n".join(entries)}, (scenario or {}).get("root")
        )},
        {"role": "user", "content": "\n".join(entries)},
    ])
    return result.content


def _entity_context(scenario: dict[str, Any]) -> str:
    resources = [row.get("id") for row in scenario.get("scenario", {}).get("resources", [])]
    people = [f"{row.get('name')}={row.get('id')}" for row in scenario.get("characters", [])]
    offices = [f"{row.get('name')}={row.get('id')}" for row in scenario.get("scenario", {}).get("offices", [])]
    return f"资源 {resources}；人物 {people}；官职 {offices}"


def _delay_probability(formula: dict[str, Any], state: WorldState) -> float:
    authority_id = formula.get("authority_resource", "authority")
    corruption_id = formula.get("corruption_resource", "corruption")
    authority = state.resources.get(authority_id)
    corruption = state.resources.get(corruption_id)
    authority_value = authority.amount if authority else 0
    corruption_value = corruption.amount if corruption else 0
    base = float(formula.get("base_delay", 0))
    authority_factor = float(formula.get("authority_factor", 0))
    corruption_factor = float(formula.get("corruption_factor", 0))
    return min(1.0, max(0.0, base + corruption_value * corruption_factor - authority_value * authority_factor))
