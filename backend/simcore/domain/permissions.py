"""玩家可直接提议的操作（政策级白名单）。

StateOp 分两层：
- 剧本层（scenario）：奏疏选项、事件效果、事件抉择、工程竣工效果、常设令（由剧本写入时）——全部操作可用；
- 玩家层（player）：客户端直接给出的 ops（draft_edict 的 ops、MCP、REST）与 LLM 解释诏令文本得到的 ops——
  只能用政策级操作（任免、调动、兴工、常设令……）；直接改数值、旗标、计数、易帜等一律 OP_NOT_PERMITTED。

剧本可用 settings.player_ops 调整（开局时随 settings 拷入 WorldState.rules）：
- 列表：整体替换默认白名单；
- {allow: [...], deny: [...], statuses: [...]}：在默认白名单上追加 / 移除，statuses 为 set_character_status 可用的状态
  （默认 imprisoned / exiled / retired；加 dead 即允许赐死，加 alive 即允许赦免）。
"""

from __future__ import annotations

from typing import Any

from simcore.domain.battle import hostile
from simcore.domain.models import WorldState
from simcore.domain.ops import OP_NAMES, StateOp, parse_op
from simcore.domain.rules import STATUS_NAMES, Rejected, mover_faction, player_character_id

PLAYER_OPS_DEFAULT: tuple[str, ...] = (
    "appoint",
    "dismiss",
    "move_character",
    "move_army",
    "march",
    "detach",
    "set_army_commander",
    "set_army_posture",
    "start_project",
    "add_standing_order",
    "cancel_standing_order",
    "cancel_ledger",
    "remember",
    "set_character_status",
)
PLAYER_STATUSES_DEFAULT: tuple[str, ...] = ("imprisoned", "exiled", "retired")
# 玩家自拟工程的竣工效果：只许提升本地区指标，且每点增益至少花费这么多（各资源每季花费 × 工期之和）。
PROJECT_COST_PER_POINT_DEFAULT = 10.0
PROJECT_MAX_GAIN_DEFAULT = 10.0

OP_LABELS: dict[str, str] = {
    "adjust_resource": "调整资源",
    "add_ledger": "新增账目",
    "cancel_ledger": "取消账目",
    "appoint": "任命",
    "dismiss": "免职",
    "set_character_status": "处置人物",
    "set_character_faction": "人物改属",
    "move_character": "人物行程",
    "move_army": "调动军队",
    "march": "率军出征",
    "detach": "分兵",
    "start_project": "兴工",
    "adjust_region": "调整地区指标",
    "adjust_relation": "调整关系",
    "add_standing_order": "常设令",
    "cancel_standing_order": "撤常设令",
    "set_flag": "设旗标",
    "remember": "人物记忆",
    "adjust_army": "调整兵力",
    "merge_army": "并军",
    "set_army_faction": "军队改隶",
    "set_army_commander": "军队易帅",
    "set_army_posture": "军令姿态",
    "adjust_trait": "调整心态",
    "adjust_counter": "调整计数",
    "set_counter": "设定计数",
    "set_region_controller": "地区易手",
    "choose_event_option": "事件抉择",
}

# 不在玩家白名单中的操作从哪里来（op_reference 的用法提示）。
SCENARIO_ONLY_HINT = "只能经奏疏选项（draft_edict 的 adopted）、事件抉择（choose）、剧本预设的工程竣工效果或剧本事件产生"
OP_ROUTES: dict[str, str] = {
    "choose_event_option": "请用 choose 工具处置待决事件",
}


def _config(state: WorldState) -> Any:
    return state.rules.get("player_ops")


def player_ops(state: WorldState) -> list[str]:
    """本局玩家可直接提议的操作名（按 OP_NAMES 顺序）。"""
    return allowed_player_ops(_config(state))


def allowed_player_ops(config: Any) -> list[str]:
    """由 settings.player_ops 配置得出玩家层白名单（validate_scenario 检查政令卡时也用它）。"""
    if isinstance(config, list):
        allowed = {str(item) for item in config}
    else:
        allowed = set(PLAYER_OPS_DEFAULT)
        if isinstance(config, dict):
            allowed |= {str(item) for item in config.get("allow", []) or []}
            allowed -= {str(item) for item in config.get("deny", []) or []}
    return [name for name in OP_NAMES if name in allowed]


def player_statuses(state: WorldState) -> list[str]:
    config = _config(state)
    if isinstance(config, dict) and isinstance(config.get("statuses"), list):
        return [str(item) for item in config["statuses"]]
    return list(PLAYER_STATUSES_DEFAULT)


def _number(state: WorldState, key: str, default: float) -> float:
    try:
        return float(state.rules.get(key, default))
    except (TypeError, ValueError):
        return default


# 须把人拿到手的处置：拘拿下狱、赐死。人须身在本朝控制之地，且当地没有护卫他的敌军。
SEIZE_STATUSES = frozenset({"imprisoned", "dead"})


def _wars(state: WorldState) -> list[Any]:
    wars = state.rules.get("wars")
    return wars if isinstance(wars, list) else []


def hostile_target(state: WorldState, character: Any, own: str | None, status: str) -> Rejected | None:
    """敌对势力的人物、统领或随行敌军的人物，诏令不能隔空处置（HOSTILE_TARGET）；拘拿须人在本朝控制之地且无敌军护卫。"""
    if own is None:
        return None
    wars = _wars(state)
    if hostile(state, character.faction, own, wars):
        faction = state.factions.get(str(character.faction))
        return Rejected(
            "HOSTILE_TARGET",
            f"{character.name}属敌对势力{faction.name if faction else ''}，诏令不及其身；须先战而擒之（战后由奏疏或事件处置）",
        )
    region_id = character.region
    for army in state.armies.values():
        if army.strength <= 0 or not hostile(state, army.faction, own, wars):
            continue
        if army.commander == character.id:
            return Rejected("HOSTILE_TARGET", f"{character.name}统领敌军{army.name}，诏令不及其身；须先战而擒之")
    if status not in SEIZE_STATUSES:
        return None
    region = state.regions.get(region_id)
    if region is None or region.controller not in (None, own):
        where = region.name if region else region_id
        return Rejected("HOSTILE_TARGET", f"{character.name}身在{where}，不在本朝控制之地，无从拘拿")
    escorts = [
        army.name
        for army in state.armies.values()
        if army.region == region_id and not army.in_transit and army.strength > 0 and hostile(state, army.faction, own, wars)
    ]
    if escorts:
        return Rejected("HOSTILE_TARGET", f"{character.name}所在的{region.name}有敌军{'、'.join(escorts)}护卫，无从拘拿；须先击破之")
    return None


def status_target_check(state: WorldState, raw: dict[str, Any]) -> Rejected | None:
    """延期执行的处置条款在结算时复核：其间人物若已投敌或落入敌手，条款不能执行。"""
    character = state.characters.get(str(raw.get("character", "")))
    if raw.get("op") != "set_character_status" or character is None:
        return None
    return hostile_target(state, character, mover_faction(state, None), str(raw.get("status", "")))


def check_player_op(op_value: StateOp | dict[str, Any], state: WorldState, settings: dict[str, Any] | None = None) -> Rejected | None:
    """玩家层权限检查（不做实体校验，那由 rules.validate 负责）。不允许时返回 Rejected(OP_NOT_PERMITTED, 中文原因)。"""
    try:
        op = parse_op(op_value)
    except (ValueError, TypeError):
        return None  # 格式错误交给 validate 报告
    name = op.op
    label = OP_LABELS.get(name, name)
    if name not in player_ops(state):
        route = OP_ROUTES.get(name, SCENARIO_ONLY_HINT)
        return Rejected("OP_NOT_PERMITTED", f"「{label}」（{name}）不是君主可直接下达的政令：此类变化{route}")
    own = mover_faction(state, None)

    def foreign(faction: str | None) -> bool:
        return own is not None and faction not in (None, own)

    if name in {"appoint", "dismiss", "move_character"}:
        character = state.characters.get(str(getattr(op, "character", "")))
        if character is not None and foreign(character.faction):
            return Rejected("OP_NOT_PERMITTED", f"{character.name}不是本朝臣属，政令不及其身")
    if name in {"move_army", "march", "set_army_commander", "detach", "set_army_posture"}:
        army = state.armies.get(str(getattr(op, "army", "")))
        if army is not None and foreign(army.faction):
            return Rejected("OP_NOT_PERMITTED", f"{army.name}不归本朝节制，不能调遣、分兵或易帅")
        commander_id = getattr(op, "character", None) if name == "set_army_commander" else getattr(op, "commander", None) if name == "detach" else None
        commander = state.characters.get(str(commander_id)) if commander_id else None
        if commander is not None and foreign(commander.faction):
            return Rejected("OP_NOT_PERMITTED", f"{commander.name}不是本朝臣属，不能统领本朝军队")
    if name == "set_character_status":
        status = str(getattr(op, "status", ""))
        if status not in player_statuses(state):
            allowed = "、".join(STATUS_NAMES.get(item, item) for item in player_statuses(state)) or "无"
            return Rejected("OP_NOT_PERMITTED", f"君主可直接施加的处置只有：{allowed}；「{STATUS_NAMES.get(status, status)}」须经奏疏或事件")
        character = state.characters.get(str(getattr(op, "character", "")))
        if character is not None:
            if character.id == player_character_id(state, settings):
                return Rejected("OP_NOT_PERMITTED", "不能处置君主本人")
            denied = hostile_target(state, character, own, status)
            if denied is not None:
                return denied
            region = state.regions.get(character.region)
            in_own_land = region is not None and not foreign(region.controller)
            if foreign(character.faction) and not in_own_land:
                return Rejected("OP_NOT_PERMITTED", f"{character.name}不是本朝臣属，且不在本朝控制之地，政令不及其身")
    if name == "start_project":
        region = state.regions.get(str(getattr(op, "region", "")))
        if region is not None and foreign(region.controller):
            return Rejected("OP_NOT_PERMITTED", f"{region.name}不在本朝控制之下，不能兴工")
        costs = dict(getattr(op, "cost_per_turn", {}) or {})
        if not costs or any(float(value) <= 0 for value in costs.values()):
            return Rejected("OP_NOT_PERMITTED", "工程每季花费须为正数（不能以兴工生财）")
        total_cost = sum(float(value) for value in costs.values()) * max(1, int(getattr(op, "turns", 1)))
        gain = 0.0
        max_gain = _number(state, "player_project_max_gain", PROJECT_MAX_GAIN_DEFAULT)
        for raw in getattr(op, "on_complete", []) or []:
            effect = raw if isinstance(raw, dict) else {}
            if effect.get("op") == "adjust_region":
                if str(effect.get("region")) != str(getattr(op, "region", "")):
                    return Rejected("OP_NOT_PERMITTED", "工程竣工效果只能作用于工程所在地区")
                delta = float(effect.get("delta", 0) or 0)
                if delta <= 0 or delta > max_gain:
                    return Rejected("OP_NOT_PERMITTED", f"工程竣工的地区指标增益须在 0～{max_gain:g} 之间")
                gain += delta
                continue
            nested = check_player_op(effect, state, settings)
            if nested is not None:
                return Rejected("OP_NOT_PERMITTED", f"工程竣工效果不允许：{nested.message}")
        per_point = _number(state, "player_project_cost_per_point", PROJECT_COST_PER_POINT_DEFAULT)
        if gain * per_point > total_cost + 1e-9:
            return Rejected(
                "OP_NOT_PERMITTED",
                f"工程花费不足以支撑竣工增益：每点增益至少花费 {per_point:g}（本工程共花费 {total_cost:g}，增益 {gain:g}）",
            )
    if name == "add_standing_order":
        for raw in getattr(op, "ops", []) or []:
            nested = check_player_op(raw, state, settings)
            if nested is not None:
                return Rejected("OP_NOT_PERMITTED", f"常设令所含操作不允许：{nested.message}")
    if name == "cancel_ledger":
        entry = next((row for row in state.ledger if row.id == getattr(op, "ledger_id", None)), None)
        if entry is not None and not entry.cancellable:
            return Rejected("OP_NOT_PERMITTED", f"账目「{entry.name}」是既定开支，不能以诏令撤销")
    return None
