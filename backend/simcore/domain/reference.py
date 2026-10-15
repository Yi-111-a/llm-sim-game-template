"""op_reference：玩家可用操作与实体 id 表（MCP 工具与 REST 共用）。

缺省输出紧凑：每个可用操作一行字段说明 + 一个用本局实体写成的示例；实体表每类至多 TABLE_LIMIT 行（附总数）。
过滤参数：op（该操作的完整 JSON Schema）、entity_kind（该类实体的完整表）、ids（只看这些 id）。
"""

from __future__ import annotations

from typing import Any

from simcore.domain.models import WorldState
from simcore.domain.ops import OP_NAMES, op_model_schema, op_signature
from simcore.domain.permissions import OP_LABELS, OP_ROUTES, SCENARIO_ONLY_HINT, player_ops, player_statuses
from simcore.domain.rules import STATUS_NAMES, held_offices, mover_faction, travel_note

TABLE_LIMIT = 12
ENTITY_KINDS = ("resources", "characters", "offices", "regions", "armies", "factions", "ledger", "standing_orders", "pending_choices")


class ReferenceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _own(state: WorldState, faction: str | None) -> bool:
    own = mover_faction(state, None)
    return own is None or faction in (None, own)


def op_examples(state: WorldState, player: str | None = None) -> dict[str, dict[str, Any]]:
    """用本局实体写成的示例（找不到合适实体时用占位 id）。"""
    people = [row for row in state.characters.values() if row.status == "alive" and row.id != player and _own(state, row.faction)]
    idle = next((row for row in people if not row.in_transit), people[0] if people else None)
    holder = next((row for row in people if held_offices(row)), None)
    vacant = next((row for row in state.offices.values() if row.holder is None), None)
    armies = [row for row in state.armies.values() if _own(state, row.faction) and row.strength > 0]
    army = next((row for row in armies if not row.in_transit), armies[0] if armies else None)
    regions = [row for row in state.regions.values() if _own(state, row.controller)]
    home = state.regions.get(idle.region) if idle else (regions[0] if regions else None)
    near = next(iter(home.adjacency), None) if home else None
    army_home = state.regions.get(army.region) if army else None
    army_near = next(iter(army_home.adjacency), None) if army_home else None
    resource = next((row.id for row in state.resources.values() if not row.hidden), "treasury")
    stat = next(iter(home.stats), "defense") if home else "defense"
    who = idle.id if idle else "<character>"
    examples: dict[str, dict[str, Any]] = {
        "appoint": {"op": "appoint", "character": who, "office": vacant.id if vacant else "<office>"},
        "dismiss": {"op": "dismiss", "character": holder.id if holder else "<character>", "office": held_offices(holder)[0] if holder else "<office>"},
        "move_character": {"op": "move_character", "character": who, "to_region": near or "<region>"},
        "move_army": {"op": "move_army", "army": army.id if army else "<army>", "to_region": army_near or "<region>", "with_commander": True},
        "march": {"op": "march", "army": army.id if army else "<army>", "to_region": army_near or "<region>"},
        "set_army_commander": {"op": "set_army_commander", "army": army.id if army else "<army>", "character": who},
        "start_project": {
            "op": "start_project",
            "name": "修城",
            "region": home.id if home else "<region>",
            "cost_per_turn": {resource: 20},
            "turns": 2,
            "on_complete": [{"op": "adjust_region", "region": home.id if home else "<region>", "stat": stat, "delta": 4}],
        },
        "add_standing_order": {
            "op": "add_standing_order",
            "text": "岁遣使存问",
            "ops": [{"op": "remember", "character": who, "fact": "朝廷岁遣使存问"}],
            "until_turn": state.turn + 4,
        },
        "cancel_standing_order": {"op": "cancel_standing_order", "id": state.standing_orders[0].id if state.standing_orders else "order-00000001"},
        "cancel_ledger": {
            "op": "cancel_ledger",
            "ledger_id": next((row.id for row in state.ledger if row.cancellable), "ledger-00000001"),
        },
        "remember": {"op": "remember", "character": who, "fact": "陛下嘉其忠勤"},
        "set_character_status": {
            "op": "set_character_status",
            "character": who,
            "status": player_statuses(state)[0] if player_statuses(state) else "retired",
            "cause": {"imprisoned": "下狱按问", "exiled": "流放岭南", "dead": "赐死", "alive": "赦免"}.get(
                player_statuses(state)[0] if player_statuses(state) else "retired", "年老致仕"
            ),
        },
    }
    return examples


def _placeholder_example(name: str) -> dict[str, Any]:
    schema = op_model_schema(name)
    example: dict[str, Any] = {"op": name}
    for field in schema.get("required", []):
        if field != "op":
            example[field] = f"<{field}>"
    return example


def _entity_rows(state: WorldState) -> dict[str, list[dict[str, Any]]]:
    return {
        "resources": [{"id": row.id, "name": row.name, "hidden": row.hidden, "unit": row.unit} for row in state.resources.values()],
        "characters": [
            {
                "id": row.id,
                "name": row.name,
                "status": row.status,
                "faction": row.faction,
                "offices": held_offices(row),
                "region": row.region,
                "bloc": row.bloc,
                "in_transit": travel_note(state, row) or None,
            }
            for row in state.characters.values()
        ],
        "offices": [{"id": row.id, "name": row.name, "holder": row.holder, "requires": row.requires} for row in state.offices.values()],
        "regions": [
            {"id": row.id, "name": row.name, "controller": row.controller, "adjacency": row.adjacency, "stats": list(row.stats)}
            for row in state.regions.values()
        ],
        "armies": [
            {
                "id": row.id,
                "name": row.name,
                "faction": row.faction,
                "region": row.region,
                "strength": row.strength,
                "commander": row.commander,
                "in_transit": travel_note(state, row) or None,
            }
            for row in state.armies.values()
        ],
        "factions": [{"id": row.id, "name": row.name} for row in state.factions.values()],
        "ledger": [{"id": row.id, "name": row.name, "amount": row.amount, "cancellable": row.cancellable} for row in state.ledger],
        "standing_orders": [{"id": row.id, "text": row.text} for row in state.standing_orders],
        "pending_choices": [{"id": row.id, "title": row.title, "options": [option.id for option in row.options]} for row in state.pending_choices],
    }


def _compact(kind: str, row: dict[str, Any], state: WorldState) -> str:
    """紧凑一行：「id 名（要点）」。"""
    extra: list[str] = []
    if kind == "characters":
        if row["status"] != "alive":
            extra.append(STATUS_NAMES.get(row["status"], row["status"]))
        extra.extend(state.offices[office].name for office in row["offices"] if office in state.offices)
        region = state.regions.get(row["region"])
        extra.append(row["in_transit"] or (region.name if region else row["region"]))
    elif kind == "offices":
        holder = state.characters.get(row["holder"]) if row["holder"] else None
        extra.append(holder.name if holder else "出缺")
    elif kind == "armies":
        region = state.regions.get(row["region"])
        commander = state.characters.get(row["commander"]) if row["commander"] else None
        extra.append(row["in_transit"] or (region.name if region else row["region"]))
        extra.append(f"统帅{commander.name}" if commander else "无统帅")
        if row["faction"] and row["faction"] in state.factions:
            extra.append(state.factions[row["faction"]].name)
    elif kind == "regions":
        faction = state.factions.get(row["controller"]) if row["controller"] else None
        if faction:
            extra.append(faction.name)
    elif kind == "resources" and row["hidden"]:
        extra.append("风闻")
    elif kind == "pending_choices":
        extra.append("/".join(row["options"]))
    name = row.get("name") or row.get("text") or row.get("title") or ""
    return f"{row['id']} {name}" + (f"（{'，'.join(str(item) for item in extra if item)}）" if extra else "")


def build_reference(
    state: WorldState,
    op: str | None = None,
    entity_kind: str | None = None,
    ids: list[str] | None = None,
    player: str | None = None,
) -> dict[str, Any]:
    allowed = player_ops(state)
    examples = op_examples(state, player)
    rows = _entity_rows(state)
    wanted = {str(item) for item in ids or [] if str(item)}
    if op:
        if op not in OP_NAMES:
            raise ReferenceError("UNKNOWN_OP", f"未知操作：{op}（可用操作见 op_reference 的 permitted_ops）")
        permitted = op in allowed
        result: dict[str, Any] = {
            "op": op,
            "label": OP_LABELS.get(op, op),
            "permitted": permitted,
            "fields": op_signature(op),
            "schema": op_model_schema(op),
            "example": examples.get(op) or _placeholder_example(op),
        }
        if not permitted:
            result["note"] = f"玩家不能直接提议此操作（OP_NOT_PERMITTED）：{OP_ROUTES.get(op, SCENARIO_ONLY_HINT)}"
        if op == "set_character_status":
            statuses = "、".join(f"{item}（{STATUS_NAMES.get(item, item)}）" for item in player_statuses(state))
            result["note"] = f"君主可直接施加：{statuses}；须为本朝臣属，或身在本朝控制之地"
        if op == "start_project":
            result["note"] = "每季花费须为正；on_complete 只能提升本地区指标（每项 ≤ 10）且每点增益至少花费 10，或其他玩家可用操作"
        return result
    if entity_kind:
        if entity_kind not in ENTITY_KINDS:
            raise ReferenceError("INVALID_REQUEST", f"未知实体类别：{entity_kind}（可选：{'、'.join(ENTITY_KINDS)}）")
        table = [row for row in rows[entity_kind] if not wanted or str(row.get("id")) in wanted]
        return {"entity_kind": entity_kind, "count": len(table), "rows": table}
    if wanted:
        found = {kind: [row for row in table if str(row.get("id")) in wanted] for kind, table in rows.items()}
        found = {kind: table for kind, table in found.items() if table}
        missing = sorted(wanted - {str(row.get("id")) for table in found.values() for row in table})
        return {"ids": sorted(wanted), "entities": found, "missing": missing}
    scenario_only = [name for name in OP_NAMES if name not in allowed]
    entities: dict[str, Any] = {}
    for kind, table in rows.items():
        compact = [_compact(kind, row, state) for row in table[:TABLE_LIMIT]]
        entry: dict[str, Any] = {"count": len(table), "rows": compact}
        if len(table) > TABLE_LIMIT:
            entry["more"] = f"另有 {len(table) - TABLE_LIMIT} 项：op_reference(entity_kind=\"{kind}\")"
        entities[kind] = entry
    return {
        "permitted_ops": [
            {
                "op": name,
                "label": OP_LABELS.get(name, name),
                "fields": op_signature(name),
                "example": examples.get(name) or _placeholder_example(name),
            }
            for name in allowed
        ],
        "scenario_only_ops": {name: OP_LABELS.get(name, name) for name in scenario_only},
        "usage": {
            "client_ops": "draft_edict(ops=[...]) 只接受 permitted_ops 中的操作；其余操作返回 OP_NOT_PERMITTED。",
            "memorials": (
                "数值类变化（资源、地区指标、关系、心态、旗标……）随奏疏选项施行：draft_edict(adopted=[{memorial_id, option_id}])，"
                "选项及其 ops 摘要见 briefing。"
            ),
            "choices": "待决事件用 choose(choice_id, option_id) 处置，不能写成 choose_event_option op。",
            "scenario_only": SCENARIO_ONLY_HINT,
        },
        "entities": entities,
        "filters": "op=<操作名> 看该操作的完整 JSON Schema 与说明；entity_kind=<类别> 看完整实体表；ids=[...] 只看这些 id。",
        "rules": {
            "max_concurrent_offices": state.rules.get("max_concurrent_offices", 1),
            "notes": [
                "每条 op 是一个 JSON 对象，op 字段为操作名；未知字段会被拒绝。",
                "move_character / move_army / march 可指向任何可达地区：引擎按邻接表寻最短路（同样短时优先本方控制地区），整段一次出发，"
                "途中不能再下令；抵达回合 = 各段回合数之和。无路可达时返回 OUT_OF_RANGE。",
                "move_army / march 时，与军队同驻一地的统帅默认随军同行（with_commander: false 则留驻）。"
                "set_army_commander 任命不在军中的人，此人自动启程赴任，到任前由副将暂摄。",
                "appoint 前须满足官职 requires；已达兼职上限时先 dismiss。",
                "被拒条款返回稳定错误码：OP_NOT_PERMITTED、UNKNOWN_ENTITY、INVALID_TARGET、OFFICE_OCCUPIED、OFFICE_HELD、OFFICE_REQUIREMENT、"
                "CHARACTER_UNAVAILABLE、OUT_OF_RANGE、INSUFFICIENT_RESOURCE、CONDITION_FAILED、GAME_OVER。",
            ],
        },
    }
