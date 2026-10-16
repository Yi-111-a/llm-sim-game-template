from __future__ import annotations

from typing import Any

from simcore.domain.models import WorldState

OPERATORS = ("eq", "gt", "gte", "lt", "lte")
CONDITION_KINDS = (
    "turn",
    "resource",
    "flag",
    "flag_age",
    "counter",
    "region_stat",
    "region_controller",
    "character_status",
    "character_trait",
    "character_region",
    "office_holder",
    "army_strength",
    "army_commander",
    "army_region",
    "relation",
)
# 只比较相等与否的条件（operator 为 eq 或 ne）
IDENTITY_KINDS = frozenset({"army_commander", "army_region"})


def matches(conditions: Any, state: WorldState, turn: int | None = None) -> bool:
    """判定条件 DSL。

    turn 为正在结算进入的目标回合，与 when.turn 同义；在回合外调用时缺省为当前回合。
    比较默认 eq；为兼容旧剧本，resource 与 region_stat 未写 operator 时默认 gte。
    """
    if turn is None:
        turn = state.turn
    if conditions in (None, {}, []):
        return True
    if isinstance(conditions, bool):
        return conditions
    if isinstance(conditions, list):
        return all(matches(item, state, turn) for item in conditions)
    if not isinstance(conditions, dict):
        return False
    if "all" in conditions:
        return all(matches(item, state, turn) for item in conditions["all"])
    if "any" in conditions:
        return any(matches(item, state, turn) for item in conditions["any"])
    if "not" in conditions:
        return not matches(conditions["not"], state, turn)
    try:
        return _match_kind(conditions, state, turn)
    except (KeyError, TypeError, ValueError):
        return False


def _match_kind(conditions: dict[str, Any], state: WorldState, turn: int) -> bool:
    kind = conditions.get("kind")
    operator = str(conditions.get("operator", "eq"))
    if kind == "resource":
        amount = state.resources.get(conditions["id"])
        return amount is not None and _compare(
            amount.amount, conditions.get("operator", "gte"), float(conditions["value"])
        )
    if kind == "flag":
        key = conditions["key"]
        if operator == "exists":
            return key in state.flags and state.flags[key] not in (None, False)
        if operator == "absent":
            return key not in state.flags or state.flags[key] in (None, False)
        value = state.flags.get(key)
        expected = conditions.get("value", True)
        if operator != "eq" and _is_number(value) and _is_number(expected):
            return _compare(float(value), operator, float(expected))
        return value == expected
    if kind == "flag_age":
        key = conditions["key"]
        if key not in state.flag_turns or state.flags.get(key) in (None, False):
            return False
        return _compare(turn - state.flag_turns[key], operator, float(conditions["value"]))
    if kind == "counter":
        return _compare(state.counters.get(conditions["key"], 0), operator, float(conditions["value"]))
    if kind == "turn":
        return _compare(turn, operator, float(conditions["value"]))
    if kind == "region_stat":
        region = state.regions.get(conditions["region"])
        if not region:
            return False
        left = region.stats.get(conditions["stat"])
        if left is None:
            return False
        return _compare(left, conditions.get("operator", "gte"), float(conditions["value"]))
    if kind == "region_controller":
        region = state.regions.get(conditions["region"])
        return region is not None and region.controller == conditions.get("faction")
    if kind == "character_status":
        character = state.characters.get(conditions["character"])
        return character is not None and character.status == conditions.get("value", "alive")
    if kind == "character_trait":
        character = state.characters.get(conditions["character"])
        if character is None:
            return False
        return _compare(character.traits.get(conditions["trait"], 0), operator, float(conditions["value"]))
    if kind == "character_region":
        character = state.characters.get(conditions["character"])
        return character is not None and character.region == conditions.get("region")
    if kind == "office_holder":
        office = state.offices.get(conditions["office"])
        return office is not None and office.holder == conditions.get("character")
    if kind == "army_strength":
        if conditions.get("army") is not None:
            army = state.armies.get(conditions["army"])
            if army is None:
                return False
            total = army.strength
        else:
            faction = conditions.get("faction")
            total = sum(row.strength for row in state.armies.values() if row.faction == faction)
        return _compare(total, operator, float(conditions["value"]))
    if kind in IDENTITY_KINDS:
        army = state.armies.get(conditions["army"])
        if army is None:
            return False
        if kind == "army_commander":
            # character: null 表示无统帅；统帅已不在世视同无统帅
            commander = army.commander
            if commander is not None and (commander not in state.characters or state.characters[commander].status != "alive"):
                commander = None
            same = commander == conditions.get("character")
        else:
            # 驻地：在途的军队仍算在出发地（抵达那一季的结算才改驻地），与 character_region 一致
            same = army.region == conditions.get("region")
        return not same if operator == "ne" else same
    if kind == "relation":
        return _compare(relation_value(state, conditions["a"], conditions["b"]), operator, float(conditions["value"]))
    return False


def relation_value(state: WorldState, a: str, b: str) -> float:
    character = state.characters.get(a)
    if character is not None:
        return float(character.traits.get(f"relation:{b}", 0))
    faction = state.factions.get(a)
    if faction is not None:
        return float(faction.relations.get(b, 0))
    return 0.0


def condition_errors(conditions: Any, location: str) -> list[str]:
    """静态检查条件结构，供剧本校验使用。"""
    if conditions in (None, {}, []) or isinstance(conditions, bool):
        return []
    if isinstance(conditions, list):
        return [error for index, item in enumerate(conditions) for error in condition_errors(item, f"{location}[{index}]")]
    if not isinstance(conditions, dict):
        return [f"{location} 条件必须是对象或列表"]
    for key in ("all", "any"):
        if key in conditions:
            if not isinstance(conditions[key], list):
                return [f"{location}.{key} 必须是列表"]
            return condition_errors(conditions[key], f"{location}.{key}")
    if "not" in conditions:
        return condition_errors(conditions["not"], f"{location}.not")
    kind = conditions.get("kind")
    if kind not in CONDITION_KINDS:
        return [f"{location} 未知条件类型：{kind}"]
    operator = conditions.get("operator")
    allowed = (*OPERATORS, "exists", "absent") if kind == "flag" else ("eq", "ne") if kind in IDENTITY_KINDS else OPERATORS
    if kind in IDENTITY_KINDS and not conditions.get("army"):
        return [f"{location} 缺少 army"]
    if kind == "army_commander" and "character" not in conditions:
        return [f"{location} 缺少 character（无统帅写 null）"]
    if kind == "army_region" and not conditions.get("region"):
        return [f"{location} 缺少 region"]
    if operator is not None and operator not in allowed:
        return [f"{location} 不支持的比较符：{operator}"]
    return []


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _compare(left: float, operator: str, right: float) -> bool:
    return {
        "gt": left > right,
        "gte": left >= right,
        "lt": left < right,
        "lte": left <= right,
        "eq": left == right,
    }.get(operator, False)
