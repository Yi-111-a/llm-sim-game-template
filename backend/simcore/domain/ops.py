from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class OpModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdjustResource(OpModel):
    op: Literal["adjust_resource"]
    resource: str
    delta: float
    reason: str = ""


class AddLedger(OpModel):
    op: Literal["add_ledger"]
    name: str
    category: str
    amount: float
    kind: Literal["one_off", "recurring"]
    turns: int | None = None
    reason: str = ""


class CancelLedger(OpModel):
    op: Literal["cancel_ledger"]
    ledger_id: str


class Appoint(OpModel):
    op: Literal["appoint"]
    character: str
    office: str


class Dismiss(OpModel):
    op: Literal["dismiss"]
    character: str
    office: str


class SetCharacterStatus(OpModel):
    op: Literal["set_character_status"]
    character: str
    status: Literal["alive", "dead", "imprisoned", "exiled", "retired"]
    cause: str = ""


class MoveCharacter(OpModel):
    op: Literal["move_character"]
    character: str
    to_region: str


class MoveArmy(OpModel):
    op: Literal["move_army"]
    army: str
    to_region: str


class StartProject(OpModel):
    op: Literal["start_project"]
    name: str
    region: str
    cost_per_turn: dict[str, float]
    turns: int
    on_complete: list[dict[str, Any]] = Field(default_factory=list)


class AdjustRegion(OpModel):
    op: Literal["adjust_region"]
    region: str
    stat: str
    delta: float


class AdjustRelation(OpModel):
    op: Literal["adjust_relation"]
    a: str
    b: str
    delta: float


class AddStandingOrder(OpModel):
    op: Literal["add_standing_order"]
    text: str
    ops: list[dict[str, Any]]
    until_turn: int | None = None


class CancelStandingOrder(OpModel):
    op: Literal["cancel_standing_order"]
    id: str


class SetFlag(OpModel):
    op: Literal["set_flag"]
    key: str
    value: Any


class Remember(OpModel):
    op: Literal["remember"]
    character: str
    fact: str


StateOp: TypeAlias = Annotated[
    AdjustResource
    | AddLedger
    | CancelLedger
    | Appoint
    | Dismiss
    | SetCharacterStatus
    | MoveCharacter
    | MoveArmy
    | StartProject
    | AdjustRegion
    | AdjustRelation
    | AddStandingOrder
    | CancelStandingOrder
    | SetFlag
    | Remember,
    Field(discriminator="op"),
]

STATE_OP_ADAPTER = TypeAdapter(StateOp)


def parse_op(value: StateOp | dict[str, Any]) -> StateOp:
    if isinstance(value, BaseModel):
        return value
    return STATE_OP_ADAPTER.validate_python(value)
