from __future__ import annotations

from typing import Any

from simcore.domain.models import (
    Fact,
    LedgerEntry,
    PendingClause,
    Project,
    StandingOrder,
    WorldState,
)
from simcore.domain.ops import (
    AddLedger,
    AddStandingOrder,
    Appoint,
    CancelLedger,
    CancelStandingOrder,
    Dismiss,
    MoveArmy,
    MoveCharacter,
    Remember,
    SetCharacterStatus,
    SetFlag,
    StartProject,
    StateOp,
    parse_op,
)
from simcore.domain.rules import Rejected, validate


def apply(
    op_value: StateOp | dict[str, Any], state: WorldState
) -> tuple[WorldState, list[Fact]]:
    result = validate(op_value, state)
    if isinstance(result, Rejected):
        raise ValueError(f"{result.code}: {result.message}")
    op = result.op
    updated = state.model_copy(deep=True)
    facts: list[Fact] = []
    if op.op == "adjust_resource":
        row = updated.resources[op.resource]
        before = row.amount
        amount = before + op.delta
        if row.minimum is not None:
            amount = max(row.minimum, amount)
        if row.maximum is not None:
            amount = min(row.maximum, amount)
        row.amount = amount
        facts.append(Fact(kind="resource", text=f"{row.name}变化", entity=op.resource, before=before, after=amount))
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
        updated.ledger = [row for row in updated.ledger if row.id != op.ledger_id]
        facts.append(Fact(kind="ledger", text="已取消账目", entity=op.ledger_id))
    elif isinstance(op, Appoint):
        updated.characters[op.character].office = op.office
        updated.offices[op.office].holder = op.character
        facts.append(Fact(kind="appointment", text="任命官职", entity=op.character, after=op.office))
    elif isinstance(op, Dismiss):
        updated.characters[op.character].office = None
        updated.offices[op.office].holder = None
        facts.append(Fact(kind="appointment", text="免去官职", entity=op.character, before=op.office))
    elif isinstance(op, SetCharacterStatus):
        character = updated.characters[op.character]
        before = character.status
        character.status = op.status
        if op.status != "alive" and character.office:
            updated.offices[character.office].holder = None
            character.office = None
        facts.append(Fact(kind="status", text=op.cause or "人物状态改变", entity=op.character, before=before, after=op.status))
    elif isinstance(op, MoveCharacter):
        character = updated.characters[op.character]
        turns = updated.regions[character.region].adjacency[op.to_region]
        character.in_transit = {"to_region": op.to_region, "eta_turn": updated.turn + turns}
        facts.append(Fact(kind="movement", text="人物开始行程", entity=op.character, after=op.to_region))
    elif isinstance(op, MoveArmy):
        army = updated.armies[op.army]
        turns = updated.regions[army.region].adjacency[op.to_region]
        army.in_transit = {"to_region": op.to_region, "eta_turn": updated.turn + turns}
        facts.append(Fact(kind="movement", text="军队开始行军", entity=op.army, after=op.to_region))
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
        facts.append(Fact(kind="project", text=f"工程立项：{op.name}", entity=op.region))
    elif op.op == "adjust_region":
        region = updated.regions[op.region]
        before = region.stats[op.stat]
        region.stats[op.stat] += op.delta
        facts.append(Fact(kind="region", text="地区指标变化", entity=op.region, before=before, after=region.stats[op.stat]))
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
        facts.append(Fact(kind="relation", text="关系变化", entity=op.a, before=before, after=after))
    elif isinstance(op, AddStandingOrder):
        updated.standing_orders.append(
            StandingOrder(
                id=_next_id(updated, "order"),
                text=op.text,
                ops=op.ops,
                until_turn=op.until_turn,
            )
        )
        facts.append(Fact(kind="standing_order", text="新增常设命令", after=op.text))
    elif isinstance(op, CancelStandingOrder):
        updated.standing_orders = [row for row in updated.standing_orders if row.id != op.id]
        facts.append(Fact(kind="standing_order", text="取消常设命令", entity=op.id))
    elif isinstance(op, SetFlag):
        before = updated.flags.get(op.key)
        updated.flags[op.key] = op.value
        facts.append(Fact(kind="flag", text=f"旗标改变：{op.key}", before=before, after=op.value))
    elif isinstance(op, Remember):
        updated.characters[op.character].memories.append(op.fact)
        facts.append(Fact(kind="memory", text="记下一条人物记忆", entity=op.character, after=op.fact))
    else:
        raise TypeError(f"Unsupported operation: {op.op}")
    return updated, facts


def queue_delayed(
    state: WorldState, ops: list[dict[str, Any]], due_turn: int, source: str = "edict"
) -> WorldState:
    updated = state.model_copy(deep=True)
    sequence = int(updated.flags.get("_internal:id_seq", 0)) + 1
    updated.flags["_internal:id_seq"] = sequence
    updated.pending_clauses.append(
        PendingClause(
            id=f"pending-{sequence:08x}",
            ops=[parse_op(op).model_dump() for op in ops],
            due_turn=due_turn,
            source=source,
        )
    )
    return updated


def _next_id(state: WorldState, prefix: str) -> str:
    sequence = int(state.flags.get("_internal:id_seq", 0)) + 1
    state.flags["_internal:id_seq"] = sequence
    return f"{prefix}-{sequence:08x}"
