from dataclasses import dataclass
from typing import Any

from simcore.domain.events import matches
from simcore.domain.models import Character, WorldState
from simcore.domain.ops import StateOp, describe_op_error, parse_op


@dataclass(frozen=True)
class Ok:
    op: StateOp


@dataclass(frozen=True)
class Rejected:
    code: str
    message: str


def held_offices(character: Character) -> list[str]:
    """人物当前担任的全部官职（兼容只写了主职 office 的旧状态）。"""
    held = list(character.offices)
    if character.office and character.office not in held:
        held.insert(0, character.office)
    return held


DEFAULT_TRAIT_NAMES = {
    "ability": "才干",
    "loyalty": "忠诚",
    "ambition": "野心",
    "fear": "畏惧",
    "favor": "恩宠",
    "courage": "胆略",
    "integrity": "操守",
}
STATUS_NAMES = {"alive": "在世", "dead": "已故", "imprisoned": "系狱", "exiled": "流放", "retired": "致仕"}


def entity_name(state: WorldState, entity_id: Any) -> str:
    """任意实体 id 的显示名：人物 / 势力 / 地区 / 军队 / 官职 / 资源；找不到时原样返回。"""
    key = str(entity_id)
    for table in (state.characters, state.factions, state.regions, state.armies, state.offices, state.resources):
        row = table.get(key)
        if row is not None:
            return str(row.name)
    return key


def stat_name(state: WorldState, stat: str) -> str:
    names = state.rules.get("stat_names") or {}
    return str(names.get(stat, stat)) if isinstance(names, dict) else stat


def trait_name(state: WorldState, trait: str) -> str:
    names = state.rules.get("trait_names") or {}
    if isinstance(names, dict) and trait in names:
        return str(names[trait])
    if trait.startswith("relation:"):
        return f"对{entity_name(state, trait.split(':', 1)[1])}的观感"
    return DEFAULT_TRAIT_NAMES.get(trait, trait)


def player_character_id(state: WorldState, settings: dict[str, Any] | None = None) -> str | None:
    """玩家本人：settings.player_character；未设时取 settings.player_office 的在任者。"""
    sources = [state.rules, settings or {}]
    for source in sources:
        character_id = source.get("player_character")
        if character_id and str(character_id) in state.characters:
            return str(character_id)
    for source in sources:
        office_id = source.get("player_office")
        if office_id and str(office_id) in state.offices:
            return state.offices[str(office_id)].holder
    return None


def max_concurrent_offices(state: WorldState) -> int:
    try:
        return max(1, int(state.rules.get("max_concurrent_offices", 1)))
    except (TypeError, ValueError):
        return 1


def validate(op_value: StateOp | dict[str, Any], state: WorldState) -> Ok | Rejected:
    try:
        op = parse_op(op_value)
    except (ValueError, TypeError) as exc:
        return Rejected("INVALID_TARGET", describe_op_error(exc, op_value))
    if state.outcome is not None:
        return Rejected("GAME_OVER", f"本局已终局（{state.outcome.title}），不能再改变世界状态")

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
            held = held_offices(character)
            limit = max_concurrent_offices(state)
            if op.office not in held and len(held) >= limit:
                if limit == 1:
                    return Rejected("OFFICE_HELD", "人物已担任其他官职，需先免去原职")
                return Rejected("OFFICE_HELD", f"人物已兼任 {len(held)} 职，达到上限 {limit}")
            if any(
                character.traits.get(key, 0) < value
                for key, value in office.requires.items()
                if isinstance(value, (int, float))
            ):
                return Rejected("OFFICE_REQUIREMENT", "人物不满足任职条件")
        elif op.office not in held_offices(character) or office.holder != op.character:
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
    if op.op in {"adjust_army", "set_army_faction", "set_army_commander"} and op.army not in state.armies:
        return Rejected("UNKNOWN_ENTITY", f"军队不存在：{op.army}")
    if op.op == "set_army_faction" and op.faction not in state.factions:
        return Rejected("UNKNOWN_ENTITY", f"势力不存在：{op.faction}")
    if op.op == "set_army_commander" and op.character is not None:
        commander = state.characters.get(op.character)
        if commander is None:
            return Rejected("UNKNOWN_ENTITY", f"人物不存在：{op.character}")
        if commander.status != "alive":
            return Rejected("CHARACTER_UNAVAILABLE", f"{commander.name}不在世或不能统兵")
    if op.op == "adjust_trait" and op.character not in state.characters:
        return Rejected("UNKNOWN_ENTITY", f"人物不存在：{op.character}")
    if op.op == "set_region_controller":
        if op.region not in state.regions:
            return Rejected("UNKNOWN_ENTITY", f"地区不存在：{op.region}")
        if state.factions and op.faction not in state.factions:
            return Rejected("UNKNOWN_ENTITY", f"势力不存在：{op.faction}")
    if op.op == "choose_event_option":
        choice = next((row for row in state.pending_choices if row.id == op.choice_id), None)
        if choice is None:
            return Rejected("UNKNOWN_ENTITY", f"待决事件不存在或已处置：{op.choice_id}")
        option = next((row for row in choice.options if row.id == op.option_id), None)
        if option is None:
            return Rejected("UNKNOWN_ENTITY", f"「{choice.title}」没有选项：{op.option_id}")
        if not matches(option.conditions, state):
            return Rejected("CONDITION_FAILED", f"选项「{option.text}」的前提条件尚不满足")
    return Ok(op)
