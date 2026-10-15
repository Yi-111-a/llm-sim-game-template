from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Calendar(DomainModel):
    year: int
    month: int = 1
    season: str = "春"
    label: str = ""


class Resource(DomainModel):
    id: str
    name: str
    amount: float
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""


class Office(DomainModel):
    id: str
    name: str
    requires: dict[str, Any] = Field(default_factory=dict)
    holder: str | None = None


class Faction(DomainModel):
    id: str
    name: str
    relations: dict[str, float] = Field(default_factory=dict)


class Region(DomainModel):
    id: str
    name: str
    x: float = 0
    y: float = 0
    polygon: list[list[float]] = Field(default_factory=list)
    adjacency: dict[str, int] = Field(default_factory=dict)
    stats: dict[str, float] = Field(default_factory=dict)


class Character(DomainModel):
    id: str
    name: str
    faction: str | None = None
    office: str | None = None
    region: str
    traits: dict[str, float] = Field(default_factory=dict)
    persona: str = ""
    speech_style: str = ""
    knowledge: list[str] = Field(default_factory=list)
    status: Literal["alive", "dead", "imprisoned", "exiled", "retired"] = "alive"
    in_transit: dict[str, Any] | None = None
    memories: list[str] = Field(default_factory=list)


class Army(DomainModel):
    id: str
    name: str
    faction: str | None = None
    region: str
    strength: float = 0
    in_transit: dict[str, Any] | None = None


class LedgerEntry(DomainModel):
    id: str
    name: str
    category: str
    amount: float
    kind: Literal["one_off", "recurring"]
    turns: int | None = None
    reason: str = ""


class Project(DomainModel):
    id: str
    name: str
    region: str
    cost_per_turn: dict[str, float]
    turns: int
    progress: int = 0
    on_complete: list[dict[str, Any]] = Field(default_factory=list)
    stalled: bool = False


class StandingOrder(DomainModel):
    id: str
    text: str
    ops: list[dict[str, Any]]
    until_turn: int | None = None


class PendingClause(DomainModel):
    id: str
    ops: list[dict[str, Any]]
    due_turn: int
    source: str = "edict"


class WorldState(DomainModel):
    scenario_id: str
    turn: int
    calendar: Calendar
    resources: dict[str, Resource]
    regions: dict[str, Region]
    characters: dict[str, Character]
    offices: dict[str, Office] = Field(default_factory=dict)
    factions: dict[str, Faction] = Field(default_factory=dict)
    armies: dict[str, Army] = Field(default_factory=dict)
    ledger: list[LedgerEntry] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    standing_orders: list[StandingOrder] = Field(default_factory=list)
    pending_clauses: list[PendingClause] = Field(default_factory=list)
    flags: dict[str, Any] = Field(default_factory=dict)
    rng_state: list[Any] | None = None


class Fact(DomainModel):
    kind: str
    text: str
    entity: str | None = None
    before: Any = None
    after: Any = None


class LedgerLine(DomainModel):
    name: str
    amount: float
    kind: str
    resource: str
    reason: str = ""


class TurnReport(DomainModel):
    turn: int
    facts: list[Fact] = Field(default_factory=list)
    ledger_lines: list[LedgerLine] = Field(default_factory=list)
    events_fired: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    state_hash: str = ""
