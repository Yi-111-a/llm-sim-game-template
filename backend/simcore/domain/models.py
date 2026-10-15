from __future__ import annotations

from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


_ATOMIC: frozenset[type] = frozenset({str, int, float, bool, type(None)})


def clone_data(value: Any) -> Any:
    kind = type(value)
    if kind in _ATOMIC:
        return value
    if kind is dict:
        return {key: item if type(item) in _ATOMIC else clone_data(item) for key, item in value.items()}
    if kind is list:
        return [item if type(item) in _ATOMIC else clone_data(item) for item in value]
    if isinstance(value, BaseModel):
        return clone(value)
    if isinstance(value, dict):
        return {key: clone_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clone_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(clone_data(item) for item in value)
    if isinstance(value, set):
        return {clone_data(item) for item in value}
    return value


_M = TypeVar("_M", bound=BaseModel)


def clone(model: _M) -> _M:
    """领域模型的快速深拷贝（结果与 model_copy(deep=True) 相等，速度约快 3 倍）。

    领域模型只含 pydantic 模型、dict、list 与不可变标量，无 extra / private 字段，可以逐字段递归复制。
    """
    cls = type(model)
    copied = cls.__new__(cls)
    object.__setattr__(copied, "__dict__", {key: clone_data(item) for key, item in model.__dict__.items()})
    object.__setattr__(copied, "__pydantic_fields_set__", set(model.__pydantic_fields_set__))
    object.__setattr__(copied, "__pydantic_extra__", None)
    object.__setattr__(copied, "__pydantic_private__", None)
    return copied


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


INTERNAL_FACT_KINDS = frozenset({"flag", "counter", "memory", "effect_skipped"})


class Fact(DomainModel):
    """回合事实。text 必须自足（带人物/地区/军队等显示名，不含内部 id）。

    internal=True 的事实（旗标、计数、人物记忆等引擎簿记）只留在日志里的 TurnReport 供调试，
    不进入简报、纪事、叙事输入与前端的事实列表。key 为涉及的指标/心态/旗标键（供隐藏规则判断）。
    """

    kind: str
    text: str
    entity: str | None = None
    before: Any = None
    after: Any = None
    key: str | None = None
    internal: bool = False


def is_internal_fact(fact: Fact | dict[str, Any]) -> bool:
    """兼容旧存档：旧报告中的事实没有 internal 字段，按 kind 判断。"""
    if isinstance(fact, Fact):
        return fact.internal or fact.kind in INTERNAL_FACT_KINDS
    return bool(fact.get("internal")) or fact.get("kind") in INTERNAL_FACT_KINDS


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
