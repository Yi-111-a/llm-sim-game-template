"""隐藏数值与公开视图。API 的 /view 与 MCP 的全部工具只能使用本模块的输出。"""

from __future__ import annotations

import os
import random
from typing import Any

from simcore.domain.crisis import band_for, evaluate_terms, expression_names, formula_values, interval_evaluate
from simcore.domain.models import TurnReport, WorldState, is_internal_fact

DEFAULT_HIDDEN_WIDTH = 10.0


def estimate_interval(state: WorldState, resource_id: str, seed: int) -> tuple[float, float]:
    """隐藏资源的「风闻区间」。

    半宽 = estimate_width × (1 + corruption/100)，flag intel:<resource> 存在时减半；
    区间中心带 ±半宽/2 的种子噪声，同一回合内稳定；真值始终落在区间内。
    estimate_width 未设（≤0）时按 10 处理，避免隐藏资源意外以零宽区间泄露。
    """
    resource = state.resources[resource_id]
    corruption_id = str(state.rules.get("corruption_resource", "corruption"))
    corruption = state.resources[corruption_id].amount if corruption_id in state.resources else 0.0
    width = resource.estimate_width if resource.estimate_width > 0 else DEFAULT_HIDDEN_WIDTH
    half = width * (1 + max(0.0, corruption) / 100)
    if state.flags.get(f"intel:{resource_id}") not in (None, False):
        half /= 2
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
        return f"{row.name} 约{format_number(low)}–{format_number(high)}{row.unit}（风闻）"
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
            "display": format_value(term_value) + ("（风闻）" if uses_hidden and term_value is not None else ""),
        })
    return {
        "title": crisis.get("title", resource.name if resource else ""),
        "resource": resource_id,
        "value_or_estimate": value,
        "display": format_value(value) + ("（风闻）" if isinstance(value, list) else ""),
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
    for fact in data.get("facts", []):
        fact = dict(fact)
        if is_internal_fact(fact):
            internal.append(fact)
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
    if data.get("crisis"):
        data["crisis"] = public_crisis(crisis, state, seed, data["crisis"].get("terms")) if crisis else {}
    return data


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


def format_value(value: Any) -> str:
    """数字或风闻区间 [lo, hi] 的显示文字；其他值原样转字符串。"""
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(item, (int, float)) for item in value):
        return f"约{format_number(value[0])}–{format_number(value[1])}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return format_number(value)
    return "—" if value is None else str(value)


_number = format_number


def redacted_report(report: TurnReport, state: WorldState) -> TurnReport:
    """给叙事 LLM 用的 TurnReport：去掉内部事实；隐藏资源的事实不带数值，隐藏资源的账目行被移除。"""
    hidden = hidden_resources(state)
    secret_traits = hidden_traits(state)
    redacted = report.model_copy(deep=True)
    redacted.facts = [fact for fact in redacted.facts if not is_internal_fact(fact)]
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
    return redacted


def fact_line(fact: dict[str, Any]) -> str:
    """事实的单行文字；数值型事实附「前→后」（隐藏数值已在 public_report 中抹去）。"""
    text = str(fact.get("text", ""))
    before, after = fact.get("before"), fact.get("after")
    numeric = all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (before, after))
    if numeric and fact.get("kind") in {"resource", "region", "army", "relation", "trait", "counter"}:
        return f"{text}（{format_number(before)}→{format_number(after)}）"
    return text
