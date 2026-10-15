"""奏疏条目（memorials.yaml）：当季可见条目、采纳选项转为诏书 ops。"""

from __future__ import annotations

from typing import Any

from simcore.domain.events import matches
from simcore.domain.models import WorldState
from simcore.domain.ops import parse_op
from simcore.domain.rules import STATUS_NAMES, entity_name, stat_name, trait_name
from simcore.domain.visibility import format_number


class MemorialError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def memorial_flag(memorial_id: str) -> str:
    return f"memorial:{memorial_id}"


def is_visible(item: dict[str, Any], state: WorldState) -> bool:
    """当季可见：未处理，且 turn 窗口与 conditions 均满足。

    - 写 turn：可见回合为 [turn, turn + expires_after]（expires_after 缺省 0）。
    - 写 conditions：条件满足时可见；与 turn 同写时二者都须满足。
    - 二者都不写：始终可见，直到被处理。
    """
    if state.flags.get(memorial_flag(str(item.get("id")))) is not None:
        return False
    turn = item.get("turn")
    if turn is not None:
        start = int(turn)
        end = start + int(item.get("expires_after", 0) or 0)
        if not start <= state.turn <= end:
            return False
    if "conditions" in item and not matches(item.get("conditions"), state):
        return False
    return True


def visible_memorials(scenario: dict[str, Any], state: WorldState) -> list[dict[str, Any]]:
    items = scenario.get("memorials") or []
    return [_public_item(item, state) for item in items if isinstance(item, dict) and is_visible(item, state)]


def _public_item(item: dict[str, Any], state: WorldState) -> dict[str, Any]:
    advisors = []
    for advisor in item.get("advisors", []) or []:
        character = state.characters.get(str(advisor.get("character")))
        advisors.append({
            "character": advisor.get("character"),
            "name": character.name if character else advisor.get("character"),
            "stance": advisor.get("stance", "neutral"),
            "line": advisor.get("line", ""),
        })
    options = [
        {
            "id": option.get("id"),
            "label": option.get("label", option.get("id")),
            "hint": option.get("hint", ""),
            "ops": list(option.get("ops", []) or []),
            "ops_summary": summarize_ops(option.get("ops", []) or [], state),
        }
        for option in item.get("options", []) or []
    ]
    region = state.regions.get(str(item.get("region"))) if item.get("region") else None
    return {
        "id": item.get("id"),
        "title": item.get("title", item.get("id")),
        "summary": item.get("summary", ""),
        "region": item.get("region"),
        "region_name": region.name if region else None,
        "advisors": advisors,
        "options": options,
        "illustration": item.get("illustration"),
    }


def adopted_ops(scenario: dict[str, Any], state: WorldState, memorial_id: str, option_id: str) -> list[dict[str, Any]]:
    """采纳某条奏疏的某个选项，返回要加入诏书草案的 ops（结构已校验）。"""
    item = next((row for row in scenario.get("memorials") or [] if isinstance(row, dict) and row.get("id") == memorial_id), None)
    if item is None:
        raise MemorialError("UNKNOWN_MEMORIAL", f"奏疏不存在：{memorial_id}")
    if not is_visible(item, state):
        raise MemorialError("MEMORIAL_UNAVAILABLE", f"奏疏「{item.get('title', memorial_id)}」本季不可处理（已处理或已过期）")
    option = next((row for row in item.get("options", []) or [] if row.get("id") == option_id), None)
    if option is None:
        raise MemorialError("UNKNOWN_OPTION", f"奏疏「{item.get('title', memorial_id)}」没有选项：{option_id}")
    return [parse_op(op).model_dump(mode="json") for op in option.get("ops", []) or []]


INTERNAL_OPS = frozenset({"set_flag", "adjust_counter", "set_counter"})
INTERNAL_SUMMARY = "记入档案（影响后续事件）"


def summarize_ops(ops: list[dict[str, Any]], state: WorldState) -> list[str]:
    """把 ops 写成简短中文摘要，供奏疏卡片与 MCP 简报使用。

    旗标 / 计数等内部操作不暴露键名，合并为一句「记入档案（影响后续事件）」。
    """
    lines: list[str] = []
    for raw in ops:
        try:
            line = summarize_op(raw, state)
        except (KeyError, TypeError, ValueError):
            line = str(raw.get("op", "?")) if isinstance(raw, dict) else "?"
        if line == INTERNAL_SUMMARY and line in lines:
            continue
        lines.append(line)
    return lines


def summarize_op(raw: dict[str, Any], state: WorldState) -> str:
    op = raw.get("op")

    def name(kind: str, key: Any) -> str:
        table: dict[str, Any] = {
            "resource": state.resources,
            "character": state.characters,
            "office": state.offices,
            "region": state.regions,
            "army": state.armies,
            "faction": state.factions,
        }[kind]
        row = table.get(str(key)) if key is not None else None
        return getattr(row, "name", str(key)) if row is not None else str(key)

    def signed(value: Any) -> str:
        number = float(value)
        text = format_number(abs(number))
        return f"+{text}" if number >= 0 else f"−{text}"

    if op in INTERNAL_OPS:
        return INTERNAL_SUMMARY
    if op == "adjust_resource":
        return f"{name('resource', raw['resource'])} {signed(raw['delta'])}"
    if op == "appoint":
        return f"任 {name('character', raw['character'])} 为 {name('office', raw['office'])}"
    if op == "dismiss":
        return f"免 {name('character', raw['character'])} {name('office', raw['office'])}"
    if op == "adjust_region":
        return f"{name('region', raw['region'])}{stat_name(state, raw['stat'])} {signed(raw['delta'])}"
    if op == "start_project":
        return f"兴工「{raw['name']}」{raw['turns']} 季"
    if op == "add_ledger":
        return f"账目「{raw['name']}」{signed(raw['amount'])}"
    if op == "adjust_army":
        return f"{name('army', raw['army'])} {signed(raw['delta'])}"
    if op == "move_army":
        return f"{name('army', raw['army'])} 移驻 {name('region', raw['to_region'])}"
    if op == "move_character":
        return f"{name('character', raw['character'])} 赴 {name('region', raw['to_region'])}"
    if op == "set_army_faction":
        return f"{name('army', raw['army'])} 改隶 {name('faction', raw['faction'])}"
    if op == "set_army_commander":
        return f"{name('army', raw['army'])} 由 {name('character', raw.get('character'))} 统领"
    if op == "adjust_trait":
        return f"{name('character', raw['character'])} {trait_name(state, raw['trait'])} {signed(raw['delta'])}"
    if op == "adjust_relation":
        return f"{entity_name(state, raw['a'])}↔{entity_name(state, raw['b'])} 关系 {signed(raw['delta'])}"
    if op == "remember":
        return f"{name('character', raw['character'])} 记下：{raw['fact']}"
    if op == "set_character_status":
        return f"{name('character', raw['character'])} {STATUS_NAMES.get(raw['status'], raw['status'])}"
    return str(op)
