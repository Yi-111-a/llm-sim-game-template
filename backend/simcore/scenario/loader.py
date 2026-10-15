from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from simcore.domain.models import (
    Army,
    Calendar,
    Character,
    Faction,
    Office,
    Region,
    Resource,
    WorldState,
)
from simcore.images.manifest import asset_path
from simcore.scenario.schema import AssetManifest, ScenarioDocument

SCENARIO_FILES = ("characters.yaml", "regions.yaml", "factions.yaml", "armies.yaml", "events.yaml", "assets.yaml")
MAX_TEXT_LENGTH = 10_000
MAX_SCENARIO_FILES = 1_000
MAX_IMAGE_DIMENSION = 4_096


def load_scenario(path: str | Path) -> dict[str, Any]:
    root = Path(path).resolve()
    scenario_path = root / "scenario.yaml"
    if not scenario_path.is_file():
        raise FileNotFoundError(f"Missing scenario.yaml in {root}")
    result: dict[str, Any] = {"root": str(root)}
    for filename in ("scenario.yaml", *SCENARIO_FILES):
        file_path = root / filename
        if file_path.exists():
            result[filename.removesuffix(".yaml")] = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
        elif filename != "scenario.yaml":
            result[filename.removesuffix(".yaml")] = []
    return result


def validate_scenario(data_or_path: dict[str, Any] | str | Path) -> list[str]:
    root: Path | None = None
    if isinstance(data_or_path, (str, Path)):
        root = Path(data_or_path).resolve()
        data, errors = _load_for_validation(root)
    else:
        data = data_or_path
        errors = []

    if root is not None and root.is_dir():
        file_count = sum(1 for item in root.rglob("*") if item.is_file())
        if file_count > MAX_SCENARIO_FILES:
            errors.append(f"场景文件数超过上限 {MAX_SCENARIO_FILES}")
        theme_path = root / "theme.css"
        if theme_path.is_file():
            css = theme_path.read_text(encoding="utf-8")
            if len(css) > MAX_TEXT_LENGTH:
                errors.append(f"theme.css 文本超过 {MAX_TEXT_LENGTH} 字符")
            if not _css_variables_only(css):
                errors.append("theme.css 只允许 :root 中的 CSS 变量声明")
        prompts_path = root / "prompts"
        if prompts_path.is_dir():
            for prompt_path in prompts_path.rglob("*.md"):
                if prompt_path.is_file() and len(prompt_path.read_text(encoding="utf-8")) > MAX_TEXT_LENGTH:
                    errors.append(f"{prompt_path.relative_to(root)} 超过 {MAX_TEXT_LENGTH} 字符")

    for field_name, field_value in data.items():
        for location, text in _text_values(field_value, field_name):
            if len(text) > MAX_TEXT_LENGTH:
                errors.append(f"{location} 文本超过 {MAX_TEXT_LENGTH} 字符")

    scenario_data = data.get("scenario", {})
    manifest_data = data.get("assets", {})
    scenario: ScenarioDocument | None = None
    manifest: AssetManifest | None = None
    try:
        scenario = ScenarioDocument.model_validate(scenario_data)
    except ValidationError as exc:
        errors.extend(_validation_errors("scenario.yaml", exc))
    try:
        manifest = AssetManifest.model_validate(manifest_data)
    except ValidationError as exc:
        errors.extend(_validation_errors("assets.yaml", exc))

    for key, rows in (
        ("characters", data.get("characters", [])),
        ("regions", data.get("regions", [])),
        ("events", data.get("events", [])),
    ):
        if not isinstance(rows, list):
            errors.append(f"{key}.yaml 顶层必须是列表")
    characters = data.get("characters", []) if isinstance(data.get("characters", []), list) else []
    regions = data.get("regions", []) if isinstance(data.get("regions", []), list) else []
    events = data.get("events", []) if isinstance(data.get("events", []), list) else []
    ids: dict[str, set[str]] = {
        "character": {str(row.get("id")) for row in characters if isinstance(row, dict)},
        "region": {str(row.get("id")) for row in regions if isinstance(row, dict)},
        "event": {str(row.get("id")) for row in events if isinstance(row, dict)},
    }
    raw_entries = manifest_data.get("entries", []) if isinstance(manifest_data, dict) else []
    if not isinstance(raw_entries, list):
        errors.append("assets.yaml entries 必须是列表")
        raw_entries = []
    entries = {
        str(entry.get("id")): entry
        for entry in raw_entries
        if isinstance(entry, dict) and entry.get("id") is not None
    }
    if len(entries) != len(raw_entries):
        errors.append("assets.yaml 含缺少 id 或重复 id 的资产")
    if manifest is not None:
        for entry in manifest.entries:
            if len(entry.size) != 2 or any(
                dimension <= 0 or dimension > MAX_IMAGE_DIMENSION for dimension in entry.size
            ):
                errors.append(f"资产 {entry.id} 尺寸必须在 1 到 {MAX_IMAGE_DIMENSION} 之间")
            if root is not None:
                try:
                    asset_path(root, entry.path)
                except ValueError:
                    errors.append(f"资产 {entry.id} 路径不能离开场景目录")

    for character_id in sorted(ids["character"]):
        asset_id = f"character.{character_id}.portrait"
        if asset_id not in entries:
            errors.append(f"人物 {character_id} 缺少必需资产 {asset_id}")
    for asset_id in (
        "ui.topbar.background",
        "ui.edict.parchment",
        "ui.court.backdrop",
        "ui.map.base",
        "ui.memorial.banner",
    ):
        if asset_id not in entries:
            errors.append(f"场景缺少必需 UI 资产 {asset_id}")
    for region_id in sorted(ids["region"]):
        asset_id = f"region.{region_id}.card"
        if asset_id not in entries:
            errors.append(f"地区 {region_id} 缺少必需资产 {asset_id}")
    for event in events:
        if not isinstance(event, dict):
            errors.append("events.yaml 含非对象事件")
            continue
        illustration = event.get("illustration")
        required_id = illustration or f"event.{event.get('id')}.illustration"
        if (illustration or event.get("kind") in ("scheduled", "conditional", "random")) and required_id not in entries:
            errors.append(f"事件 {event.get('id')} 缺少必需插图资产 {required_id}")
    known_regions = ids["region"]
    for region in regions:
        if not isinstance(region, dict):
            errors.append("regions.yaml 含非对象地区")
            continue
        for neighbor in region.get("adjacency", []):
            if isinstance(neighbor, dict) and neighbor.get("to") not in known_regions:
                errors.append(f"地区 {region.get('id')} 的邻接地区不存在：{neighbor.get('to')}")
    for character in characters:
        if not isinstance(character, dict):
            errors.append("characters.yaml 含非对象人物")
            continue
        if character.get("region") not in known_regions:
            errors.append(f"人物 {character.get('id')} 的地区不存在")
    if scenario is not None and scenario.turn_unit not in {"month", "season", "year"}:
        errors.append("turn_unit 必须为 month、season 或 year")
    if scenario is not None:
        try:
            date.fromisoformat(scenario.start_date)
        except ValueError:
            errors.append("start_date 必须是 ISO 日期")
        if scenario.manifest_version != 1:
            errors.append("不支持的场景 manifest_version")
        if not scenario.version or not scenario.requires_engine:
            errors.append("version 与 requires_engine 不能为空")
        calendar = scenario.calendar
        if len({era.from_turn for era in calendar.eras}) != len(calendar.eras):
            errors.append("calendar.eras 的 from_turn 不能重复")
        try:
            calendar.turn_format.format(era="时代", year="一", season="春")
        except (KeyError, ValueError):
            errors.append("calendar.turn_format 只能使用 era、year、season 占位符")
    if manifest is not None and manifest.manifest_version != 1:
        errors.append("不支持的资产 manifest_version")
    return errors


def _load_for_validation(root: Path) -> tuple[dict[str, Any], list[str]]:
    data: dict[str, Any] = {"root": str(root)}
    errors: list[str] = []
    for filename in ("scenario.yaml", *SCENARIO_FILES):
        path = root / filename
        key = filename.removesuffix(".yaml")
        if not path.is_file():
            if filename == "scenario.yaml":
                errors.append(f"缺少 {path}")
            data[key] = {"entries": []} if filename == "assets.yaml" else []
            continue
        try:
            data[key] = yaml.safe_load(path.read_text(encoding="utf-8"))
            if data[key] is None:
                data[key] = {"entries": []} if filename == "assets.yaml" else []
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"{filename} 无法读取：{exc}")
            data[key] = {"entries": []} if filename == "assets.yaml" else []
    return data, errors


def _validation_errors(filename: str, exc: ValidationError) -> list[str]:
    return [
        f"{filename} {'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
        for item in exc.errors()
    ]


def _text_values(value: Any, location: str) -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(location, value)]
    if isinstance(value, dict):
        return [
            item
            for key, child in value.items()
            for item in _text_values(child, f"{location}.{key}")
        ]
    if isinstance(value, list):
        return [
            item
            for index, child in enumerate(value)
            for item in _text_values(child, f"{location}[{index}]")
        ]
    return []


def _css_variables_only(css: str) -> bool:
    cleaned = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL).replace(":root", "")
    declarations = cleaned.replace("{", "").replace("}", "").split(";")
    return all(
        not item.strip()
        and not item.strip().startswith("--")
        or bool(re.fullmatch(r"\s*--[\w-]+\s*:\s*[^{};]+\s*", item))
        for item in declarations
    )


def build_initial_state(data: dict[str, Any]) -> WorldState:
    scenario = ScenarioDocument.model_validate(data["scenario"])
    start = date.fromisoformat(scenario.start_date)
    month = start.month
    season = ("春", "夏", "秋", "冬")[(month - 1) // 3]
    resources = {
        row["id"]: Resource(
            id=row["id"],
            name=row["name"],
            amount=float(row.get("initial", 0)),
            minimum=row.get("min"),
            maximum=row.get("max"),
            unit=row.get("unit", ""),
        )
        for row in scenario.resources
    }
    regions = {
        row["id"]: Region(
            id=row["id"],
            name=row["name"],
            x=row.get("map", {}).get("x", 0),
            y=row.get("map", {}).get("y", 0),
            polygon=row.get("map", {}).get("polygon", []),
            adjacency={edge["to"]: int(edge.get("travel_turns", 1)) for edge in row.get("adjacency", [])},
            stats=row.get("stats", {}),
        )
        for row in data.get("regions", [])
    }
    characters = {
        row["id"]: Character(
            id=row["id"],
            name=row["name"],
            faction=row.get("faction"),
            office=row.get("office"),
            region=row.get("region", next(iter(regions))),
            traits=row.get("traits", {}),
            persona=row.get("persona", ""),
            speech_style=row.get("speech_style", ""),
            knowledge=row.get("knowledge", []),
            status=row.get("status", "alive"),
        )
        for row in data.get("characters", [])
    }
    offices = {row["id"]: Office(**row) for row in scenario.offices}
    for character in characters.values():
        if character.office in offices:
            offices[character.office].holder = character.id
    factions = {row["id"]: Faction(**row) for row in data.get("factions", [])}
    armies = {row["id"]: Army(**row) for row in data.get("armies", [])}
    return WorldState(
        scenario_id=scenario.id,
        turn=0,
        calendar=Calendar(year=start.year, month=month, season=season, label=scenario.start_date),
        resources=resources,
        regions=regions,
        characters=characters,
        offices=offices,
        factions=factions,
        armies=armies,
    )


def scenario_json_schema() -> dict[str, Any]:
    return ScenarioDocument.model_json_schema()
