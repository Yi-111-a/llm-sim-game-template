from dataclasses import dataclass
from typing import Any

from simcore.domain.models import WorldState
from simcore.domain.ops import StateOp, parse_op


@dataclass(frozen=True)
class Ok:
    op: StateOp


@dataclass(frozen=True)
class Rejected:
    code: str
    message: str


def validate(op_value: StateOp | dict[str, Any], state: WorldState) -> Ok | Rejected:
    try:
        op = parse_op(op_value)
    except (ValueError, TypeError) as exc:
        return Rejected("INVALID_TARGET", str(exc))

    if op.op == "adjust_resource" and op.resource not in state.resources:
        return Rejected("UNKNOWN_ENTITY", f"未知资源：{op.resource}")
    if op.op == "add_ledger" and op.category not in state.resources:
        return Rejected("UNKNOWN_ENTITY", f"账目资源不存在：{op.category}")
    if op.op == "cancel_ledger" and not any(row.id == op.ledger_id for row in state.ledger):
        return Rejected("UNKNOWN_ENTITY", f"账目不存在：{op.ledger_id}")
    if op.op in {"appoint", "dismiss"}:
        character = state.characters.get(op.character)
        office = state.offices.get(op.office)
        if character is None or office is None:
            return Rejected("UNKNOWN_ENTITY", "人物或官职不存在")
        if op.op == "appoint":
            if character.status != "alive" or character.in_transit:
                return Rejected("CHARACTER_UNAVAILABLE", "人物当前不能任职")
            if office.holder not in (None, op.character):
                return Rejected("OFFICE_OCCUPIED", "该官职已有任职者")
            if character.office not in (None, op.office):
                return Rejected("OFFICE_HELD", "人物已担任其他官职，需先免去原职")
            if any(
                character.traits.get(key, 0) < value
                for key, value in office.requires.items()
                if isinstance(value, (int, float))
            ):
                return Rejected("OFFICE_REQUIREMENT", "人物不满足任职条件")
        elif character.office != op.office or office.holder != op.character:
            return Rejected("INVALID_TARGET", "该人物并未担任此官职")
    if op.op == "set_character_status" and op.character not in state.characters:
        return Rejected("UNKNOWN_ENTITY", f"人物不存在：{op.character}")
    if op.op == "move_character":
        character = state.characters.get(op.character)
        if character is None or op.to_region not in state.regions:
            return Rejected("UNKNOWN_ENTITY", "人物或目标地区不存在")
        if character.status != "alive" or character.in_transit:
            return Rejected("CHARACTER_UNAVAILABLE", "人物当前不能行动")
        if op.to_region not in state.regions[character.region].adjacency:
            return Rejected("OUT_OF_RANGE", "目标地区不与人物所在地区相邻")
    if op.op == "move_army":
        army = state.armies.get(op.army)
        if army is None or op.to_region not in state.regions:
            return Rejected("UNKNOWN_ENTITY", "军队或目标地区不存在")
        if army.in_transit:
            return Rejected("CHARACTER_UNAVAILABLE", "军队正在行军")
        if op.to_region not in state.regions[army.region].adjacency:
            return Rejected("OUT_OF_RANGE", "目标地区不与军队所在地区相邻")
    if op.op == "start_project":
        if op.region not in state.regions or op.turns <= 0:
            return Rejected("INVALID_TARGET", "工程地区无效或工期必须大于零")
        for resource, cost in op.cost_per_turn.items():
            current = state.resources.get(resource)
            if current is None:
                return Rejected("UNKNOWN_ENTITY", f"工程资源不存在：{resource}")
            committed = sum(project.cost_per_turn.get(resource, 0) for project in state.projects)
            if current.amount - committed < cost:
                return Rejected("INSUFFICIENT_RESOURCE", f"{current.name}不足，无法支付首期工程款")
    if op.op == "adjust_region" and (
        op.region not in state.regions or op.stat not in state.regions[op.region].stats
    ):
        return Rejected("INVALID_TARGET", "地区或地区指标不存在")
    if op.op == "adjust_relation":
        entities = set(state.characters) | set(state.factions)
        if op.a not in entities or op.b not in entities:
            return Rejected("UNKNOWN_ENTITY", "关系对象不存在")
    if op.op == "cancel_standing_order" and not any(
        row.id == op.id for row in state.standing_orders
    ):
        return Rejected("UNKNOWN_ENTITY", f"常设命令不存在：{op.id}")
    if op.op == "remember" and op.character not in state.characters:
        return Rejected("UNKNOWN_ENTITY", f"人物不存在：{op.character}")
    return Ok(op)
