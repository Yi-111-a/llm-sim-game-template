"""奏疏条目（memorials.yaml）：当季可见条目、采纳选项转为诏书 ops。"""

from __future__ import annotations

from typing import Any

from simcore.domain.events import matches
from simcore.domain.models import WorldState
from simcore.domain.ops import memory_ops, parse_op
from simcore.domain.rules import STATUS_NAMES, entity_name, find_route, stat_name, trait_name, turn_unit_label
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
    """采纳某条奏疏的某个选项，返回要加入诏书草案的 ops（结构已校验）。

    选项的 memory（{character, text} 或其列表）追加为 Remember op，随选项一同执行。
    """
    item = next((row for row in scenario.get("memorials") or [] if isinstance(row, dict) and row.get("id") == memorial_id), None)
    if item is None:
        raise MemorialError("UNKNOWN_MEMORIAL", f"奏疏不存在：{memorial_id}")
    if not is_visible(item, state):
        raise MemorialError("MEMORIAL_UNAVAILABLE", f"奏疏「{item.get('title', memorial_id)}」本季不可处理（已处理或已过期）")
    option = next((row for row in item.get("options", []) or [] if row.get("id") == option_id), None)
    if option is None:
        raise MemorialError("UNKNOWN_OPTION", f"奏疏「{item.get('title', memorial_id)}」没有选项：{option_id}")
    ops = [*(option.get("ops", []) or []), *memory_ops(option.get("memory"))]
    return [parse_op(op).model_dump(mode="json") for op in ops]


def memorial_item(scenario: dict[str, Any], memorial_id: str) -> dict[str, Any] | None:
    return next((row for row in scenario.get("memorials") or [] if isinstance(row, dict) and str(row.get("id")) == memorial_id), None)


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
        if key is None or key == "":
            return "无"
        row = table.get(str(key))
        return getattr(row, "name", str(key)) if row is not None else str(key)

    def signed(value: Any) -> str:
        number = float(value)
        text = format_number(abs(number))
        return f"+{text}" if number >= 0 else f"−{text}"

    if op in INTERNAL_OPS:
        return INTERNAL_SUMMARY
    if op == "adjust_resource":
        resource = state.resources.get(str(raw["resource"]))
        if resource is not None and resource.hidden:
            # 隐藏数值不给确数，只给方向（与公开视图一致）
            return f"{resource.name} {'↑' if float(raw['delta']) > 0 else '↓' if float(raw['delta']) < 0 else '不变'}（风闻）"
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
        army = state.armies.get(str(raw["army"]))
        route = _route_note(state, army.region if army else None, raw["to_region"], army.faction if army else None)
        return f"{name('army', raw['army'])} 移驻 {name('region', raw['to_region'])}{route}"
    if op == "move_character":
        character = state.characters.get(str(raw["character"]))
        route = _route_note(state, character.region if character else None, raw["to_region"], character.faction if character else None)
        return f"{name('character', raw['character'])} 赴 {name('region', raw['to_region'])}{route}"
    if op == "set_army_faction":
        return f"{name('army', raw['army'])} 改隶 {name('faction', raw['faction'])}"
    if op == "set_army_commander":
        if not raw.get("character"):
            return f"罢{name('army', raw['army'])}统帅"
        return f"{name('army', raw['army'])} 由 {name('character', raw.get('character'))} 统领"
    if op == "adjust_trait":
        if str(raw["trait"]) in {str(item) for item in state.rules.get("hidden_traits", []) or []}:
            return f"{name('character', raw['character'])} 心态有变（不明）"
        return f"{name('character', raw['character'])} {trait_name(state, raw['trait'])} {signed(raw['delta'])}"
    if op == "adjust_relation":
        return f"{entity_name(state, raw['a'])}↔{entity_name(state, raw['b'])} 关系 {signed(raw['delta'])}"
    if op == "remember":
        return f"{name('character', raw['character'])} 记下：{raw['fact']}"
    if op == "set_character_status":
        return f"{name('character', raw['character'])} {STATUS_NAMES.get(raw['status'], raw['status'])}"
    if op == "cancel_ledger":
        ledger = next((row.name for row in state.ledger if row.id == raw.get("ledger_id")), raw.get("ledger_id"))
        return f"取消账目「{ledger}」"
    if op == "add_standing_order":
        return f"常设令「{raw.get('text', '')}」"
    if op == "cancel_standing_order":
        order = next((row.text for row in state.standing_orders if row.id == raw.get("id")), raw.get("id"))
        return f"撤销常设令「{order}」"
    if op == "set_region_controller":
        return f"{name('region', raw['region'])} 归 {name('faction', raw['faction'])}"
    if op == "choose_event_option":
        return f"处置待决事件 {raw.get('choice_id')}"
    return str(op)


def _route_note(state: WorldState, origin: str | None, destination: Any, faction: str | None) -> str:
    """非相邻行程的摘要：（经某某，约 n 季）；相邻或无路时为空串。"""
    if origin is None:
        return ""
    found = find_route(state, origin, str(destination), faction)
    if found is None or len(found[0]) <= 2:
        return ""
    via = "、".join(state.regions[region_id].name for region_id in found[0][1:-1])
    return f"（经{via}，约{found[1]}{turn_unit_label(state)}）"
