from __future__ import annotations

from typing import Any

from simcore.domain.models import (
    Army,
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
    Detach,
    Dismiss,
    March,
    MergeArmy,
    MoveArmy,
    MoveCharacter,
    Remember,
    SetArmyCommander,
    SetArmyFaction,
    SetArmyPosture,
    SetCharacterFaction,
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
    appointment_station,
    army_station,
    clamp_stat,
    coalesce_postings,
    entity_name,
    find_route,
    held_offices,
    left_behind_armies,
    left_behind_text,
    office_army,
    posting_plan,
    stat_name,
    stat_range,
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
        capped = ""
        if abs((amount - before) - op.delta) > 1e-9:
            # 到上下限截断：与地区指标一样注明，免得只见「+0」而不知其故
            edge = f"上限 {row.maximum:g}" if op.delta > 0 and row.maximum is not None else f"下限 {row.minimum:g}" if row.minimum is not None else "限"
            capped = f"（已至{edge}，原拟 {signed_number(op.delta)}）"
        facts.append(
            Fact(kind="resource", text=f"{row.name} {signed_number(amount - before)}{capped}{suffix}", entity=op.resource, before=before, after=amount)
        )
    elif isinstance(op, AddLedger):
        replaced = next((row for row in updated.ledger if op.id and row.id == op.id), None)
        if op.id:
            updated.ledger = [row for row in updated.ledger if row.id != op.id]
        updated.ledger.append(
            LedgerEntry(
                id=op.id or _next_id(updated, "ledger"),
                name=op.name,
                category=op.category,
                amount=op.amount,
                kind=op.kind,
                turns=op.turns,
                reason=op.reason,
                cancellable=op.cancellable,
            )
        )
        if replaced is not None:
            text = f"账目改定：{op.name}（{signed_number(replaced.amount)} → {signed_number(op.amount)}）"
        else:
            text = f"新增账目：{op.name}"
        facts.append(Fact(kind="ledger", text=text, entity=op.category))
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
        text = f"{character.name}任{office_name}"
        army_id = office_army(updated, op.office)
        army = updated.armies[army_id] if army_id else None
        if army is not None and army.commander != op.character:
            army.commander = op.character
            text += f"，兼统{army.name}"
        station = appointment_station(updated, op.office, op.dispatch)
        if station is not None:
            text += _posting(updated, character, station, op.dispatch, army)[0]
        facts.append(Fact(kind="appointment", text=text, entity=op.character, after=op.office))
    elif isinstance(op, Dismiss):
        character = updated.characters[op.character]
        held = [office for office in held_offices(character) if office != op.office]
        character.offices = held
        if character.office == op.office or character.office not in held:
            character.office = held[0] if held else None
        updated.offices[op.office].holder = None
        office_name = updated.offices[op.office].name
        text = f"{character.name}免{office_name}"
        army_id = office_army(updated, op.office)
        if army_id and updated.armies[army_id].commander == op.character:
            updated.armies[army_id].commander = None
            text += f"，罢统{updated.armies[army_id].name}"
        facts.append(Fact(kind="appointment", text=text, entity=op.character, before=op.office))
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
    elif isinstance(op, (MoveArmy, March)):
        army = updated.armies[op.army]
        route_text = _start_transit(updated, army, op.to_region, army.faction)
        origin = updated.regions[army.region].name
        escort = _escort_commander(updated, army) if op.with_commander else ""
        verb = "出征" if isinstance(op, March) else "开拔"
        # 兵已打光的空番号照常归营（统帅随行），但不再当作一支军队开拔报给玩家
        facts.append(
            Fact(
                kind="movement",
                text=f"{army.name}自{origin}{verb}，赴{route_text}{escort}",
                entity=op.army,
                before=army.region,
                after=op.to_region,
                internal=army.strength <= 0,
            )
        )
    elif isinstance(op, Detach):
        facts.append(_detach(updated, op))
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
        region.stats[op.stat] = clamp_stat(updated, op.stat, before + op.delta)
        change = region.stats[op.stat] - before
        bounds = stat_range(updated, op.stat)
        capped = ""
        if bounds is not None and abs(change - op.delta) > 1e-9:
            edge = f"上限 {bounds[1]:g}" if op.delta > 0 else f"下限 {bounds[0]:g}"
            capped = f"（已至{edge}，原拟 {signed_number(op.delta)}）"
        facts.append(
            Fact(
                kind="region",
                text=f"{region.name}{stat_name(updated, op.stat)} {signed_number(change)}{capped}{suffix}",
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
        _record_intel_source(updated, op.key, op.value, cause)
        facts.append(Fact(kind="flag", text=f"旗标改变：{op.key}", before=before, after=op.value, key=op.key, internal=True))
    elif isinstance(op, Remember):
        # 记忆里写 {when}：填入此事实际发生的那一季（奏疏可在数季之内批答、条款可延期，写死年月常常不对）
        fact_text = op.fact.replace("{when}", updated.calendar.label or "") if "{when}" in op.fact else op.fact
        updated.characters[op.character].memories.append(fact_text)
        name = updated.characters[op.character].name
        facts.append(Fact(kind="memory", text=f"{name}记下：{fact_text}", entity=op.character, after=fact_text, internal=True))
    elif isinstance(op, AdjustArmy):
        army = updated.armies[op.army]
        before = army.strength
        army.strength = max(0.0, before + op.delta)
        # 已覆灭之军（0 兵）再折损：数目不变，不出「兵力 +0（0→0）」这样的空事实
        if army.strength != before or (not op.delta and not op.quality_delta and before > 0):
            text = reason if reason and army.name in reason else f"{army.name}兵力 {signed_number(army.strength - before)}{suffix}"
            facts.append(Fact(kind="army", text=text, entity=op.army, before=before, after=army.strength))
        if op.quality_delta:
            quality_before = army.quality
            army.quality = round(max(0.1, min(3.0, quality_before + op.quality_delta)), 3)
            text = f"{army.name}素质 {quality_before:g}→{army.quality:g}{suffix}"
            facts.append(Fact(kind="army_quality", text=text, entity=op.army, before=quality_before, after=army.quality))
    elif isinstance(op, SetCharacterFaction):
        character = updated.characters[op.character]
        before = character.faction
        character.faction = op.faction
        faction_name = updated.factions[op.faction].name
        facts.append(Fact(kind="character_faction", text=f"{character.name}改属{faction_name}", entity=op.character, before=before, after=op.faction))
    elif isinstance(op, MergeArmy):
        source = updated.armies[op.army]
        target = updated.armies[op.into]
        moved = source.strength
        before = target.strength
        if moved > 0:
            total = before + moved
            target.quality = round((target.quality * before + source.quality * moved) / total, 3) if total else target.quality
            target.strength = total
        source.strength = 0.0
        source.in_transit = None
        source.posture = None
        text = f"{source.name}{format_number(moved)}并入{target.name}{suffix}"
        facts.append(Fact(kind="army", text=text, entity=op.into, before=before, after=target.strength))
        facts.append(Fact(kind="army", text=f"{source.name}归建", entity=op.army, before=moved, after=0.0, internal=True))
    elif isinstance(op, SetArmyFaction):
        army = updated.armies[op.army]
        before = army.faction
        army.faction = op.faction
        faction_name = updated.factions[op.faction].name
        facts.append(Fact(kind="army_faction", text=f"{army.name}改隶{faction_name}", entity=op.army, before=before, after=op.faction))
    elif isinstance(op, SetArmyPosture):
        from simcore.domain.military import posture_label

        army = updated.armies[op.army]
        before = army.posture
        army.posture = op.posture
        where = updated.regions[army.region].name if army.region in updated.regions else army.region
        facts.append(
            Fact(
                kind="army_posture",
                text=f"{army.name}（{where}）改取{posture_label(op.posture)}之势{suffix}",
                entity=op.army,
                before=before,
                after=op.posture,
            )
        )
    elif isinstance(op, SetArmyCommander):
        army = updated.armies[op.army]
        before = army.commander
        army.commander = op.character
        text = f"{army.name}易帅，由{entity_name(updated, op.character)}统领" if op.character else f"罢{army.name}统帅"
        quiet = False
        if op.character:
            if before == op.character:
                text = f"{army.name}仍由{entity_name(updated, op.character)}统领"
            suffix, kind = _posting(updated, updated.characters[op.character], army_station(army), op.dispatch, army)
            text += suffix
            # 同一人且未因此启程：前一条任命（兼统）已说明，不再重复一条事实
            quiet = before == op.character and kind != "dispatch"
        facts.append(Fact(kind="army_commander", text=text, entity=op.army, before=before, after=op.character, internal=quiet))
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
        for effect in coalesce_postings(list(option.effects), updated):
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


INTEL_PREFIX = "intel:"
INTEL_SOURCES_PREFIX = "_internal:intel_sources:"


def _record_intel_source(state: WorldState, key: str, value: Any, cause: str | None) -> None:
    """情报旗标（intel:<resource>[:<来源>]）记下来源，供风闻精度说明与叠加（visibility.intel_precision）。"""
    if not key.startswith(INTEL_PREFIX):
        return
    resource = key[len(INTEL_PREFIX):].split(":", 1)[0]
    if not resource:
        return
    slot = f"{INTEL_SOURCES_PREFIX}{resource}"
    if value in (None, False):
        state.flags.pop(slot, None)
        return
    sources = [str(item) for item in state.flags.get(slot) or []]
    label = (cause or "").strip() or "密报"
    if label not in sources:
        sources.append(label)
    state.flags[slot] = sources


def _escort_commander(state: WorldState, army: Any) -> str:
    """统帅与军队同驻一地（且不在途）时随军同行：复制军队的行程，返回事实后缀「，统帅某某随军」。"""
    commander = state.characters.get(army.commander) if army.commander else None
    if commander is None or commander.status != "alive" or commander.in_transit or commander.region != army.region:
        return ""
    commander.in_transit = dict(army.in_transit or {})
    return f"，统帅{commander.name}随军"


def _posting(state: WorldState, character: Any, station: str, dispatch: bool, army: Any = None) -> tuple[str, str]:
    """任帅 / 任有任所的官职之后：按 rules.posting_plan 启程赴任或遥领，返回 (事实后缀「；已启程赴任（…）」/「；遥领，不赴任（…）」, kind)。

    启程赴任时，留在原地的所统军队按 rules.left_behind_armies 的规则真正解除其统帅（官职所统之军除外，仍兼统），
    与回执文字一致。
    """
    plan = posting_plan(state, character, station, dispatch, army=army)
    if plan["kind"] == "dispatch":
        path, turns = plan["route"]
        for army_id in plan.get("release", []):
            if army_id in state.armies and state.armies[army_id].commander == character.id:
                state.armies[army_id].commander = None
        character.in_transit = {"to_region": station, "eta_turn": state.turn + turns, "from_region": character.region, "route": path}
    return f"；{plan['text']}", str(plan["kind"])


def detach_name(state: WorldState, army: Any, wanted: str | None) -> str:
    """分兵新军的名字：给定名，或「原军名偏师」；重名时加序号。"""
    base = (wanted or "").strip() or f"{army.name}偏师"
    names = {row.name for row in state.armies.values()}
    if base not in names:
        return base
    index = 2
    while f"{base}{index}" in names:
        index += 1
    return f"{base}{index}"


def _detach(state: WorldState, op: Detach) -> Fact:
    """分兵：新军 id 为「原军 id_d流水号」（全局流水号），兵力自原军扣出；去向不同则整段出发（同 move_army 寻路）。"""
    source = state.armies[op.army]
    sequence = int(state.flags.get("_internal:id_seq", 0)) + 1
    state.flags["_internal:id_seq"] = sequence
    army_id = f"{source.id}_d{sequence}"
    name = detach_name(state, source, op.new_army_name)
    source.strength = source.strength - op.strength
    detachment = Army(
        id=army_id,
        name=name,
        faction=source.faction,
        region=source.region,
        strength=op.strength,
        loyal_to=source.loyal_to,
        quality=source.quality,
    )
    state.armies[army_id] = detachment
    origin = state.regions[source.region].name
    text = f"{source.name}分兵 {format_number(op.strength)}（留 {format_number(source.strength)}），编为{name}"
    if op.to_region != source.region:
        text += f"，自{origin}开拔，赴{_start_transit(state, detachment, op.to_region, detachment.faction)}"
    else:
        text += f"，就地分屯{origin}"
    if op.commander:
        leader = state.characters[op.commander]
        detachment.commander = leader.id
        if not leader.in_transit and leader.region == source.region:
            # 与原军同驻：随新军同行；留下的所统之军按 left_behind_armies 处置
            kept, released = left_behind_armies(state, leader, detachment) if op.to_region != source.region else ([], [])
            for row in released:
                row.commander = None
            if detachment.in_transit:
                leader.in_transit = dict(detachment.in_transit)
            text += f"，由{leader.name}统领" + ("随行" if detachment.in_transit else "") + left_behind_text(kept, released)
        else:
            suffix, _ = _posting(state, leader, army_station(detachment), True, detachment)
            text += f"，由{leader.name}统领{suffix}"
    else:
        text += "，暂无主帅（副将摄事）"
    return Fact(kind="movement", text=text, entity=army_id, before=source.region, after=op.to_region)


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
