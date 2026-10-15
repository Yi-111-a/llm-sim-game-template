from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from simcore.domain.calendar import format_calendar, start_season_index
from simcore.domain.events import condition_errors
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
from simcore.domain.ops import parse_op
from simcore.images.manifest import asset_path
from simcore.scenario.schema import AssetManifest, ScenarioDocument

SCENARIO_FILES = (
    "characters.yaml",
    "regions.yaml",
    "factions.yaml",
    "armies.yaml",
    "events.yaml",
    "assets.yaml",
    "memorials.yaml",
)
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
    errors.extend(_mechanics_errors(data, scenario))
    return errors


def _mechanics_errors(data: dict[str, Any], scenario: ScenarioDocument | None) -> list[str]:
    """§1 引擎扩展字段的结构与引用检查：事件抉择、结局、计数器、危机计、AI 路线、奏疏。"""
    errors: list[str] = []

    def rows(key: str) -> list[dict[str, Any]]:
        value = data.get(key, [])
        return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []

    region_ids = {str(row.get("id")) for row in rows("regions")}
    character_ids = {str(row.get("id")) for row in rows("characters")}
    faction_ids = {str(row.get("id")) for row in rows("factions")}
    army_ids = {str(row.get("id")) for row in rows("armies")}
    resource_ids = {str(row.get("id")) for row in (scenario.resources if scenario else [])}
    office_ids = {str(row.get("id")) for row in (scenario.offices if scenario else [])}

    def check_ops(ops: Any, location: str) -> None:
        if not isinstance(ops, list):
            errors.append(f"{location} 必须是列表")
            return
        for index, op in enumerate(ops):
            try:
                parse_op(op)
            except (ValueError, TypeError) as exc:
                first = str(exc).splitlines()[0]
                errors.append(f"{location}[{index}] 不是合法操作：{first}")

    for character in rows("characters"):
        for office in [character.get("office"), *(character.get("offices") or [])]:
            if office is not None and office_ids and office not in office_ids:
                errors.append(f"人物 {character.get('id')} 的官职不存在：{office}")
    for army in rows("armies"):
        if army.get("region") not in region_ids:
            errors.append(f"军队 {army.get('id')} 的地区不存在：{army.get('region')}")
        if army.get("faction") is not None and faction_ids and army.get("faction") not in faction_ids:
            errors.append(f"军队 {army.get('id')} 的势力不存在：{army.get('faction')}")
        if army.get("commander") is not None and army.get("commander") not in character_ids:
            errors.append(f"军队 {army.get('id')} 的统帅不存在：{army.get('commander')}")
    for region in rows("regions"):
        controller = region.get("controller")
        if controller is not None and controller not in faction_ids:
            errors.append(f"地区 {region.get('id')} 的控制势力不存在：{controller}")

    seen_events: set[str] = set()
    for event in rows("events"):
        event_id = str(event.get("id"))
        location = f"事件 {event_id}"
        if event_id in seen_events:
            errors.append(f"{location} id 重复")
        seen_events.add(event_id)
        errors.extend(condition_errors(event.get("conditions"), f"{location}.conditions"))
        check_ops(event.get("effects", []) or [], f"{location}.effects")
        choices = event.get("choices") or []
        if not isinstance(choices, list):
            errors.append(f"{location}.choices 必须是列表")
            continue
        if not all(isinstance(choice, dict) for choice in choices):
            errors.append(f"{location}.choices 选项必须是对象")
            continue
        choice_ids = [str(choice.get("id") or f"option_{index + 1}") for index, choice in enumerate(choices)]
        if len(set(choice_ids)) != len(choice_ids):
            errors.append(f"{location}.choices 选项 id 重复")
        for choice in choices:
            if isinstance(choice, dict):
                errors.extend(condition_errors(choice.get("conditions"), f"{location}.choices.{choice.get('id')}.conditions"))
                check_ops(choice.get("effects", choice.get("ops", [])) or [], f"{location}.choices.{choice.get('id')}.effects")
        default = event.get("default_choice")
        if default is not None and default not in choice_ids:
            errors.append(f"{location}.default_choice 不在选项中：{default}")
        if event.get("choice_deadline") is not None and int(event.get("choice_deadline", 1)) < 1:
            errors.append(f"{location}.choice_deadline 至少为 1")

    if scenario is not None:
        ending_ids = [ending.id for ending in scenario.endings]
        if len(set(ending_ids)) != len(ending_ids):
            errors.append("endings 的 id 重复")
        for ending in scenario.endings:
            errors.extend(condition_errors(ending.conditions, f"结局 {ending.id}.conditions"))
        length = scenario.game_length
        if length is not None and length.fallback_ending and scenario.endings and length.fallback_ending not in ending_ids:
            errors.append(f"game_length.fallback_ending 不在 endings 中：{length.fallback_ending}")
        if length is not None and length.overtime_last_turn is not None and length.overtime_last_turn < length.last_turn:
            errors.append("game_length.overtime_last_turn 不能早于 last_turn")
        for counter in scenario.counters:
            errors.extend(condition_errors(counter.conditions, f"计数器 {counter.id}.conditions"))
        if scenario.crisis is not None and scenario.crisis.resource not in resource_ids:
            errors.append(f"crisis.resource 不存在：{scenario.crisis.resource}")
        for route in scenario.ai_routes:
            if faction_ids and route.faction not in faction_ids:
                errors.append(f"ai_routes 势力不存在：{route.faction}")
            for region_id in route.route:
                if region_id not in region_ids:
                    errors.append(f"ai_routes 地区不存在：{region_id}")
            for army_id in route.armies:
                if army_id not in army_ids:
                    errors.append(f"ai_routes 军队不存在：{army_id}")
        for objective in scenario.objectives:
            errors.extend(condition_errors(objective.conditions, f"目标 {objective.id}.conditions"))
            errors.extend(condition_errors(objective.fail_conditions, f"目标 {objective.id}.fail_conditions"))
        for resource in scenario.resources:
            if resource.get("hidden") and float(resource.get("estimate_width", 0) or 0) < 0:
                errors.append(f"资源 {resource.get('id')} 的 estimate_width 不能为负")

    memorials = data.get("memorials", [])
    if memorials and not isinstance(memorials, list):
        errors.append("memorials.yaml 顶层必须是列表")
        memorials = []
    memorial_ids: set[str] = set()
    for item in memorials or []:
        if not isinstance(item, dict) or not item.get("id"):
            errors.append("memorials.yaml 含缺少 id 的条目")
            continue
        location = f"奏疏 {item['id']}"
        if item["id"] in memorial_ids:
            errors.append(f"{location} id 重复")
        memorial_ids.add(item["id"])
        if not item.get("title"):
            errors.append(f"{location} 缺少 title")
        if item.get("region") is not None and item.get("region") not in region_ids:
            errors.append(f"{location} 的地区不存在：{item.get('region')}")
        errors.extend(condition_errors(item.get("conditions"), f"{location}.conditions"))
        for advisor in item.get("advisors", []) or []:
            if not isinstance(advisor, dict) or advisor.get("character") not in character_ids:
                errors.append(f"{location} 的谏言人不存在：{advisor.get('character') if isinstance(advisor, dict) else advisor}")
            elif advisor.get("stance", "neutral") not in {"support", "oppose", "neutral"}:
                errors.append(f"{location} 的谏言立场无效：{advisor.get('stance')}")
        option_ids = [str(option.get("id")) for option in item.get("options", []) or [] if isinstance(option, dict)]
        if not option_ids:
            errors.append(f"{location} 至少需要一个选项")
        if len(set(option_ids)) != len(option_ids):
            errors.append(f"{location} 选项 id 重复")
        for option in item.get("options", []) or []:
            if isinstance(option, dict):
                check_ops(option.get("ops", []) or [], f"{location}.options.{option.get('id')}.ops")
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
            hidden=bool(row.get("hidden", False)),
            estimate_width=float(row.get("estimate_width", 0) or 0),
        )
        for row in scenario.resources
    }
    factions = {row["id"]: Faction(**row) for row in data.get("factions", []) or []}
    rules: dict[str, Any] = dict(scenario.settings)
    rules.setdefault("max_concurrent_offices", 1)
    if factions:
        rules.setdefault("player_faction", next(iter(factions)))
    main_faction = rules.get("player_faction")
    regions = {
        row["id"]: Region(
            id=row["id"],
            name=row["name"],
            x=row.get("map", {}).get("x", 0),
            y=row.get("map", {}).get("y", 0),
            polygon=row.get("map", {}).get("polygon", []),
            adjacency={edge["to"]: int(edge.get("travel_turns", 1)) for edge in row.get("adjacency", [])},
            stats=row.get("stats", {}),
            controller=row.get("controller", main_faction),
        )
        for row in data.get("regions", [])
    }
    characters = {
        row["id"]: Character(
            id=row["id"],
            name=row["name"],
            faction=row.get("faction"),
            office=row.get("office") or next(iter(row.get("offices") or []), None),
            offices=_initial_offices(row),
            bloc=row.get("bloc"),
            title=row.get("title", ""),
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
        for office_id in character.offices:
            if office_id in offices:
                offices[office_id].holder = character.id
    armies = {row["id"]: Army(**row) for row in data.get("armies", []) or []}
    calendar_config = scenario.calendar.model_dump(mode="json", exclude_none=True)
    label = scenario.start_date
    if scenario.calendar.eras:
        label = format_calendar(
            0,
            season,
            calendar_config,
            start_season_index(data["scenario"]),
            fallback_year=start.year,
        )
    return WorldState(
        scenario_id=scenario.id,
        turn=0,
        calendar=Calendar(year=start.year, month=month, season=season, label=label),
        resources=resources,
        regions=regions,
        characters=characters,
        offices=offices,
        factions=factions,
        armies=armies,
        rules=rules,
    )


def _initial_offices(row: dict[str, Any]) -> list[str]:
    offices = [str(item) for item in row.get("offices") or []]
    if row.get("office") and row["office"] not in offices:
        offices.insert(0, str(row["office"]))
    return offices


def scenario_json_schema() -> dict[str, Any]:
    return ScenarioDocument.model_json_schema()
