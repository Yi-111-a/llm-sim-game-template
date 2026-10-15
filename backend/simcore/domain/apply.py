from __future__ import annotations

from typing import Any

from simcore.domain.models import (
    Fact,
    LedgerEntry,
    PendingClause,
    Project,
    StandingOrder,
    WorldState,
    clone,
)
from simcore.domain.ops import (
    AddLedger,
    AddStandingOrder,
    AdjustArmy,
    AdjustCounter,
    AdjustTrait,
    Appoint,
    CancelLedger,
    CancelStandingOrder,
    ChooseEventOption,
    Dismiss,
    MoveArmy,
    MoveCharacter,
    Remember,
    SetArmyCommander,
    SetArmyFaction,
    SetCharacterStatus,
    SetCounter,
    SetFlag,
    SetRegionController,
    StartProject,
    StateOp,
    parse_op,
)
from simcore.domain.rules import (
    STATUS_NAMES,
    Rejected,
    entity_name,
    find_route,
    held_offices,
    stat_name,
    trait_name,
    turn_unit_label,
    validate,
)
from simcore.domain.visibility import format_number


class OpRejected(ValueError):
    """规则层拒绝的操作；code 为稳定英文错误码，message 为中文说明。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def signed_number(value: float) -> str:
    """带符号的变化量：+5、−40（全角减号，与奏疏摘要一致）。"""
    text = format_number(abs(value))
    return f"−{text}" if round(value, 1) < 0 else f"+{text}"


def apply(
    op_value: StateOp | dict[str, Any],
    state: WorldState,
    *,
    turn: int | None = None,
    inplace: bool = False,
    source: str | None = None,
) -> tuple[WorldState, list[Fact]]:
    """校验并执行一条操作，返回新状态与事实。

    turn 为写入 flag_turns 等时间戳所用的回合；回合结算中传入正在结算进入的回合，缺省为 state.turn。
    inplace=True 时直接修改传入的状态（调用方已持有私有副本，例如回合结算中）；校验在任何修改之前完成，
    被拒时状态不变。缺省返回新副本、不动传入状态。
    source 为来源说明（奏疏标题、事件标题、抉择、工程、常设令等）：op 没写 reason 时作为事实的原因（Fact.cause），
    数值型事实的文本写成「国库 −40（原因）」。
    """
    result = validate(op_value, state)
    if isinstance(result, Rejected):
        raise OpRejected(result.code, result.message)
    op = result.op
    stamp = state.turn if turn is None else turn
    updated = state if inplace else clone(state)
    facts: list[Fact] = []
    reason = str(getattr(op, "reason", "") or "").strip()
    cause = reason or (source or "").strip() or None
    suffix = f"（{cause}）" if cause else ""
    if op.op == "adjust_resource":
        row = updated.resources[op.resource]
        before = row.amount
        amount = before + op.delta
        if row.minimum is not None:
            amount = max(row.minimum, amount)
        if row.maximum is not None:
            amount = min(row.maximum, amount)
        row.amount = amount
        facts.append(
            Fact(kind="resource", text=f"{row.name} {signed_number(amount - before)}{suffix}", entity=op.resource, before=before, after=amount)
        )
    elif isinstance(op, AddLedger):
        updated.ledger.append(
            LedgerEntry(
                id=_next_id(updated, "ledger"),
                name=op.name,
                category=op.category,
                amount=op.amount,
                kind=op.kind,
                turns=op.turns,
                reason=op.reason,
            )
        )
        facts.append(Fact(kind="ledger", text=f"新增账目：{op.name}", entity=op.category))
    elif isinstance(op, CancelLedger):
        cancelled = next((row.name for row in updated.ledger if row.id == op.ledger_id), op.ledger_id)
        updated.ledger = [row for row in updated.ledger if row.id != op.ledger_id]
        facts.append(Fact(kind="ledger", text=f"取消账目：{cancelled}", entity=op.ledger_id))
    elif isinstance(op, Appoint):
        character = updated.characters[op.character]
        held = held_offices(character)
        if op.office not in held:
            held.append(op.office)
        character.offices = held
        if character.office is None:
            character.office = op.office
        updated.offices[op.office].holder = op.character
        office_name = updated.offices[op.office].name
        facts.append(Fact(kind="appointment", text=f"{character.name}任{office_name}", entity=op.character, after=op.office))
    elif isinstance(op, Dismiss):
        character = updated.characters[op.character]
        held = [office for office in held_offices(character) if office != op.office]
        character.offices = held
        if character.office == op.office or character.office not in held:
            character.office = held[0] if held else None
        updated.offices[op.office].holder = None
        office_name = updated.offices[op.office].name
        facts.append(Fact(kind="appointment", text=f"{character.name}免{office_name}", entity=op.character, before=op.office))
    elif isinstance(op, SetCharacterStatus):
        character = updated.characters[op.character]
        before = character.status
        character.status = op.status
        if op.status != "alive":
            for office_id in held_offices(character):
                if office_id in updated.offices and updated.offices[office_id].holder == character.id:
                    updated.offices[office_id].holder = None
            character.offices = []
            character.office = None
            character.in_transit = None
            for army in updated.armies.values():
                if army.commander == character.id:
                    army.commander = None
        cause = (op.cause or "").strip()
        if cause and character.name in cause:
            text = cause
        else:
            text = f"{character.name}{STATUS_NAMES.get(op.status, op.status)}" + (f"：{cause}" if cause else "")
        facts.append(Fact(kind="status", text=text, entity=op.character, before=before, after=op.status))
    elif isinstance(op, MoveCharacter):
        character = updated.characters[op.character]
        route_text = _start_transit(updated, character, op.to_region, character.faction)
        origin = updated.regions[character.region].name
        facts.append(
            Fact(kind="movement", text=f"{character.name}自{origin}启程赴{route_text}", entity=op.character, before=character.region, after=op.to_region)
        )
    elif isinstance(op, MoveArmy):
        army = updated.armies[op.army]
        route_text = _start_transit(updated, army, op.to_region, army.faction)
        origin = updated.regions[army.region].name
        facts.append(Fact(kind="movement", text=f"{army.name}自{origin}开拔，赴{route_text}", entity=op.army, before=army.region, after=op.to_region))
    elif isinstance(op, StartProject):
        updated.projects.append(
            Project(
                id=_next_id(updated, "project"),
                name=op.name,
                region=op.region,
                cost_per_turn=op.cost_per_turn,
                turns=op.turns,
                on_complete=op.on_complete,
            )
        )
        facts.append(Fact(kind="project", text=f"{updated.regions[op.region].name}兴工：{op.name}", entity=op.region))
    elif op.op == "adjust_region":
        region = updated.regions[op.region]
        before = region.stats[op.stat]
        region.stats[op.stat] += op.delta
        facts.append(
            Fact(
                kind="region",
                text=f"{region.name}{stat_name(updated, op.stat)} {signed_number(op.delta)}{suffix}",
                entity=op.region,
                before=before,
                after=region.stats[op.stat],
                key=op.stat,
            )
        )
    elif op.op == "adjust_relation":
        target = updated.characters.get(op.a)
        if target:
            before = target.traits.get(f"relation:{op.b}", 0)
            target.traits[f"relation:{op.b}"] = before + op.delta
            after = before + op.delta
        else:
            target_faction = updated.factions[op.a]
            before = target_faction.relations.get(op.b, 0)
            target_faction.relations[op.b] = before + op.delta
            after = before + op.delta
        facts.append(
            Fact(
                kind="relation",
                text=f"{entity_name(updated, op.a)}与{entity_name(updated, op.b)}关系 {signed_number(op.delta)}{suffix}",
                entity=op.a,
                before=before,
                after=after,
                key=op.b,
            )
        )
    elif isinstance(op, AddStandingOrder):
        updated.standing_orders.append(
            StandingOrder(
                id=_next_id(updated, "order"),
                text=op.text,
                ops=op.ops,
                until_turn=op.until_turn,
            )
        )
        facts.append(Fact(kind="standing_order", text=f"新增常设令：{op.text}", after=op.text))
    elif isinstance(op, CancelStandingOrder):
        order_text = next((row.text for row in updated.standing_orders if row.id == op.id), op.id)
        updated.standing_orders = [row for row in updated.standing_orders if row.id != op.id]
        facts.append(Fact(kind="standing_order", text=f"撤销常设令：{order_text}", entity=op.id))
    elif isinstance(op, SetFlag):
        before = updated.flags.get(op.key)
        updated.flags[op.key] = op.value
        updated.flag_turns[op.key] = stamp
        facts.append(Fact(kind="flag", text=f"旗标改变：{op.key}", before=before, after=op.value, key=op.key, internal=True))
    elif isinstance(op, Remember):
        updated.characters[op.character].memories.append(op.fact)
        name = updated.characters[op.character].name
        facts.append(Fact(kind="memory", text=f"{name}记下：{op.fact}", entity=op.character, after=op.fact, internal=True))
    elif isinstance(op, AdjustArmy):
        army = updated.armies[op.army]
        before = army.strength
        army.strength = max(0.0, before + op.delta)
        text = reason if reason and army.name in reason else f"{army.name}兵力 {signed_number(army.strength - before)}{suffix}"
        facts.append(Fact(kind="army", text=text, entity=op.army, before=before, after=army.strength))
    elif isinstance(op, SetArmyFaction):
        army = updated.armies[op.army]
        before = army.faction
        army.faction = op.faction
        faction_name = updated.factions[op.faction].name
        facts.append(Fact(kind="army_faction", text=f"{army.name}改隶{faction_name}", entity=op.army, before=before, after=op.faction))
    elif isinstance(op, SetArmyCommander):
        army = updated.armies[op.army]
        before = army.commander
        army.commander = op.character
        text = f"{army.name}易帅，由{entity_name(updated, op.character)}统领" if op.character else f"罢{army.name}统帅"
        facts.append(Fact(kind="army_commander", text=text, entity=op.army, before=before, after=op.character))
    elif isinstance(op, AdjustTrait):
        character = updated.characters[op.character]
        before = character.traits.get(op.trait, 0)
        character.traits[op.trait] = before + op.delta
        facts.append(
            Fact(
                kind="trait",
                text=f"{character.name}{trait_name(updated, op.trait)} {signed_number(op.delta)}{suffix}",
                entity=op.character,
                before=before,
                after=before + op.delta,
                key=op.trait,
            )
        )
    elif isinstance(op, AdjustCounter):
        before_count = updated.counters.get(op.key, 0)
        updated.counters[op.key] = before_count + op.delta
        facts.append(
            Fact(kind="counter", text=f"计数变化：{op.key}", entity=op.key, before=before_count, after=before_count + op.delta, key=op.key, internal=True)
        )
    elif isinstance(op, SetCounter):
        before_count = updated.counters.get(op.key, 0)
        updated.counters[op.key] = op.value
        facts.append(Fact(kind="counter", text=f"计数设定：{op.key}", entity=op.key, before=before_count, after=op.value, key=op.key, internal=True))
    elif isinstance(op, SetRegionController):
        region = updated.regions[op.region]
        before = region.controller
        region.controller = op.faction
        faction = updated.factions.get(op.faction)
        text = f"{region.name}归{faction.name if faction else op.faction}控制"
        facts.append(Fact(kind="region_controller", text=text, entity=op.region, before=before, after=op.faction))
    elif isinstance(op, ChooseEventOption):
        choice = next(row for row in updated.pending_choices if row.id == op.choice_id)
        option = next(row for row in choice.options if row.id == op.option_id)
        updated.pending_choices = [row for row in updated.pending_choices if row.id != op.choice_id]
        for key in (f"choice:{choice.id}", f"event:{choice.event_id}:choice"):
            updated.flags[key] = option.id
            updated.flag_turns[key] = stamp
        facts.append(Fact(kind="choice", text=f"{choice.title}：{option.text}", entity=choice.event_id, after=option.id))
        for effect in option.effects:
            try:
                updated, effect_facts = apply(effect, updated, turn=stamp, inplace=True, source=choice.title)
                facts.extend(effect_facts)
            except ValueError as exc:
                facts.append(Fact(kind="effect_skipped", text=f"选项效果未执行：{exc}", entity=choice.event_id, internal=True))
    else:
        raise TypeError(f"Unsupported operation: {op.op}")
    if cause:
        for fact in facts:
            if fact.cause is None and fact.kind != "choice":
                fact.cause = cause
    return updated, facts


def _start_transit(state: WorldState, mover: Any, destination: str, faction: str | None) -> str:
    """按最短路径（rules.find_route）整段出发：写入 in_transit，返回「目的地（经某某，约 n 季）」。"""
    found = find_route(state, mover.region, destination, faction)
    path, turns = found if found is not None else ([mover.region, destination], 1)
    turns = max(1, turns)
    mover.in_transit = {"to_region": destination, "eta_turn": state.turn + turns, "from_region": mover.region, "route": path}
    via = [state.regions[region_id].name for region_id in path[1:-1]]
    detail = (f"经{'、'.join(via)}，" if via else "") + f"约{turns}{turn_unit_label(state)}"
    return f"{state.regions[destination].name}（{detail}）"


RULER_TITLE_DEFAULT = "陛下"


def edict_memories(op_value: StateOp | dict[str, Any], state: WorldState) -> list[dict[str, Any]]:
    """诏书中任免、处置人物的条款给当事人写一条记忆（Remember op）。state 为执行该条款之前的状态。

    称谓取 rules.ruler_title（缺省「陛下」）；赐死等 dead 状态不写记忆。
    """
    try:
        op = parse_op(op_value)
    except (ValueError, TypeError):
        return []
    character_id = getattr(op, "character", None)
    character = state.characters.get(str(character_id)) if character_id else None
    if character is None:
        return []
    ruler = str(state.rules.get("ruler_title") or RULER_TITLE_DEFAULT)
    when = state.calendar.label or f"第{state.turn}回合"
    if isinstance(op, (Appoint, Dismiss)):
        office = state.offices.get(op.office)
        if office is None:
            return []
        phrase = f"{ruler}任{character.name}为{office.name}" if isinstance(op, Appoint) else f"{ruler}免{character.name}{office.name}之职"
    elif isinstance(op, SetCharacterStatus):
        phrase = {
            "imprisoned": f"{ruler}下{character.name}狱",
            "exiled": f"{ruler}流放{character.name}",
            "retired": f"{ruler}令{character.name}致仕",
            "alive": f"{ruler}赦免{character.name}" if character.status != "alive" else "",
        }.get(op.status, "")
        if phrase and op.cause:
            phrase += f"（{op.cause}）"
    else:
        return []
    return [{"op": "remember", "character": character.id, "fact": f"{when}，{phrase}"}] if phrase else []


def queue_delayed(
    state: WorldState, ops: list[dict[str, Any]], due_turn: int, source: str = "edict", label: str = ""
) -> WorldState:
    updated = clone(state)
    sequence = int(updated.flags.get("_internal:id_seq", 0)) + 1
    updated.flags["_internal:id_seq"] = sequence
    updated.pending_clauses.append(
        PendingClause(
            id=f"pending-{sequence:08x}",
            ops=[parse_op(op).model_dump() for op in ops],
            due_turn=due_turn,
            source=source,
            label=label,
        )
    )
    return updated


def _next_id(state: WorldState, prefix: str) -> str:
    sequence = int(state.flags.get("_internal:id_seq", 0)) + 1
    state.flags["_internal:id_seq"] = sequence
    return f"{prefix}-{sequence:08x}"
