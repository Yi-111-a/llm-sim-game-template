from __future__ import annotations

from typing import Any

from simcore.domain.models import WorldState


def matches(conditions: Any, state: WorldState) -> bool:
    if conditions in (None, {}, []):
        return True
    if isinstance(conditions, list):
        return all(matches(item, state) for item in conditions)
    if not isinstance(conditions, dict):
        return False
    if "all" in conditions:
        return all(matches(item, state) for item in conditions["all"])
    if "any" in conditions:
        return any(matches(item, state) for item in conditions["any"])
    if "not" in conditions:
        return not matches(conditions["not"], state)
    kind = conditions.get("kind")
    if kind == "resource":
        amount = state.resources.get(conditions["id"])
        return amount is not None and _compare(
            amount.amount, conditions.get("operator", "gte"), float(conditions["value"])
        )
    if kind == "flag":
        return state.flags.get(conditions["key"]) == conditions.get("value", True)
    if kind == "turn":
        return state.turn == int(conditions["value"])
    if kind == "region_stat":
        region = state.regions.get(conditions["region"])
        if not region:
            return False
        left = region.stats.get(conditions["stat"])
        if left is None:
            return False
        return _compare(left, conditions.get("operator", "gte"), float(conditions["value"]))
    return False


def _compare(left: float, operator: str, right: float) -> bool:
    return {
        "gt": left > right,
        "gte": left >= right,
        "lt": left < right,
        "lte": left <= right,
        "eq": left == right,
    }.get(operator, False)
