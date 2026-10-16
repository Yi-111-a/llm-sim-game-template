from __future__ import annotations

import asyncio
import json
import random
import re
import uuid
from typing import Any

from simcore.agent.loop import guarded_chat
from simcore.agent.offline import chronicle_numbers, is_offline, offline_chronicle, offline_minister_reply
from simcore.agent.tools import PLAYER_TIER, AgentTools, cascade_delays
from simcore.domain.cards import CardError, expand_card
from simcore.domain.courier import courier_delay_reason, far_target
from simcore.domain.difficulty import delay_factor
from simcore.domain.digest import digest_numbers, render_digest
from simcore.domain.memorials import MemorialError, adopted_ops
from simcore.domain.military import autonomy_preview
from simcore.domain.models import TurnReport, WorldState
from simcore.domain.rules import audience_mode
from simcore.domain.visibility import format_number, resource_display
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


OFFLINE_TEXT_REASON = "未接模型：御笔只记入史册，不生效力；请用奏疏批答或政令卡"


async def interpret_edict(
    sections: dict[str, str],
    scenario: dict[str, Any],
    state: WorldState,
    provider: LLMProvider | None,
    seed: int,
    abort_event: asyncio.Event | None = None,
    adopted: list[dict[str, str]] | None = None,
    client_ops: list[dict[str, Any]] | None = None,
    cards: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """生成诏书草案回执（不改变状态）。

    处理顺序：采纳的奏疏选项（source=memorial）→ 客户端模型直接给出的 ops（source=client）
    → 政令卡展开（source=card，玩家层）→ 各类别诏令文本经 LLM 解释（source=text）。
    作用于远地（驿程超过阈值）的条款整组延至下季（delay_kind=courier，「驿程遥远，令达于下季」），与有司稽迟
    （delay_kind=bureaucratic）分开显示；军令条款附将领自主风险等级（autonomy_risk）。
    provider 为离线 mock 时，文本一律 narrative_only（「未接模型：御笔只记入史册……」）。所有条款按顺序累积预演，并按同一种子 RNG 判定延期：
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

    delay = delay_explanation(execution, state)

    prior = {"state": state}  # 本组之前各组条款施行后的状态（风险预览用）

    def add_clauses(receipt: list[dict[str, Any]], base: dict[str, Any]) -> None:
        """一组条款（一个奏疏选项 / 一条客户端 op / 一段诏令文本）只掷一次延期骰，组内 ops 同进同退。"""
        nonlocal group_counter
        group_counter += 1
        # 有司稽迟只及于朝廷主动施行之事；选项写 immediate: true（「依旧」「不问」之类只是放任的后果）不掷延期骰
        applied = any(item["status"] == "applied" for item in receipt)
        roll = rng.random() if applied else 1.0  # 与以前同样只在有可行条款时取随机数，种子重放不变
        delayed = applied and not base.get("immediate") and roll < delay["probability"]
        far = far_target(state, [item["op"] for item in receipt if item["status"] == "applied" and isinstance(item.get("op"), dict)])
        for item in receipt:
            status = item["status"]
            row = {
                **base,
                "group": group_counter,
                "status": status,
                "op": item.get("op"),
                "reason": item.get("reason", ""),
                "code": item.get("code"),
            }
            if status == "applied" and isinstance(item.get("op"), dict):
                # 按本诏书前序各组条款施行后的状态评估（同一诏书先易帅、后下军令时，风险按新帅计）
                risk = autonomy_preview(prior["state"], item["op"])
                if risk is not None:
                    row.update({"autonomy_risk": risk["level"], "autonomy_note": risk["text"]})
            if status == "applied" and far is not None:
                row.update({
                    "status": "delayed",
                    "delay_kind": "courier",
                    "delay_reason": courier_delay_reason(state, far[0], far[1]),
                    "due_turn": state.turn + 1,
                })
            elif status == "applied" and delayed:
                row.update({"status": "delayed", "delay_kind": "bureaucratic", "delay_reason": delay["reason"], "due_turn": state.turn + 1})
            clauses.append(row)
        prior["state"] = tools.state

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
            "label": str(item.get("title", memorial_id)),
            **({"immediate": True} if option.get("immediate") else {}),
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
        add_clauses(tools.propose_ops([client_op], tier=PLAYER_TIER), {"category": "client", "text": "", "source": "client", "label": "诏令"})
    used_cards: list[str] = []
    for entry in cards or []:
        entry = entry if isinstance(entry, dict) else {}
        card_id = str(entry.get("card_id") or entry.get("id") or "")
        base = {"category": "card", "text": f"政令卡 {card_id}", "source": "card", "card_id": card_id, "label": "政令卡"}
        try:
            card, card_ops, summary = expand_card(tools.state, scenario, card_id, entry.get("params") if isinstance(entry.get("params"), dict) else {})
        except CardError as exc:
            clauses.append({**base, "status": "rejected", "op": None, "reason": exc.message, "code": exc.code})
            continue
        title = str(card.get("title", card_id))
        base.update({
            "category": str(card.get("category", "card")),
            "text": f"{title}：{summary}" if summary else title,
            "card_title": title,
            "label": f"政令卡「{title}」",
            "cost_hint": str(card.get("cost_hint", "")),
        })
        if int(card.get("cooldown") or 0) and card_id in used_cards:
            clauses.append({**base, "status": "rejected", "op": None, "reason": f"「{title}」有冷却，一道诏书只能用一次", "code": "CARD_COOLDOWN"})
            continue
        used_cards.append(card_id)
        add_clauses(tools.propose_ops(card_ops, tier=PLAYER_TIER), base)

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
                "reason": "客户端模式下诏令文本不经解释，仅作记录；请以 ops 或政令卡（cards）提交可执行条款。",
                "source": "text",
            })
            continue
        if is_offline(provider):
            clauses.append({
                "category": category,
                "text": text,
                "status": "narrative_only",
                "ops": [],
                "reason": OFFLINE_TEXT_REASON,
                "code": "OFFLINE_TEXT",
                "source": "text",
            })
            continue
        prompt = (
            render_prompt(
                "edict_interpreter",
                {
                    "op_guide": op_guide(tools.state),
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
                    "content": '仅返回 JSON 对象，格式为 {"ops":[...]}；每个元素必须有 "op" 字段，只提出类型化操作，不解释。',
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
        notes: list[str] = []
        questions: list[str] = []
        for call in result.tool_calls:
            if call.name in {"propose_ops", "submit_ops"}:
                real = split_model_ops(call.arguments.get("ops", []), notes, questions)
                proposed.extend(tools.propose_ops(real, tier=PLAYER_TIER))
        if not result.tool_calls and result.content:
            try:
                operations = split_model_ops(_parse_ops_json(result.content), notes, questions)
                proposed.extend(tools.propose_ops(operations, tier=PLAYER_TIER))
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                proposed.append({
                    "op": {},
                    "status": "rejected",
                    "reason": f"JSON 模式输出无效：{exc}",
                    "code": "INVALID_TARGET",
                })
        if questions:
            clauses.append({"category": category, "text": text, "status": "needs_clarification", "ops": [], "reason": "；".join(questions), "source": "text"})
        if not proposed:
            if not questions:
                clauses.append({"category": category, "text": text, "status": "narrative_only", "ops": [], "reason": "；".join(notes), "source": "text"})
            continue
        add_clauses(proposed, {"category": category, "text": text, "source": "text", "label": f"诏令（{category}）"})
    cascade_delays(state, clauses)
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


NARRATIVE_OPS = frozenset({"narrative", "note", "narrative_only"})
CLARIFY_OPS = frozenset({"clarify", "request_clarification", "needs_clarification", "ask_clarification"})


def split_model_ops(ops: Any, notes: list[str], questions: list[str]) -> list[dict[str, Any]]:
    """整理模型给出的操作：写成 {"type": ...} 而缺 "op" 的按 op 名读；「仅叙事」「请澄清」两类不是操作，
    分别记入 notes / questions（成为 narrative_only / needs_clarification 条款），其余原样交规则层校验。"""
    real: list[dict[str, Any]] = []
    for raw in ops if isinstance(ops, list) else []:
        if not isinstance(raw, dict):
            real.append(raw)
            continue
        row = dict(raw)
        if "op" not in row and isinstance(row.get("type"), str):
            row["op"] = row.pop("type")
        name = str(row.get("op") or "")
        if name in NARRATIVE_OPS:
            notes.append(str(row.get("text") or row.get("note") or ""))
        elif name in CLARIFY_OPS:
            questions.append(str(row.get("question") or row.get("text") or "此条须补充说明"))
        else:
            real.append(row)
    return real


def op_guide(state: WorldState) -> str:
    """解释诏令时给模型看的可用操作（玩家层）与示例，一行一个。"""
    from simcore.domain.permissions import player_ops
    from simcore.domain.reference import op_examples

    examples = op_examples(state, None)
    return "\n".join(json.dumps(examples.get(name) or {"op": name}, ensure_ascii=False) for name in sorted(player_ops(state)))


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
    report: TurnReport,
    provider: LLMProvider,
    scenario: dict[str, Any] | None = None,
    *,
    state: WorldState | None = None,
    seed: int = 0,
    context: dict[str, Any] | None = None,
) -> str:
    """本季纪事。离线 provider（mock）且给了 state 时用模板史笔（offline_chronicle），否则调用模型；
    两种来源都过数字核对：出现报告之外的数字即退回事实拼接。"""
    facts = [fact.text for fact in report.facts if not fact.internal]
    if is_offline(provider) and state is not None:
        content = offline_chronicle(report, state, scenario, seed, context)
    else:
        if not facts:
            return "本回合暂无可记录的确定变化。"
        prompt = render_prompt("narrator", {"facts": facts}, (scenario or {}).get("root"))
        result = await provider.chat([
            {"role": "system", "content": "只根据已应用事实写简短纪要，不添加事实、数字或推断。"},
            {"role": "user", "content": prompt},
        ])
        content = result.content
    if any(not _number_allowed(number, report) for number in _NUMBER.findall(content)):
        return "；".join(facts)
    return content if content else ("；".join(facts) or "本回合暂无可记录的确定变化。")


_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])-?\d+(?:\.\d+)?")


def _number_allowed(text: str, report: TurnReport) -> bool:
    """按数值比较纪要中的数字：100 与 100.0 等价；事实文本中的数字、数值事实的前后值与差值、
    账目行金额及其按资源的合计（含一位小数的取整）都视为已知。"""
    number = float(text)
    return any(abs(number - value) < 1e-6 for value in chronicle_numbers(report))


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
    """召对提示词：人设 + 该人物可知的 StateDigest + 该人物记忆 + 召对方式（面见 / 书面奏对 / 遣使问对）。"""
    character = state.characters[character_id]
    digest = render_digest(state, seed, character_id, report, scenario)
    mode = audience_mode(state, character_id)
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
                    "audience_mode": mode["label"],
                },
                (scenario or {}).get("root"),
            )
            + f"\n召对方式：{mode['label']}。{mode['note']}" + AUDIENCE_VOICE[mode["mode"]],
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
    if is_offline(provider):
        return {"status": "ok", "content": offline_minister_text(character_id, state, user_text, scenario, seed, report)}
    result = await provider.chat(minister_chat_messages(character_id, state, user_text, scenario, seed, report))
    return {"status": "ok", "content": result.content}


def offline_minister_text(
    character_id: str,
    state: WorldState,
    user_text: str,
    scenario: dict[str, Any] | None = None,
    seed: int = 0,
    report: TurnReport | dict[str, Any] | None = None,
) -> str:
    """离线召对答话（mock provider）：模板 + 该人物可知的摘要；数字只取自摘要与问话。"""
    digest = render_digest(state, seed, character_id, report, scenario)
    text = offline_minister_reply(character_id, state, user_text, scenario, seed, digest)
    allowed = digest_numbers(digest) | {float(item) for item in _NUMBER.findall(user_text)}
    if any(float(item) not in allowed for item in _NUMBER.findall(text)):
        text = offline_minister_reply(character_id, state, "", scenario, seed, "")
    mode = audience_mode(state, character_id)
    if mode["mode"] == "written":
        where = state.regions.get(state.characters[character_id].region)
        text = f"〔书面奏对·自{where.name if where else '外镇'}驿递〕{text}"
    elif mode["mode"] == "envoy":
        text = f"〔遣使问对·途中回奏〕{text}"
    return text


AUDIENCE_VOICE = {
    "in_person": "可写当面应对。",
    "written": "请以书面奏疏的口吻作答（如「臣某谨奏」「伏惟圣鉴」），不得写成当面应对、入殿或叩见的情景，也不得说自己已奉召入京。",
    "envoy": "此人在途中，由使者赍问，请以途中书面回奏的口吻作答，不得写成当面应对。",
}


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
    regions = [f"{row.get('name')}={row.get('id')}" for row in scenario.get("regions", []) if isinstance(row, dict)]
    armies = [f"{row.get('name')}={row.get('id')}" for row in scenario.get("armies", []) if isinstance(row, dict)]
    return f"资源 {resources}；人物 {people}；官职 {offices}；地区 {regions}；军队 {armies}"


def _delay_probability(formula: dict[str, Any], state: WorldState) -> float:
    return float(delay_explanation(formula, state)["probability"])


def delay_explanation(formula: dict[str, Any], state: WorldState) -> dict[str, Any]:
    """诏令延期的概率与原因（由 formulas.execution 的各项推出）。

    概率 = (base_delay + 积弊 × corruption_factor − 君威 × authority_factor) × 难度 delay_scale，截到 [0, 1]。
    原因取贡献最大的正项：积弊项 →「吏治积弊高（55），有司稽迟」；基础项 →「文书往返需时，有司未及施行」；
    难度系数 > 1 时补「时局多事」；君威项把概率压低时注明「君威尚能督责」。
    """
    authority_id = formula.get("authority_resource", "authority")
    corruption_id = formula.get("corruption_resource", "corruption")
    authority = state.resources.get(authority_id)
    corruption = state.resources.get(corruption_id)
    authority_value = authority.amount if authority else 0
    corruption_value = corruption.amount if corruption else 0
    base = float(formula.get("base_delay", 0))
    authority_term = authority_value * float(formula.get("authority_factor", 0))
    corruption_term = corruption_value * float(formula.get("corruption_factor", 0))
    scale = delay_factor(state)
    probability = min(1.0, max(0.0, (base + corruption_term - authority_term) * scale))
    parts: list[str] = []
    if corruption is not None and corruption_term > 0 and corruption_term >= base:
        parts.append(f"{corruption.name}高（{format_number(corruption_value)}），有司稽迟")
    else:
        parts.append("文书往返需时，有司未及施行")
        if corruption is not None and corruption_term > 0:
            parts.append(f"{corruption.name}（{format_number(corruption_value)}）亦有拖累")
    if scale > 1:
        parts.append("时局多事，政令更易稽迟")
    if authority is not None and authority_term > 0:
        parts.append(f"{authority.name}（{format_number(authority_value)}）尚能督责")
    terms = {"base": round(base, 4), "corruption": round(corruption_term, 4), "authority": round(-authority_term, 4), "scale": scale}
    return {"probability": probability, "reason": "；".join(parts) + f"（本季延期概率约 {round(probability * 100)}%）", "terms": terms}
