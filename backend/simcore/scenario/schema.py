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


class CalendarEra(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_turn: int = Field(ge=1)
    name: str
    start_year: int = Field(default=1, ge=1)


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
