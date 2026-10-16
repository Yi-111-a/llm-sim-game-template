"""条件 DSL 的中文渲染：结局「所满足的条件」、目标说明等。

只写阈值，不写隐藏数值的真值（「渔阳鼙鼓 < 60」）。旗标、计数器没有显示名时可由剧本
settings.flag_labels / settings.counter_labels（{键: 名}）提供，否则用键名本身。
"""

from __future__ import annotations

from typing import Any

from simcore.domain.events import matches
from simcore.domain.models import WorldState
from simcore.domain.rules import STATUS_NAMES, entity_name, stat_name, trait_name, turn_label
from simcore.domain.visibility import format_number

SYMBOLS = {"eq": "=", "gt": ">", "gte": "≥", "lt": "<", "lte": "≤"}
NEGATED = {"eq": "ne", "gt": "lte", "gte": "lt", "lt": "gte", "lte": "gt"}


def _symbol(operator: str, negate: bool) -> str:
    if negate:
        operator = NEGATED.get(operator, operator)
    return "≠" if operator == "ne" else SYMBOLS.get(operator, operator)


def _number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number) >= 100000:
        return format_number(number)  # 十万以上写作「54.79万」
    return str(int(number)) if number == int(number) else f"{number:g}"


def _label(state: WorldState, table: str, key: str) -> str:
    labels = state.rules.get(table) or {}
    if isinstance(labels, dict) and key in labels:
        return str(labels[key])
    if table == "flag_labels" and key.startswith("memorial:"):
        return f"奏疏 {key.split(':', 1)[1]} 已处置"
    if table == "flag_labels" and key.startswith("event:") and key.endswith(":fired"):
        return f"事件 {key.split(':')[1]} 已发生"
    return key


def describe_condition(condition: Any, state: WorldState, negate: bool = False) -> str:
    """单个条件（或组合）的中文说明。"""
    if condition in (None, {}, []):
        return "无条件"
    if isinstance(condition, bool):
        return "恒真" if condition != negate else "恒假"
    if isinstance(condition, list):
        condition = {"all": condition}
    if not isinstance(condition, dict):
        return str(condition)
    for key, joiner in (("all", "且"), ("any", "或")):
        if key in condition:
            parts = [describe_condition(item, state) for item in condition[key]]
            text = joiner.join(parts)
            return f"并非（{text}）" if negate else (f"（{text}）" if len(parts) > 1 else text)
    if "not" in condition:
        return describe_condition(condition["not"], state, not negate)
    return _leaf(condition, state, negate)


def _leaf(condition: dict[str, Any], state: WorldState, negate: bool) -> str:
    kind = condition.get("kind")
    operator = str(condition.get("operator", "gte" if kind in {"resource", "region_stat"} else "eq"))
    value = condition.get("value")
    if kind == "resource":
        return f"{entity_name(state, condition.get('id'))} {_symbol(operator, negate)} {_number(value)}"
    if kind == "turn":
        return f"时间 {_symbol(operator, negate)} {turn_label(state, value)}"
    if kind == "flag":
        label = _label(state, "flag_labels", str(condition.get("key")))
        expected = condition.get("value", True)
        positive = operator == "exists" or (operator == "eq" and expected is True)
        if operator == "absent":
            positive, negate = True, not negate
        if positive:
            return f"「{label}」{'未成' if negate else '已成'}"
        return f"「{label}」{_symbol(operator, negate)} {expected}"
    if kind == "flag_age":
        label = _label(state, "flag_labels", str(condition.get("key")))
        return f"「{label}」已历 {_symbol(operator, negate)} {_number(value)} 回合"
    if kind == "counter":
        label = _label(state, "counter_labels", str(condition.get("key")))
        return f"{label} {_symbol(operator, negate)} {_number(value)}"
    if kind == "region_stat":
        region = entity_name(state, condition.get("region"))
        return f"{region}{stat_name(state, str(condition.get('stat')))} {_symbol(operator, negate)} {_number(value)}"
    if kind == "region_controller":
        verb = "不归" if negate else "归"
        return f"{entity_name(state, condition.get('region'))}{verb}{entity_name(state, condition.get('faction'))}"
    if kind == "character_status":
        status = STATUS_NAMES.get(str(condition.get("value", "alive")), str(condition.get("value")))
        return f"{entity_name(state, condition.get('character'))}{'非' if negate else ''}{status}"
    if kind == "character_trait":
        trait = trait_name(state, str(condition.get("trait")))
        return f"{entity_name(state, condition.get('character'))}{trait} {_symbol(operator, negate)} {_number(value)}"
    if kind == "character_region":
        verb = "不在" if negate else "在"
        settled = "（不在途中）" if condition.get("arrived") and not negate else ""
        return f"{entity_name(state, condition.get('character'))}{verb}{entity_name(state, condition.get('region'))}{settled}"
    if kind == "office_holder":
        office = entity_name(state, condition.get("office"))
        if condition.get("character") is None:
            return f"{office}{'有人' if negate else '出缺'}"
        return f"{office}{'不' if negate else ''}由{entity_name(state, condition.get('character'))}担任"
    if kind in {"army_commander", "army_region"}:
        negate = negate != (str(condition.get("operator", "eq")) == "ne")
        army = entity_name(state, condition.get("army"))
        if kind == "army_region":
            return f"{army}{'不驻' if negate else '驻'}{entity_name(state, condition.get('region'))}"
        if condition.get("character") is None:
            return f"{army}{'有统帅' if negate else '无统帅'}"
        return f"{army}{'不' if negate else ''}由{entity_name(state, condition.get('character'))}统领"
    if kind == "army_strength":
        who = entity_name(state, condition.get("army") or condition.get("faction"))
        return f"{who}兵力 {_symbol(operator, negate)} {_number(value)}"
    if kind == "relation":
        return f"{entity_name(state, condition.get('a'))}与{entity_name(state, condition.get('b'))}关系 {_symbol(operator, negate)} {_number(value)}"
    return str(condition)


def describe_satisfied(condition: Any, state: WorldState, turn: int) -> list[str]:
    """已满足的条件拆成可读条目：all 列出全部子项；any 只列成立的分支；not 写成否定句。"""
    if condition in (None, {}, []):
        return []
    if isinstance(condition, list):
        condition = {"all": condition}
    if isinstance(condition, dict) and "all" in condition:
        return [line for item in condition["all"] for line in describe_satisfied(item, state, turn)]
    if isinstance(condition, dict) and "any" in condition:
        for item in condition["any"]:
            if matches(item, state, turn):
                return describe_satisfied(item, state, turn)
        return []
    if isinstance(condition, dict) and "not" in condition:
        inner = condition["not"]
        if isinstance(inner, dict) and "any" in inner:
            return [line for item in inner["any"] for line in describe_satisfied({"not": item}, state, turn)]
        return [describe_condition(inner, state, negate=True)]
    return [describe_condition(condition, state)]


def stamp(lines: list[str], turn: int, state: WorldState | None = None) -> list[str]:
    """「天宝十四载冬时…」（无历法可推时为「第20回合时…」）。"""
    when = turn_label(state, turn, short=True) if state is not None else f"第{turn}回合"
    return [f"{when}时{line}" for line in lines]
