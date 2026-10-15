"""隐藏数值与公开视图。API 的 /view 与 MCP 的全部工具只能使用本模块的输出。"""

from __future__ import annotations

import math
import os
import random
from typing import Any

from simcore.domain.crisis import band_for, evaluate_terms, expression_names, formula_values, interval_evaluate
from simcore.domain.models import TurnReport, WorldState, is_internal_fact

DEFAULT_HIDDEN_WIDTH = 10.0


INTEL_PREFIX = "intel:"
INTEL_SOURCES_PREFIX = "_internal:intel_sources:"


def intel_precision(state: WorldState, resource_id: str) -> dict[str, Any]:
    """隐藏资源风闻区间的精度说明。

    基础半宽 = estimate_width × (1 + 积弊/100)。每条情报（flag intel:<resource> 或 intel:<resource>:<来源>，
    以及 set_flag 时记下的不同来源）使半宽减半，但不低于精度上限 floor：settings.intel_min_width
    （数值，或 {resource: 数值}）；未设时 floor = 基础半宽的一半，即情报不叠加，只有第一条生效。
    返回 {half_width, base_half_width, floor, sources, narrowing, applied, capped, display}。
    """
    resource = state.resources[resource_id]
    corruption_id = str(state.rules.get("corruption_resource", "corruption"))
    corruption = state.resources[corruption_id].amount if corruption_id in state.resources else 0.0
    width = resource.estimate_width if resource.estimate_width > 0 else DEFAULT_HIDDEN_WIDTH
    base = width * (1 + max(0.0, corruption) / 100)
    prefix = f"{INTEL_PREFIX}{resource_id}"
    markers = [key for key, value in state.flags.items() if (key == prefix or key.startswith(prefix + ":")) and value not in (None, False)]
    recorded = state.flags.get(f"{INTEL_SOURCES_PREFIX}{resource_id}")
    sources = [str(item) for item in recorded] if isinstance(recorded, list) and markers else []
    narrowing = max(len(markers), len(sources))
    configured = state.rules.get("intel_min_width")
    if isinstance(configured, dict):
        configured = configured.get(resource_id)
    try:
        floor = float(configured) if configured is not None else base / 2
    except (TypeError, ValueError):
        floor = base / 2
    floor = min(base, max(0.0, floor))
    half = base
    applied = 0
    for _ in range(narrowing):
        if half / 2 < floor - 1e-9:
            break
        half /= 2
        applied += 1
    if narrowing > applied and half > floor + 1e-9:
        half = floor  # 剩余情报把区间收到上限为止
    capped = narrowing > applied
    return {
        "half_width": round(half, 2),
        "base_half_width": round(base, 2),
        "floor": round(floor, 2),
        "sources": sources or (["密报"] if narrowing else []),
        "narrowing": narrowing,
        "applied": applied,
        "capped": capped,
        "display": f"±{format_number(half)}",
    }


def precision_text(state: WorldState, resource_id: str) -> str:
    """简报用一句话：「精度 ±4.6（基础 ±9.2）；情报：密结颜杲卿、中使实奏；已达精度上限 ±4.6，再得情报不再收窄」。"""
    info = intel_precision(state, resource_id)
    text = f"精度 {info['display']}"
    if info["narrowing"]:
        text += f"（基础 ±{format_number(info['base_half_width'])}）；情报：{'、'.join(info['sources'])}"
    else:
        text += "；尚无情报来源（积弊越高区间越宽）"
    if info["capped"]:
        text += f"；已达精度上限 ±{format_number(info['floor'])}，再得情报不再收窄"
    elif info["narrowing"]:
        text += f"；精度上限 ±{format_number(info['floor'])}"
    return text


def estimate_interval(state: WorldState, resource_id: str, seed: int) -> tuple[float, float]:
    """隐藏资源的「风闻区间」。

    半宽见 intel_precision；区间中心带 ±半宽/2 的种子噪声，同一回合内稳定；真值始终落在区间内。
    estimate_width 未设（≤0）时按 10 处理，避免隐藏资源意外以零宽区间泄露。
    """
    resource = state.resources[resource_id]
    half = float(intel_precision(state, resource_id)["half_width"])
    rng = random.Random(f"estimate:{seed}:{state.turn}:{resource_id}")
    center = resource.amount + rng.uniform(-half / 2, half / 2)
    low, high = center - half, center + half
    if resource.minimum is not None:
        low = max(resource.minimum, low)
    if resource.maximum is not None:
        high = min(resource.maximum, high)
    return (round(low, 1), round(high, 1))


def hidden_resources(state: WorldState) -> set[str]:
    return {key for key, row in state.resources.items() if row.hidden}


def hidden_traits(state: WorldState) -> set[str]:
    return {str(item) for item in state.rules.get("hidden_traits", []) or []}


def public_resource(state: WorldState, resource_id: str, seed: int) -> dict[str, Any]:
    row = state.resources[resource_id]
    if not row.hidden:
        return {**row.model_dump(mode="json"), "display": resource_display(state, resource_id, seed)}
    return {
        "id": row.id,
        "name": row.name,
        "hidden": True,
        "estimate": list(estimate_interval(state, resource_id, seed)),
        "unit": row.unit,
        "minimum": row.minimum,
        "maximum": row.maximum,
        "display": resource_display(state, resource_id, seed),
        "precision": intel_precision(state, resource_id),
    }


def public_view(state: WorldState, seed: int) -> dict[str, Any]:
    """返回可给玩家与外部模型看的状态：隐藏资源只给区间，隐藏心态与内部字段被移除。"""
    data = state.model_dump(mode="json")
    data.pop("rng_state", None)
    data["resources"] = {key: public_resource(state, key, seed) for key in state.resources}
    data["flags"] = {key: value for key, value in data["flags"].items() if not str(key).startswith("_internal:")}
    data["flag_turns"] = {key: value for key, value in data["flag_turns"].items() if not str(key).startswith("_internal:")}
    secret_traits = hidden_traits(state)
    if secret_traits:
        for character in data["characters"].values():
            character["traits"] = {key: value for key, value in character["traits"].items() if key not in secret_traits}
    return data


def resource_display(state: WorldState, resource_id: str, seed: int) -> str:
    """用于 StateDigest 与简报的单行文字：明值或「约 lo–hi（风闻）」。"""
    row = state.resources[resource_id]
    if row.hidden:
        low, high = estimate_interval(state, resource_id, seed)
        return f"{row.name} {format_range(low, high)}{row.unit}（风闻）"
    return f"{row.name} {format_number(row.amount)}{row.unit}"


def public_crisis(
    crisis: dict[str, Any] | None,
    state: WorldState,
    seed: int,
    terms: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """危机计公开版：{title, resource, value_or_estimate, band, terms:[{id,label,value}]}。

    分项公式引用隐藏资源时，该分项只给区间（以隐藏资源的风闻区间代入求得）。
    """
    if not crisis:
        return {}
    resource_id = crisis.get("resource")
    resource = state.resources.get(resource_id) if resource_id else None
    value: Any = None
    center: float | None = None
    if resource is not None:
        if resource.hidden:
            low, high = estimate_interval(state, resource.id, seed)
            value = [low, high]
            center = (low + high) / 2
        else:
            value = round(resource.amount, 2)
            center = resource.amount
    hidden = hidden_resources(state)
    intervals = {key: estimate_interval(state, key, seed) for key in hidden}
    current_values = formula_values(state)
    exact_terms = {row.get("id"): row for row in (terms if terms is not None else evaluate_terms(crisis, current_values))}
    public_terms: list[dict[str, Any]] = []
    for term in crisis.get("terms", []) or []:
        expression = term.get("expr", "0")
        uses_hidden = bool(expression_names(expression) & hidden)
        if uses_hidden:
            term_value: Any = interval_evaluate(str(expression), current_values, intervals)
            term_value = list(term_value) if term_value is not None else None
        else:
            term_value = exact_terms.get(term.get("id"), {}).get("value")
        public_terms.append({
            "id": term.get("id"),
            "label": term.get("label", term.get("id")),
            "value": term_value,
            "estimated": uses_hidden,
            "display": format_value(term_value, signed=True),
        })
    return {
        "title": crisis.get("title", resource.name if resource else ""),
        "resource": resource_id,
        "value_or_estimate": value,
        "display": format_value(value),
        "band": band_for(center, crisis.get("bands", []) or []),
        "bands": crisis.get("bands", []) or [],
        "terms": public_terms,
    }


def public_report(
    report: TurnReport | dict[str, Any] | None,
    state: WorldState,
    seed: int,
    crisis: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """把 TurnReport 中涉及隐藏资源/隐藏心态的数值抹去，危机分项换成公开版。

    内部事实（旗标、计数、人物记忆等，见 is_internal_fact）不进入 facts；仅在 SIM_DEBUG=1 时另列于 internal_facts。
    每条事实与账目行附 display（玩家可读的一行文字，数字至多一位小数）；JSON 数值字段保持原始浮点。
    """
    if report is None:
        return None
    data = report.model_dump(mode="json") if isinstance(report, TurnReport) else dict(report)
    hidden = hidden_resources(state)
    secret_traits = hidden_traits(state)
    facts = []
    internal = []
    relation_seen: set[tuple[Any, ...]] = set()
    for fact in data.get("facts", []):
        fact = dict(fact)
        if is_internal_fact(fact):
            internal.append(fact)
            continue
        if _duplicate_relation(fact, relation_seen):
            continue
        if fact.get("kind") == "resource" and fact.get("entity") in hidden:
            name = state.resources[fact["entity"]].name
            cause = f"{fact['cause']}；" if fact.get("cause") else ""
            fact.update({"text": f"{name}有所变动（{cause}数值不明）", "before": None, "after": None})
        if fact.get("kind") == "trait" and _secret_trait(fact, secret_traits):
            fact.update({"text": "某人心态有所变化", "before": None, "after": None, "key": None, "cause": None})
        fact["display"] = fact_line(fact)
        facts.append(fact)
    data["facts"] = facts
    if os.environ.get("SIM_DEBUG") == "1":
        data["internal_facts"] = internal
    lines = []
    for line in data.get("ledger_lines", []):
        line = dict(line)
        if line.get("resource") in hidden:
            line["amount"] = None
            line["reason"] = "数值不明"
        amount = line.get("amount")
        line["display"] = f"{line.get('name', '')} {'数值不明' if amount is None else format_number(amount, signed=True)}"
        lines.append(line)
    data["ledger_lines"] = lines
    events, internal_events = public_events(data)
    data["events"] = events
    data["featured_event"] = events[0] if events else None
    data["events_fired"] = [row["id"] for row in events if not row.get("outcome")]
    if os.environ.get("SIM_DEBUG") == "1":
        data["internal_events"] = internal_events
    data["battles"] = [
        {**battle, "text": battle_text(battle, state)} if isinstance(battle, dict) else battle
        for battle in data.get("battles", []) or []
    ]
    if data.get("crisis"):
        snapshot = data["crisis"].get("public")
        if isinstance(snapshot, dict):
            data["crisis"] = snapshot  # 结算当时计算的公开快照（end_turn / 季报 / 简报同一份数字）
        else:
            data["crisis"] = public_crisis(crisis, state, seed, data["crisis"].get("terms")) if crisis else {}
    data["reconciliation"] = public_reconciliation(data.get("reconciliation"), state)
    return data


RECONCILIATION_STEPS = (
    ("opening", "期初"),
    ("edicts", "诏令"),
    ("delayed", "延期执行"),
    ("regular", "经常收支"),
    ("events", "事件/选择"),
    ("closing", "期末"),
)


def public_reconciliation(raw: Any, state: WorldState) -> list[dict[str, Any]]:
    """公开资源的分段对账：期初 → 诏令 → 延期执行 → 经常收支 → 事件/选择 → 期末（隐藏资源不列）。

    数值字段保留原始浮点（相加等于期末，误差 < 1e-6）；shown / display 按一位小数取整，并把取整余差归入变动最大的一段，
    使显示的各段之和与期末完全一致。
    """
    if not isinstance(raw, dict):
        return []
    hidden = hidden_resources(state)
    rows: list[dict[str, Any]] = []
    for key, values in raw.items():
        if key in hidden or key not in state.resources or not isinstance(values, dict) or "opening" not in values:
            continue
        resource = state.resources[key]
        numbers = {step: float(values.get(step, 0.0) or 0.0) for step, _ in RECONCILIATION_STEPS}
        shown = {step: round(numbers[step], 1) for step, _ in RECONCILIATION_STEPS}
        deltas = [step for step, _ in RECONCILIATION_STEPS[1:-1]]
        residual = round(shown["closing"] - shown["opening"] - sum(shown[step] for step in deltas), 1)
        if residual:
            biggest = max(deltas, key=lambda step: abs(numbers[step]))
            shown[biggest] = round(shown[biggest] + residual, 1)
        parts = [
            f"{label} {_signed(shown[step]) if step not in {'opening', 'closing'} else format_number(shown[step])}"
            for step, label in RECONCILIATION_STEPS
        ]
        unit = f"（{resource.unit}）" if resource.unit else ""
        rows.append({
            "resource": key,
            "name": resource.name,
            "unit": resource.unit,
            **numbers,
            "shown": shown,
            "display": f"{resource.name}{unit}：" + " → ".join(parts),
        })
    return rows


def _signed(value: float) -> str:
    """对账的变动量：+5、−40、0（全角减号，与事实文本一致）。"""
    text = format_number(abs(value))
    return "0" if text == "0" else (f"−{text}" if value < 0 else f"+{text}")


def _duplicate_relation(fact: dict[str, Any], seen: set[tuple[Any, ...]]) -> bool:
    """对称的关系变化（A→B 与 B→A 同幅、同因）只留第一条。"""
    if fact.get("kind") != "relation":
        return False
    before, after = fact.get("before"), fact.get("after")
    numeric = all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (before, after))
    delta = round(float(after) - float(before), 3) if numeric else None
    key = (frozenset({str(fact.get("entity")), str(fact.get("key"))}), delta, fact.get("cause"))
    if key in seen:
        return True
    seen.add(key)
    return False


def _secret_trait(fact: dict[str, Any], secret_traits: set[str]) -> bool:
    key = fact.get("key")
    if key is not None:
        return str(key) in secret_traits
    return any(str(fact.get("text", "")).endswith(f"：{trait}") for trait in secret_traits)  # 旧存档的事实文本


def format_number(value: float | int, signed: bool = False) -> str:
    """玩家可见的数字：至多一位小数，去掉末尾的 0（235.5、60、-3）；signed=True 时正数带 +。"""
    rounded = round(float(value), 1)
    if rounded == 0:
        rounded = 0.0  # 避免 -0
    text = str(int(rounded)) if rounded == int(rounded) else f"{rounded:.1f}"
    return f"+{text}" if signed and rounded > 0 else text


def format_range(low: float, high: float, signed: bool = False) -> str:
    """区间的紧凑显示。

    不带符号（资源、危机值）：「约 55–60」；区间宽 ≥ 2 时两端向外取整（真值仍在区间内），否则至多一位小数；
    含负数时用「～」分隔，避免与负号相混。带符号（危机分项）：「+0.3～+0.9」。两端显示相同时只给一个值。
    """
    low, high = (float(low), float(high)) if float(low) <= float(high) else (float(high), float(low))
    if not signed and high - low >= 2:
        low, high = float(math.floor(low + 1e-9)), float(math.ceil(high - 1e-9))
    left, right = format_number(low, signed=signed), format_number(high, signed=signed)
    if signed:
        return left if left == right else f"{left}～{right}"
    if left == right:
        return f"约 {left}"
    separator = "～" if low < 0 else "–"
    return f"约 {left}{separator}{right}"


def format_value(value: Any, signed: bool = False) -> str:
    """数字或风闻区间 [lo, hi] 的显示文字（见 format_range）；signed=True 时正数带 +；其他值原样转字符串。"""
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(
        isinstance(item, (int, float)) and not isinstance(item, bool) for item in value
    ):
        return format_range(value[0], value[1], signed=signed)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return format_number(value, signed=signed)
    return "—" if value is None else str(value)


def battle_text(battle: dict[str, Any], state: WorldState | None = None) -> str:
    """战报一行（只用显示名）：优先用引擎写好的 text；旧存档按势力 id 查名。"""
    if battle.get("text"):
        return str(battle["text"])

    def name(key: str) -> str:
        if battle.get(f"{key}_name"):
            return str(battle[f"{key}_name"])
        faction = state.factions.get(str(battle.get(key))) if state is not None and battle.get(key) else None
        return faction.name if faction else ""

    attacker, defender = name("attacker") or "敌", name("defender") or "守军"
    result = battle.get("result")
    if result == "stalemate":
        return f"{attacker}攻{defender}，相持一季。"
    winner = attacker if result == "attacker_won" else defender
    return f"{attacker}攻{defender}，{winner}胜。"


def public_events(data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """报告中触发事件的展示列表（{id, title, text, illustration, importance}，按 importance 降序）与内部事件列表。

    旧存档的报告没有 events 字段：按 events_fired 与事件事实（kind=event）补出 id 与标题。
    """
    rows = data.get("events")
    if not isinstance(rows, list):
        titles = {
            str(fact.get("entity")): str(fact.get("text", ""))
            for fact in data.get("facts", []) or []
            if isinstance(fact, dict) and fact.get("kind") == "event"
        }
        internal_ids = {
            str(fact.get("entity"))
            for fact in data.get("facts", []) or []
            if isinstance(fact, dict) and fact.get("kind") == "event" and fact.get("internal")
        }
        rows = [
            {
                "id": event_id,
                "title": titles.get(str(event_id), str(event_id)),
                "text": "",
                "illustration": None,
                "importance": 0,
                "internal": str(event_id) in internal_ids,
            }
            for event_id in data.get("events_fired", []) or []
        ]
    shown: list[dict[str, Any]] = []
    hidden: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        (hidden if row.get("internal") else shown).append({key: value for key, value in row.items() if key != "internal"})
    shown.sort(key=lambda row: -int(row.get("importance", 0) or 0))
    return shown, hidden


_number = format_number


def redacted_report(report: TurnReport, state: WorldState) -> TurnReport:
    """给叙事 LLM 用的 TurnReport：去掉内部事实；隐藏资源的事实不带数值，隐藏资源的账目行被移除。"""
    hidden = hidden_resources(state)
    secret_traits = hidden_traits(state)
    redacted = report.model_copy(deep=True)
    relation_seen: set[tuple[Any, ...]] = set()
    redacted.facts = [
        fact for fact in redacted.facts if not is_internal_fact(fact) and not _duplicate_relation(fact.model_dump(), relation_seen)
    ]
    for fact in redacted.facts:
        if fact.kind == "resource" and fact.entity in hidden:
            fact.text = f"{state.resources[fact.entity].name}有所变动" + (f"（{fact.cause}）" if fact.cause else "")
            fact.before = None
            fact.after = None
        if fact.kind == "trait" and _secret_trait(fact.model_dump(), secret_traits):
            fact.text = "某人心态有所变化"
            fact.before = None
            fact.after = None
            fact.cause = None
    redacted.ledger_lines = [line for line in redacted.ledger_lines if line.resource not in hidden]
    redacted.crisis = {}
    redacted.reconciliation = {key: value for key, value in redacted.reconciliation.items() if key not in hidden}
    return redacted


def fact_line(fact: dict[str, Any]) -> str:
    """事实的单行文字；数值型事实附「前→后」（隐藏数值已在 public_report 中抹去）。"""
    text = str(fact.get("text", ""))
    before, after = fact.get("before"), fact.get("after")
    numeric = all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (before, after))
    if numeric and fact.get("kind") in {"resource", "region", "army", "relation", "trait", "counter"}:
        return f"{text}（{format_number(before)}→{format_number(after)}）"
    return text
