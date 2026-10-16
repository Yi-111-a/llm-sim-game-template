from __future__ import annotations

import ast
import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from simcore.domain.calendar import format_calendar, start_season_index
from simcore.domain.difficulty import apply_difficulty, difficulty_errors, resolve_difficulty
from simcore.domain.events import condition_errors
from simcore.domain.models import (
    Army,
    Calendar,
    Character,
    Faction,
    LedgerEntry,
    Office,
    Region,
    Resource,
    WorldState,
    clone_data,
)
from simcore.domain.ops import parse_op
from simcore.domain.rules import parse_stat_range
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
# 可选文件（第 9 轮）：缺失时不写入解析结果，旧剧本的内容哈希不变。
OPTIONAL_SCENARIO_FILES = (
    "policy_cards.yaml",
    "tutorial.yaml",
)
MAX_TEXT_LENGTH = 10_000
MAX_SCENARIO_FILES = 1_000
MAX_IMAGE_DIMENSION = 4_096


_YAML_LOADER: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_SCENARIO_CACHE: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}


def yaml_load(text: str) -> Any:
    """safe_load 的快速版：有 libyaml 时用 C 实现（语义同 SafeLoader）。"""
    return yaml.load(text, Loader=_YAML_LOADER)  # noqa: S506 - 只用 SafeLoader / CSafeLoader


def _files_signature(root: Path) -> tuple[Any, ...]:
    signature: list[tuple[str, int | None, int | None]] = []
    for filename in ("scenario.yaml", *SCENARIO_FILES, *OPTIONAL_SCENARIO_FILES):
        try:
            stat = (root / filename).stat()
            signature.append((filename, stat.st_mtime_ns, stat.st_size))
        except OSError:
            signature.append((filename, None, None))
    return tuple(signature)


def load_scenario(path: str | Path) -> dict[str, Any]:
    """读取场景目录。解析结果按文件 mtime/size 缓存；每次返回独立的深拷贝，调用方可随意修改。"""
    root = Path(path).resolve()
    scenario_path = root / "scenario.yaml"
    if not scenario_path.is_file():
        raise FileNotFoundError(f"Missing scenario.yaml in {root}")
    signature = _files_signature(root)
    cached = _SCENARIO_CACHE.get(str(root))
    if cached is not None and cached[0] == signature:
        return clone_data(cached[1])
    result: dict[str, Any] = {"root": str(root)}
    for filename in ("scenario.yaml", *SCENARIO_FILES):
        file_path = root / filename
        if file_path.exists():
            result[filename.removesuffix(".yaml")] = yaml_load(file_path.read_text(encoding="utf-8")) or {}
        elif filename != "scenario.yaml":
            result[filename.removesuffix(".yaml")] = []
    for filename in OPTIONAL_SCENARIO_FILES:
        file_path = root / filename
        if file_path.is_file():
            result[filename.removesuffix(".yaml")] = yaml_load(file_path.read_text(encoding="utf-8")) or []
    _SCENARIO_CACHE[str(root)] = (signature, result)
    return clone_data(result)


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

    requirements = _asset_requirements(data.get("scenario", {}))
    character_pattern = requirements["character"]
    if character_pattern:
        for character_id in sorted(ids["character"]):
            asset_id = character_pattern.format(id=character_id)
            if asset_id not in entries:
                errors.append(f"人物 {character_id} 缺少必需资产 {asset_id}")
    for asset_id in requirements["ui"]:
        if asset_id not in entries:
            errors.append(f"场景缺少必需 UI 资产 {asset_id}")
    region_pattern = requirements["region"]
    if region_pattern:
        for region_id in sorted(ids["region"]):
            asset_id = region_pattern.format(id=region_id)
            if asset_id not in entries:
                errors.append(f"地区 {region_id} 缺少必需资产 {asset_id}")
    for ending in data.get("scenario", {}).get("endings", []) or []:
        illustration = ending.get("illustration") if isinstance(ending, dict) else None
        if illustration and illustration not in entries:
            errors.append(f"结局 {ending.get('id')} 缺少插图资产 {illustration}")
    for event in events:
        if not isinstance(event, dict):
            errors.append("events.yaml 含非对象事件")
            continue
        illustration = event.get("illustration")
        required_id = illustration or f"event.{event.get('id')}.illustration"
        if (illustration or event.get("kind") in ("scheduled", "conditional", "random")) and required_id not in entries:
            errors.append(f"事件 {event.get('id')} 缺少必需插图资产 {required_id}")
    known_regions = ids["region"]
    for row in (scenario_data.get("stats", []) if isinstance(scenario_data, dict) else []) or []:
        if isinstance(row, dict) and "range" in row and parse_stat_range(row.get("range")) is None:
            errors.append(f"地区指标 {row.get('id')} 的 range 须为 [下限, 上限] 两个数且下限 ≤ 上限")
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

    def check_memory(raw: Any, location: str) -> None:
        """奏疏选项 / 事件选项的 memory：{character, text} 或其列表；人物须存在、text 非空。"""
        if raw is None:
            return
        rows = raw if isinstance(raw, list) else [raw]
        for row in rows:
            if not isinstance(row, dict):
                errors.append(f"{location}.memory 必须是 {{character, text}} 对象或其列表")
            elif row.get("character") not in character_ids:
                errors.append(f"{location}.memory 人物不存在：{row.get('character')}")
            elif not str(row.get("text") or "").strip():
                errors.append(f"{location}.memory 缺少 text")

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

    for office in scenario.offices if scenario else []:
        if not isinstance(office, dict):
            continue
        if office.get("army") is not None and office.get("army") not in army_ids:
            errors.append(f"官职 {office.get('id')} 的所统军队不存在：{office.get('army')}")
        if office.get("seat") is not None and office.get("seat") not in region_ids:
            errors.append(f"官职 {office.get('id')} 的任所地区不存在：{office.get('seat')}")
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
                check_memory(choice.get("memory"), f"{location}.choices.{choice.get('id')}")
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
        errors.extend(initial_ledger_errors((data.get("scenario", {}) or {}).get("ledger"), resource_ids))
        errors.extend(upkeep_errors((scenario.formulas or {}).get("upkeep"), resource_ids, faction_ids, army_ids))
        errors.extend(difficulty_errors(data.get("scenario", {}) or {}, resource_ids, seen_events))
        errors.extend(player_ops_errors((data.get("scenario", {}) or {}).get("settings") or {}))

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
            elif advisor.get("requires") is not None:
                errors.extend(_advisor_requires_errors(advisor, location, region_ids))
        option_ids = [str(option.get("id")) for option in item.get("options", []) or [] if isinstance(option, dict)]
        for advisor in item.get("advisors", []) or []:
            if isinstance(advisor, dict) and advisor.get("option") is not None and str(advisor.get("option")) not in option_ids:
                errors.append(f"{location} 的谏言人 {advisor.get('character')} 的 option 不在选项中：{advisor.get('option')}")
        if item.get("proposal") is not None and str(item.get("proposal")) not in option_ids:
            errors.append(f"{location} 的 proposal 不在选项中：{item.get('proposal')}")
        if not option_ids:
            errors.append(f"{location} 至少需要一个选项")
        if len(set(option_ids)) != len(option_ids):
            errors.append(f"{location} 选项 id 重复")
        for option in item.get("options", []) or []:
            if isinstance(option, dict):
                check_ops(option.get("ops", []) or [], f"{location}.options.{option.get('id')}.ops")
                check_memory(option.get("memory"), f"{location}.options.{option.get('id')}")
    errors.extend(round9_errors(data, region_ids))
    return errors


def round9_errors(data: dict[str, Any], region_ids: set[str]) -> list[str]:
    """第 9 轮：settings.agenda / capital_region / courier / autonomy / postures、地区 courier_days、政令卡、新手引导。"""
    from simcore.domain.cards import card_errors

    errors: list[str] = []
    scenario = data.get("scenario") if isinstance(data.get("scenario"), dict) else {}
    settings = scenario.get("settings") if isinstance(scenario.get("settings"), dict) else {}
    agenda = settings.get("agenda")
    if agenda is not None:
        if not isinstance(agenda, dict):
            errors.append("settings.agenda 须为对象 {audience_per_season, debate_per_season, remote_reply?}")
        else:
            for key in sorted(set(agenda) - {"audience_per_season", "debate_per_season", "remote_reply"}):
                errors.append(f"settings.agenda 含未知键：{key}")
            for key in ("audience_per_season", "debate_per_season"):
                value = agenda.get(key)
                if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                    errors.append(f"settings.agenda.{key} 须为非负整数")
            if agenda.get("remote_reply") not in (None, "next_season", "immediate"):
                errors.append("settings.agenda.remote_reply 须为 next_season 或 immediate")
    capital = settings.get("capital_region")
    if capital is not None and str(capital) not in region_ids:
        errors.append(f"settings.capital_region 地区不存在：{capital}")
    courier = settings.get("courier")
    if courier is not None:
        if not isinstance(courier, dict) or any(key != "late_threshold_days" for key in courier):
            errors.append("settings.courier 只支持 {late_threshold_days}")
        elif not isinstance(courier.get("late_threshold_days", 60), (int, float)):
            errors.append("settings.courier.late_threshold_days 须为数")
    autonomy = settings.get("autonomy")
    if autonomy is not None:
        if not isinstance(autonomy, dict) or not (autonomy.get("formula") or autonomy.get("by_order")):
            errors.append("settings.autonomy 须为 {formula, by_order?, outcomes?, levels?}")
        else:
            for key in sorted(set(autonomy) - {"formula", "by_order", "outcomes", "levels"}):
                errors.append(f"settings.autonomy 含未知键：{key}")
            formulas = [autonomy.get("formula")] if autonomy.get("formula") is not None else []
            by_order = autonomy.get("by_order") if isinstance(autonomy.get("by_order"), dict) else {}
            for key in by_order:
                if key not in {"march", "move", "detach", "hold", "attack", "scorch"}:
                    errors.append(f"settings.autonomy.by_order 的命令类型无效：{key}")
            formulas.extend(by_order.values())
            for formula in formulas:
                try:
                    ast.parse(str(formula), mode="eval")
                except SyntaxError:
                    errors.append(f"settings.autonomy 公式无效：{formula}")
            outcomes = autonomy.get("outcomes")
            if outcomes is not None and (
                not isinstance(outcomes, dict) or any(key not in {"delay", "alter", "sortie"} for key in outcomes)
            ):
                errors.append("settings.autonomy.outcomes 只能含 delay / alter / sortie 的权重")
    postures = settings.get("postures")
    if postures is not None and (not isinstance(postures, dict) or any(key not in {"hold", "attack", "scorch"} for key in postures)):
        errors.append("settings.postures 只能覆盖 hold / attack / scorch 的系数")
    for region in data.get("regions") or []:
        if isinstance(region, dict) and "courier_days" in region:
            value = region.get("courier_days")
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"地区 {region.get('id')} 的 courier_days 须为非负整数")
    for event in data.get("events") or []:
        if isinstance(event, dict) and event.get("region") is not None and str(event.get("region")) not in region_ids:
            errors.append(f"事件 {event.get('id')} 的 region 不存在：{event.get('region')}")
    errors.extend(card_errors(data))
    for index, step in enumerate(tutorial_steps(data)):
        where = f"tutorial[{index}]"
        if not isinstance(step, dict):
            errors.append(f"{where} 须为对象")
            continue
        for key in sorted(set(step) - {"turn", "step", "title", "text", "target"}):
            errors.append(f"{where} 含未知键：{key}")
        if not isinstance(step.get("turn"), int) or not step.get("title") or not step.get("text"):
            errors.append(f"{where} 须有 turn（整数）、title、text")
    return errors


def tutorial_steps(data: dict[str, Any]) -> list[Any]:
    """新手引导步骤：scenario.yaml 的 tutorial 键，或 tutorial.yaml（二者都写时合并）。"""
    scenario = data.get("scenario") if isinstance(data.get("scenario"), dict) else {}
    rows: list[Any] = []
    for raw in (scenario.get("tutorial"), data.get("tutorial")):
        if isinstance(raw, list):
            rows.extend(raw)
    return rows


def upkeep_errors(raw: Any, resource_ids: set[str], faction_ids: set[str], army_ids: set[str]) -> list[str]:
    """formulas.upkeep：[{resource, per, amount, label, factions?, armies?}]（按现兵额每 per 兵计 amount，正数为支出）。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        return ["formulas.upkeep 必须是列表"]
    errors: list[str] = []
    for index, row in enumerate(raw):
        where = f"formulas.upkeep[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{where} 必须是对象")
            continue
        unknown = [key for key in row if key not in {"resource", "per", "amount", "label", "factions", "armies"}]
        if unknown:
            errors.append(f"{where} 含未知键：{'、'.join(map(str, unknown))}")
        if str(row.get("resource") or "treasury") not in resource_ids:
            errors.append(f"{where} 的资源不存在：{row.get('resource')}")
        per, amount = row.get("per"), row.get("amount")
        if isinstance(per, bool) or not isinstance(per, (int, float)) or per <= 0:
            errors.append(f"{where}.per 须为正数")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            errors.append(f"{where}.amount 须为数值")
        for faction in row.get("factions") or []:
            if faction_ids and str(faction) not in faction_ids:
                errors.append(f"{where} 的势力不存在：{faction}")
        for army in row.get("armies") or []:
            if str(army) not in army_ids:
                errors.append(f"{where} 的军队不存在：{army}")
    return errors


def _advisor_requires_errors(advisor: dict[str, Any], location: str, region_ids: set[str]) -> list[str]:
    """谏言人 requires：{office: id|[id], region: id|[id], alive: bool}。"""
    requires = advisor.get("requires")
    who = advisor.get("character")
    if not isinstance(requires, dict):
        return [f"{location} 的谏言人 {who} 的 requires 必须是对象"]
    errors = [f"{location} 的谏言人 {who} 的 requires 含未知键：{key}" for key in requires if key not in {"office", "region", "alive"}]
    region = requires.get("region")
    for region_id in region if isinstance(region, list) else ([region] if region is not None else []):
        if region_ids and str(region_id) not in region_ids:
            errors.append(f"{location} 的谏言人 {who} 的 requires.region 不存在：{region_id}")
    if "alive" in requires and not isinstance(requires["alive"], bool):
        errors.append(f"{location} 的谏言人 {who} 的 requires.alive 必须是 true/false")
    return errors


def player_ops_errors(settings: dict[str, Any]) -> list[str]:
    """settings.player_ops：操作名列表，或 {allow, deny, statuses}。"""
    from simcore.domain.ops import OP_NAMES

    config = settings.get("player_ops") if isinstance(settings, dict) else None
    if config is None:
        return []
    names: list[Any] = []
    statuses: list[Any] = []
    if isinstance(config, list):
        names = config
    elif isinstance(config, dict):
        unknown = [key for key in config if key not in {"allow", "deny", "statuses"}]
        if unknown:
            return [f"settings.player_ops 含未知键：{'、'.join(map(str, unknown))}"]
        names = [*(config.get("allow") or []), *(config.get("deny") or [])]
        statuses = list(config.get("statuses") or [])
    else:
        return ["settings.player_ops 必须是操作名列表或 {allow, deny, statuses}"]
    errors = [f"settings.player_ops 含未知操作：{name}" for name in names if name not in OP_NAMES]
    errors.extend(
        f"settings.player_ops.statuses 含未知状态：{status}"
        for status in statuses
        if status not in {"alive", "dead", "imprisoned", "exiled", "retired"}
    )
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
            data[key] = yaml_load(path.read_text(encoding="utf-8"))
            if data[key] is None:
                data[key] = {"entries": []} if filename == "assets.yaml" else []
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"{filename} 无法读取：{exc}")
            data[key] = {"entries": []} if filename == "assets.yaml" else []
    for filename in OPTIONAL_SCENARIO_FILES:
        path = root / filename
        if not path.is_file():
            continue
        try:
            data[filename.removesuffix(".yaml")] = yaml_load(path.read_text(encoding="utf-8")) or []
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"{filename} 无法读取：{exc}")
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


def _clamped_stats(stats: Any, ranges: dict[str, list[float]]) -> dict[str, float]:
    """开局地区指标按剧本 stats[].range 截入范围。"""
    result = dict(stats or {})
    for key, bounds in ranges.items():
        if key in result and isinstance(result[key], (int, float)):
            result[key] = min(max(result[key], bounds[0]), bounds[1])
    return result


def build_initial_state(data: dict[str, Any], difficulty: str | None = None) -> WorldState:
    """场景开局状态。difficulty 为难度档 id（缺省用 default_difficulty）；未知 id 抛 DifficultyError。"""
    scenario = ScenarioDocument.model_validate(data["scenario"])
    difficulty_row = resolve_difficulty(data["scenario"], difficulty)
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
    rules.setdefault("turn_unit", scenario.turn_unit)
    stat_names = {
        str(row.get("id")): str(row.get("name", row.get("id")))
        for row in data["scenario"].get("stats", []) or []
        if isinstance(row, dict) and row.get("id") is not None
    }
    if stat_names:
        rules.setdefault("stat_names", stat_names)
    stat_ranges = {
        str(row.get("id")): list(bounds)
        for row in data["scenario"].get("stats", []) or []
        if isinstance(row, dict) and row.get("id") is not None and (bounds := parse_stat_range(row.get("range"))) is not None
    }
    if stat_ranges:
        rules.setdefault("stat_ranges", stat_ranges)
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
            stats=_clamped_stats(row.get("stats", {}), stat_ranges),
            controller=row.get("controller", main_faction),
            courier_days=int(row["courier_days"]) if isinstance(row.get("courier_days"), (int, float)) else None,
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
    offices = {
        row["id"]: Office(**{key: value for key, value in row.items() if key not in OFFICE_POSTING_KEYS}) for row in scenario.offices
    }
    postings = office_postings(scenario.offices)
    if postings:
        rules.setdefault("office_postings", postings)
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
    if scenario.calendar.eras and start_season_index(data["scenario"]) is not None:
        rules.setdefault("calendar_config", calendar_config)  # 供 turn_label 推算任意回合的历法标签
        rules.setdefault("calendar_start_index", start_season_index(data["scenario"]))
    state = WorldState(
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
    _initial_ledger(state, data["scenario"].get("ledger"))
    return apply_difficulty(state, difficulty_row)


def _initial_ledger(state: WorldState, rows: Any) -> None:
    """scenario.yaml 的 `ledger:`：开局即有的经常账目（T0 起可见，T0→T1 结算即计入经常收支）。

    每项 `{id?, name, category, amount, kind: recurring|one_off = recurring, turns?, reason?, cancellable = false}`；
    剧本定额默认不可由诏令撤销（cancellable: false）。id 缺省按引擎序号生成（ledger-xxxxxxxx）。格式不对的条目跳过（validate 报告）。
    """
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or str(row.get("category")) not in state.resources or not row.get("name"):
            continue
        try:
            amount = float(row.get("amount", 0))
        except (TypeError, ValueError):
            continue
        entry_id = str(row.get("id") or "")
        if not entry_id:
            sequence = int(state.flags.get("_internal:id_seq", 0)) + 1
            state.flags["_internal:id_seq"] = sequence
            entry_id = f"ledger-{sequence:08x}"
        state.ledger.append(
            LedgerEntry(
                id=entry_id,
                name=str(row["name"]),
                category=str(row["category"]),
                amount=amount,
                kind="one_off" if row.get("kind") == "one_off" else "recurring",
                turns=int(row["turns"]) if row.get("turns") is not None else None,
                reason=str(row.get("reason") or ""),
                cancellable=bool(row.get("cancellable", False)),
            )
        )


def initial_ledger_errors(rows: Any, resource_ids: set[str]) -> list[str]:
    """scenario.yaml 的 ledger 列表检查。"""
    if rows is None:
        return []
    if not isinstance(rows, list):
        return ["ledger 必须是列表"]
    errors: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        where = f"ledger[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{where} 必须是对象")
            continue
        unknown = [key for key in row if key not in {"id", "name", "category", "amount", "kind", "turns", "reason", "cancellable"}]
        if unknown:
            errors.append(f"{where} 含未知键：{'、'.join(map(str, unknown))}")
        if not row.get("name"):
            errors.append(f"{where} 缺少 name")
        if str(row.get("category")) not in resource_ids:
            errors.append(f"{where} 的资源不存在：{row.get('category')}")
        if isinstance(row.get("amount"), bool) or not isinstance(row.get("amount"), (int, float)):
            errors.append(f"{where}.amount 须为数值")
        if row.get("kind", "recurring") not in {"recurring", "one_off"}:
            errors.append(f"{where}.kind 须为 recurring 或 one_off")
        if row.get("turns") is not None and (isinstance(row.get("turns"), bool) or not isinstance(row.get("turns"), int) or row["turns"] < 1):
            errors.append(f"{where}.turns 须为正整数")
        if row.get("id"):
            if str(row["id"]) in seen:
                errors.append(f"{where} id 重复：{row['id']}")
            seen.add(str(row["id"]))
    return errors


OFFICE_POSTING_KEYS = frozenset({"army", "seat"})


def office_postings(rows: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """官职的任所：{office: {army?, seat?}}（只收写了 army / seat 的官职），开局写入 rules.office_postings。

    army：此职所统军队（任命即兼统，免职即罢其统帅）；seat：任所地区（未写时取所统军队的驻地）。
    """
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        posting = {key: str(row[key]) for key in ("army", "seat") if isinstance(row, dict) and row.get(key)}
        if posting:
            result[str(row.get("id"))] = posting
    return result


def _initial_offices(row: dict[str, Any]) -> list[str]:
    offices = [str(item) for item in row.get("offices") or []]
    if row.get("office") and row["office"] not in offices:
        offices.insert(0, str(row["office"]))
    return offices


def scenario_json_schema() -> dict[str, Any]:
    return ScenarioDocument.model_json_schema()


DEFAULT_ASSET_REQUIREMENTS: dict[str, Any] = {
    "character": "character.{id}.portrait",
    "region": "region.{id}.card",
    "ui": [
        "ui.topbar.background",
        "ui.edict.parchment",
        "ui.court.backdrop",
        "ui.map.base",
        "ui.memorial.banner",
    ],
}


def _asset_requirements(scenario: Any) -> dict[str, Any]:
    """Required asset ids; scenario.yaml `asset_requirements` overrides each key (null disables a pattern)."""
    configured = scenario.get("asset_requirements", {}) if isinstance(scenario, dict) else {}
    if not isinstance(configured, dict):
        configured = {}
    result = dict(DEFAULT_ASSET_REQUIREMENTS)
    for key in ("character", "region"):
        if key in configured:
            value = configured[key]
            result[key] = value if isinstance(value, str) and "{id}" in value else None
    if "ui" in configured:
        result["ui"] = [item for item in configured["ui"] or [] if isinstance(item, str)]
    return result
