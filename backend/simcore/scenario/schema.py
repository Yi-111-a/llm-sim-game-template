from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ScenarioDocument(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    title: str
    era: str
    start_date: str
    turn_unit: str
    manifest_version: int = 1
    version: str = "0.1.0"
    requires_engine: str = ">=0.1.0"
    calendar: ScenarioCalendar = Field(default_factory=lambda: ScenarioCalendar())
    currency: str = ""
    resources: list[dict[str, Any]] = Field(default_factory=list)
    stats: list[dict[str, Any]] = Field(default_factory=list)
    offices: list[dict[str, Any]] = Field(default_factory=list)
    edict_categories: list[dict[str, Any]] = Field(default_factory=list)
    formulas: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)
    endings: list[EndingDef] = Field(default_factory=list)
    game_length: GameLength | None = None
    counters: list[CounterDef] = Field(default_factory=list)
    crisis: CrisisDef | None = None
    ai_routes: list[AiRoute] = Field(default_factory=list)
    wars: list[Any] = Field(default_factory=list)
    objectives: list[ObjectiveDef] = Field(default_factory=list)


class EndingDef(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kind: Literal["ending", "fail"] = "ending"
    title: str
    text: str = ""
    priority: float = 100
    conditions: Any = None


class GameLength(BaseModel):
    model_config = ConfigDict(extra="forbid")

    last_turn: int = Field(ge=1)
    overtime_flag: str | None = None
    overtime_last_turn: int | None = None
    fallback_ending: str | None = None
    # 延长模式（结局 extension_hint 的前提）。引擎尚未实现任何延长模式，声明了也不显示提示（见 turn.extension_hint）。
    extension_mode: str | None = None


class CounterDef(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    conditions: Any = None
    mode: Literal["streak", "count"] = "streak"


class CrisisBand(BaseModel):
    model_config = ConfigDict(extra="allow")

    max: float
    name: str


class CrisisTerm(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    label: str
    expr: str


class CrisisDef(BaseModel):
    model_config = ConfigDict(extra="allow")

    resource: str
    title: str = ""
    bands: list[CrisisBand] = Field(default_factory=list)
    terms: list[CrisisTerm] = Field(default_factory=list)


class AiRoute(BaseModel):
    model_config = ConfigDict(extra="allow")

    faction: str
    route: list[str] = Field(min_length=1)
    hold_if_outnumbered: bool = True
    armies: list[str] = Field(default_factory=list)
    conditions: Any = None


class ObjectiveDef(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    title: str
    detail: str = ""
    conditions: Any = None
    fail_conditions: Any = None


class CalendarEra(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_turn: int = Field(ge=0)
    name: str
    start_year: int = Field(default=1, ge=1)
    turn_format: str | None = None
    number_system: Literal["chinese", "arabic"] | None = None


class ScenarioCalendar(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eras: list[CalendarEra] = Field(default_factory=list)
    seasons: list[str] = Field(default_factory=lambda: ["春", "夏", "秋", "冬"], min_length=1)
    turn_format: str = "{era}{year}年{season}"
    number_system: Literal["chinese", "arabic"] = "chinese"
    pre_game_turns: int = Field(default=0, ge=0)


class AssetEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kind: str
    used_by: str
    path: str
    size: list[int]
    format: Literal["webp", "png"]
    prompt: str = ""
    negative_prompt: str = ""
    style_ref: str = "整体风格"
    seed: int | None = None
    model: str | None = None
    sha256: str | None = None
    status: Literal["missing", "placeholder", "generated", "approved"] = "missing"
    version: int = 1


class AssetManifest(BaseModel):
    model_config = ConfigDict(extra="allow")

    manifest_version: int = 1
    entries: list[AssetEntry] = Field(default_factory=list)


ScenarioDocument.model_rebuild()
