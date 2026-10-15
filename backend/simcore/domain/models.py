from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    hidden: bool = False
    estimate_width: float = 0


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
    controller: str | None = None


class Character(DomainModel):
    id: str
    name: str
    faction: str | None = None
    office: str | None = None
    offices: list[str] = Field(default_factory=list)
    bloc: str | None = None
    title: str = ""
    region: str
    traits: dict[str, float] = Field(default_factory=dict)
    persona: str = ""
    speech_style: str = ""
    knowledge: list[str] = Field(default_factory=list)
    status: Literal["alive", "dead", "imprisoned", "exiled", "retired"] = "alive"
    in_transit: dict[str, Any] | None = None
    memories: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _sync_offices(cls, data: Any) -> Any:
        """旧存档只有主职 office：构造时补齐 offices；两者此后由 apply 同步维护。"""
        if isinstance(data, dict) and data.get("office") and not data.get("offices"):
            data = {**data, "offices": [data["office"]]}
        return data


class Army(DomainModel):
    id: str
    name: str
    faction: str | None = None
    region: str
    strength: float = 0
    in_transit: dict[str, Any] | None = None
    commander: str | None = None
    loyal_to: str | None = None
    quality: float = 1.0


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


class ChoiceOption(DomainModel):
    id: str
    text: str
    effects: list[dict[str, Any]] = Field(default_factory=list)
    conditions: Any = None


class PendingChoice(DomainModel):
    id: str
    event_id: str
    title: str
    text: str = ""
    options: list[ChoiceOption]
    default_option: str
    created_turn: int
    deadline_turn: int


class Outcome(DomainModel):
    kind: Literal["ending", "fail"]
    id: str
    title: str
    text: str = ""
    turn: int


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
    counters: dict[str, int] = Field(default_factory=dict)
    flag_turns: dict[str, int] = Field(default_factory=dict)
    pending_choices: list[PendingChoice] = Field(default_factory=list)
    outcome: Outcome | None = None
    rules: dict[str, Any] = Field(default_factory=dict)


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
    battles: list[dict[str, Any]] = Field(default_factory=list)
    choices_opened: list[dict[str, Any]] = Field(default_factory=list)
    outcome: Outcome | None = None
    crisis: dict[str, Any] = Field(default_factory=dict)
    state_hash: str = ""
