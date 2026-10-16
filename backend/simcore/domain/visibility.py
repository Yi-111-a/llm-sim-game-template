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


HIDDEN_DELTA_EDGES = (0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 30.0, 50.0, 100.0)


def delta_range(amount: float) -> list[float] | float:
    """隐藏数值的变化量只给档位区间：[+3, +5]、[−1, −0.5]；为 0 时给 0。档位见 HIDDEN_DELTA_EDGES。"""
    if abs(amount) < 1e-9:
        return 0.0
    size = abs(amount)
    low, high = HIDDEN_DELTA_EDGES[-2], HIDDEN_DELTA_EDGES[-1]
    for left, right in zip(HIDDEN_DELTA_EDGES, HIDDEN_DELTA_EDGES[1:], strict=False):
        if size <= right + 1e-9:
            low, high = left, right
            break
    return [low, high] if amount > 0 else [-high, -low]


def _noisy_interval(value: float, half: float, key: str) -> tuple[float, float]:
    """隐藏变量的区间（与风闻区间同法：中心带 ±半宽/2 的种子噪声，真值必在区间内）。"""
    center = value + random.Random(key).uniform(-half / 2, half / 2)
    return (center - half, center + half)


CRISIS_ITEM_KINDS = {
    "edict": "诏令",
    "choice": "抉择",
    "delayed": "延期执行",
    "standing": "常设令/账目/工程",
    "event": "事件冲击",
}


def public_crisis_change(
    crisis: dict[str, Any] | None, change: Any, state: WorldState, seed: int
) -> dict[str, Any] | None:
    """本季危机值变化的公开拆分：漂移（结算当时各分项）、难度加成、事件冲击（带事件标题）、诏令 / 延期 / 抉择等，
    以及上下限截断与余差。危机资源隐藏时，各项与合计只给档位区间（delta_range）；分项公式引用隐藏变量时以区间代入。

    返回 {title, hidden, total, items: [{kind, kind_label, label, value, display}], drift: {terms, base, factor, difficulty_label,
    difficulty, clamp}, lines: [中文行]}。
    """
    if not crisis or not isinstance(change, dict) or change.get("resource") not in state.resources:
        return None
    resource = state.resources[str(change["resource"])]
    hidden_crisis = resource.hidden
    hidden = hidden_resources(state)

    def shown(amount: float) -> Any:
        return delta_range(amount) if hidden_crisis else round(amount, 2)

    items = []
    for row in change.get("items", []) or []:
        amount = float(row.get("amount", 0.0) or 0.0)
        value = shown(amount)
        items.append({
            "kind": row.get("kind"),
            "kind_label": CRISIS_ITEM_KINDS.get(str(row.get("kind")), "其他"),
            "label": str(row.get("label", "")),
            "value": value,
            "display": format_value(value, signed=True),
        })
    drift_raw = change.get("drift") or {}
    drift: dict[str, Any] = {}
    if drift_raw:
        values = {key: float(value) for key, value in (drift_raw.get("values") or {}).items()}
        intervals = {
            key: _noisy_interval(values[key], float(intel_precision(state, key)["half_width"]), f"crisis-split:{seed}:{state.turn}:{key}")
            for key in hidden
            if key in values
        }
        terms = []
        low_sum = high_sum = 0.0
        exact_ids = {row.get("id"): row.get("value") for row in drift_raw.get("terms", []) or []}
        for term in crisis.get("terms", []) or []:
            expression = str(term.get("expr", "0"))
            if expression_names(expression) & hidden:
                bounds = interval_evaluate(expression, values, intervals)
                value: Any = list(bounds) if bounds is not None else None
                if bounds is not None:
                    low_sum += bounds[0]
                    high_sum += bounds[1]
            else:
                value = exact_ids.get(term.get("id"))
                if isinstance(value, (int, float)):
                    low_sum += float(value)
                    high_sum += float(value)
            terms.append({"id": term.get("id"), "label": term.get("label", term.get("id")), "value": value, "display": format_value(value, signed=True)})
        factor = float(drift_raw.get("factor", 1.0) or 1.0)
        if hidden_crisis and any(isinstance(row["value"], list) for row in terms):
            base: Any = [round(low_sum, 2), round(high_sum, 2)]
            extra: Any = sorted([round(low_sum * (factor - 1), 2), round(high_sum * (factor - 1), 2)])
        else:
            base = round(float(drift_raw.get("base", 0.0) or 0.0), 2)  # 危机值公开时漂移总额本已见于账目行
            extra = round(float(drift_raw.get("difficulty", 0.0) or 0.0), 2)
        if abs(factor - 1) < 1e-9:
            extra = 0.0
        difficulty = state.rules.get("difficulty") if isinstance(state.rules.get("difficulty"), dict) else {}
        clamp = float(drift_raw.get("clamp", 0.0) or 0.0)
        drift = {
            "terms": terms,
            "base": base,
            "base_display": format_value(base, signed=True),
            "factor": factor,
            "difficulty_label": str(difficulty.get("label") or ""),
            "difficulty": extra,
            "difficulty_display": format_value(extra, signed=True),
            "clamp": shown(clamp) if abs(clamp) > 1e-9 else 0.0,
        }
    other = float(change.get("other", 0.0) or 0.0)
    total_raw = float(change.get("closing", 0.0) or 0.0) - float(change.get("opening", 0.0) or 0.0)
    total = shown(total_raw)
    title = str(crisis.get("title", resource.name))
    suffix = "，风闻" if hidden_crisis else ""
    lines = [f"{title}变化：{format_value(total, signed=True)}{suffix}（以下为该季结算当时的实际贡献，非下季预计）"]
    if drift:
        term_text = "；".join(f"{row['label']} {row['display']}" for row in drift["terms"])
        lines.append(f"漂移 {drift['base_display']}（{term_text}）")
        if abs(drift["factor"] - 1) > 1e-9:
            label = drift["difficulty_label"] or "难度"
            lines.append(f"难度加成（{label} ×{drift['factor']:g}）{drift['difficulty_display']}")
        if drift["clamp"] not in (0, 0.0):
            lines.append(f"上下限截断 {format_value(drift['clamp'], signed=True)}")
    for item in items:
        lines.append(f"{item['kind_label']}「{item['label']}」{item['display']}")
    if abs(other) > 1e-9:
        lines.append(f"其他（截断等）{format_value(shown(other), signed=True)}")
    return {
        "title": title,
        "hidden": hidden_crisis,
        "total": total,
        "total_display": format_value(total, signed=True),
        "items": items,
        "drift": drift,
        "other": shown(other) if abs(other) > 1e-9 else 0.0,
        "lines": lines,
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
        {**battle, "text": battle_text(battle, state), "detail": battle_breakdown(battle)} if isinstance(battle, dict) else battle
        for battle in data.get("battles", []) or []
    ]
    # 本季危机变化拆分（结算当时的公开快照，见 public_crisis_change）；旧存档没有时为 None
    data["crisis_change"] = data["crisis"].get("public_change") if isinstance(data.get("crisis"), dict) else None
    if data.get("crisis"):
        snapshot = data["crisis"].get("public")
        if isinstance(snapshot, dict):
            data["crisis"] = snapshot  # 结算当时计算的公开快照（end_turn / 季报 / 简报同一份数字）
        else:
            data["crisis"] = public_crisis(crisis, state, seed, data["crisis"].get("terms")) if crisis else {}
    data["reconciliation"] = public_reconciliation(data.get("reconciliation"), state, data.get("regular_detail"))
    data.pop("regular_detail", None)  # 已并入 reconciliation[].regular_detail（隐藏资源的明细不外露）
    return data


RECONCILIATION_STEPS = (
    ("opening", "期初"),
    ("edicts", "诏令"),
    ("delayed", "延期执行"),
    ("regular", "经常收支"),
    ("events", "事件/选择"),
    ("closing", "期末"),
)
REGULAR_KIND_LABELS = {"income": "收入", "upkeep": "养兵", "ledger": "账目", "project": "工程", "drift": "漂移", "standing": "常设令/竣工", "other": "其他"}


def public_reconciliation(raw: Any, state: WorldState, detail: Any = None) -> list[dict[str, Any]]:
    """公开资源的分段对账：期初 → 诏令 → 延期执行 → 经常收支 → 事件/选择 → [取整差] → 期末（隐藏资源不列）。

    数值字段保留原始浮点（相加等于期末，误差 < 1e-6）；shown / display 各段按一位小数取整，取整余差不并入任何一段，
    而是单列「取整差」（shown.rounding），使显示的各段之和与期末完全一致、每段显示值都等于其真值的取整。
    regular_detail：经常收支明细 [{kind, label, amount, shown, display}]（收入按公式 label、账目逐项、工程逐项、常设令、漂移、
    上下限截断），明细显示值与该段显示值之差同样单列「取整差」。
    """
    if not isinstance(raw, dict):
        return []
    hidden = hidden_resources(state)
    details = detail if isinstance(detail, dict) else {}
    rows: list[dict[str, Any]] = []
    for key, values in raw.items():
        if key in hidden or key not in state.resources or not isinstance(values, dict) or "opening" not in values:
            continue
        resource = state.resources[key]
        numbers = {step: float(values.get(step, 0.0) or 0.0) for step, _ in RECONCILIATION_STEPS}
        shown = {step: round(numbers[step], 1) for step, _ in RECONCILIATION_STEPS}
        deltas = [step for step, _ in RECONCILIATION_STEPS[1:-1]]
        residual = round(shown["closing"] - shown["opening"] - sum(shown[step] for step in deltas), 1)
        parts = [
            f"{label} {_signed(shown[step]) if step not in {'opening', 'closing'} else format_number(shown[step])}"
            for step, label in RECONCILIATION_STEPS
        ]
        if residual:
            shown["rounding"] = residual
            parts.insert(-1, f"取整差 {_signed(residual)}")
        unit = f"（{resource.unit}）" if resource.unit else ""
        row: dict[str, Any] = {
            "resource": key,
            "name": resource.name,
            "unit": resource.unit,
            **numbers,
            "shown": shown,
            "display": f"{resource.name}{unit}：" + " → ".join(parts),
        }
        items = details.get(key)
        if isinstance(items, list) and items:
            regular: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                amount = float(item.get("amount", 0.0) or 0.0)
                regular.append({
                    "kind": item.get("kind"),
                    "label": str(item.get("label", "")),
                    "amount": amount,
                    "shown": round(amount, 1),
                    "display": f"{item.get('label', '')} {_signed(round(amount, 1))}",
                })
            gap = round(shown["regular"] - sum(item["shown"] for item in regular), 1)
            if gap:
                regular.append({"kind": "rounding", "label": "取整差", "amount": 0.0, "shown": gap, "display": f"取整差 {_signed(gap)}"})
            row["regular_detail"] = regular
            row["regular_display"] = "经常收支明细：" + "；".join(item["display"] for item in regular)
        rows.append(row)
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
    if abs(rounded) >= 100000:  # 十万以上用「万」，如 547900 → 54.79万（玩家文本不出现六位以上的裸数）
        wan = f"{abs(rounded) / 10000:.2f}".rstrip("0").rstrip(".")
        text = f"{'-' if rounded < 0 else ''}{wan}万"
    else:
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


def _battle_side(rows: list[dict[str, Any]]) -> str:
    parts = []
    for row in rows:
        extras = []
        quality = float(row.get("quality", 1) or 1)
        if abs(quality - 1) > 1e-9:
            extras.append(f"素质×{quality:g}")
        who = str(row.get("commander") or "")
        command = row.get("command")
        extras.append(f"主帅{who}在军" if command == "present" else f"主帅{who}不在军中，副将暂摄" if command == "acting" else "无主帅")
        extras.extend(str(note) for note in row.get("modifiers") or [])
        parts.append(f"{row.get('name', row.get('army'))}{format_number(float(row.get('strength', 0)))}（{'，'.join(extras)}）")
    return "、".join(parts) or "—"


def battle_breakdown(battle: dict[str, Any]) -> str:
    """战报因素一段：双方各军兵力 / 素质 / 主帅、城防加成、天时幅度、战力算法、各军伤亡与去向（退往 / 溃散）。旧存档无因素时返回空串。"""
    factors = battle.get("factors")
    if not isinstance(factors, dict):
        return ""
    defense = float(factors.get("defense_bonus") or 0)
    luck = float(factors.get("luck_spread", 0.15) or 0) * 100
    sides = f"攻：{_battle_side(factors.get('attacker') or [])}；守：{_battle_side(factors.get('defender') or [])}"
    if defense:
        sides += f"，城防+{format_number(defense)}%"
    ordered = any(row.get("modifiers") for row in [*(factors.get("attacker") or []), *(factors.get("defender") or [])])
    formula = f"战力=兵×素质{'×军令' if ordered else ''}{'×(1+城防%)' if defense else ''}×天时±{luck:g}%"
    if battle.get("result") == "stalemate":
        return f"{sides}。{formula}；城防≥80 且攻方战力不足守方两倍，据险相持，双方无损。"
    losses = battle.get("losses") or {}
    routed = battle.get("routed") or {}
    retreats = battle.get("retreat_names") or {}
    pieces = []
    for row in [*(factors.get("attacker") or []), *(factors.get("defender") or [])]:
        army = row.get("army")
        if army not in losses:
            continue
        piece = f"{row.get('name', army)}−{format_number(float(losses[army]))}"
        if army in routed:
            piece += f"，无路可退，余{format_number(float(routed[army]))}溃散"
        elif army in retreats:
            piece += f"，退往{retreats[army]}"
        pieces.append(piece)
    rule = f"胜方各折{float(factors.get('winner_loss', 0.1)) * 10:g}成、败方各折{float(factors.get('loser_loss', 0.3)) * 10:g}成"
    return f"{sides}。{formula}。伤亡（{rule}）：{'；'.join(pieces) or '无'}。"


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
