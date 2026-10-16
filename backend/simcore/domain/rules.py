import heapq
from dataclasses import dataclass
from typing import Any

from simcore.domain.calendar import format_calendar
from simcore.domain.events import matches
from simcore.domain.models import Character, WorldState
from simcore.domain.ops import StateOp, describe_op_error, parse_op


@dataclass(frozen=True)
class Ok:
    op: StateOp


@dataclass(frozen=True)
class Rejected:
    code: str
    message: str


def held_offices(character: Character) -> list[str]:
    """人物当前担任的全部官职（兼容只写了主职 office 的旧状态）。"""
    held = list(character.offices)
    if character.office and character.office not in held:
        held.insert(0, character.office)
    return held


DEFAULT_TRAIT_NAMES = {
    "ability": "才干",
    "loyalty": "忠诚",
    "ambition": "野心",
    "fear": "畏惧",
    "favor": "恩宠",
    "courage": "胆略",
    "integrity": "操守",
}
STATUS_NAMES = {"alive": "在世", "dead": "已故", "imprisoned": "系狱", "exiled": "流放", "retired": "致仕"}


def entity_name(state: WorldState, entity_id: Any) -> str:
    """任意实体 id 的显示名：人物 / 势力 / 地区 / 军队 / 官职 / 资源；找不到时原样返回；空 id 返回「无」。"""
    if entity_id is None or entity_id == "":
        return "无"
    key = str(entity_id)
    for table in (state.characters, state.factions, state.regions, state.armies, state.offices, state.resources):
        row = table.get(key)
        if row is not None:
            return str(row.name)
    return key


def stat_name(state: WorldState, stat: str) -> str:
    names = state.rules.get("stat_names") or {}
    return str(names.get(stat, stat)) if isinstance(names, dict) else stat


def parse_stat_range(raw: Any) -> tuple[float, float] | None:
    """剧本 stats[].range：[下限, 上限]（两个数，下限 ≤ 上限）；不合格返回 None。"""
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    try:
        low, high = float(raw[0]), float(raw[1])
    except (TypeError, ValueError):
        return None
    return (low, high) if low <= high else None


def stat_range(state: WorldState, stat: str) -> tuple[float, float] | None:
    """地区指标的取值范围（开局自剧本 stats[].range 拷入 rules.stat_ranges）；未定义为 None（不设限）。"""
    ranges = state.rules.get("stat_ranges") or {}
    return parse_stat_range(ranges.get(stat)) if isinstance(ranges, dict) else None


def clamp_stat(state: WorldState, stat: str, value: float) -> float:
    bounds = stat_range(state, stat)
    return value if bounds is None else min(max(value, bounds[0]), bounds[1])


def stat_legend(state: WorldState) -> str:
    """「繁荣 0–100、治安 0–100……」：有范围的地区指标及其量程（简报、摘要的图例）。"""
    ranges = state.rules.get("stat_ranges") or {}
    if not isinstance(ranges, dict):
        return ""
    parts = []
    for stat in ranges:
        bounds = stat_range(state, stat)
        if bounds is not None:
            parts.append(f"{stat_name(state, stat)} {bounds[0]:g}–{bounds[1]:g}")
    return "、".join(parts)


def trait_name(state: WorldState, trait: str) -> str:
    names = state.rules.get("trait_names") or {}
    if isinstance(names, dict) and trait in names:
        return str(names[trait])
    if trait.startswith("relation:"):
        return f"对{entity_name(state, trait.split(':', 1)[1])}的观感"
    return DEFAULT_TRAIT_NAMES.get(trait, trait)


def player_character_id(state: WorldState, settings: dict[str, Any] | None = None) -> str | None:
    """玩家本人：settings.player_character；未设时取 settings.player_office 的在任者。"""
    sources = [state.rules, settings or {}]
    for source in sources:
        character_id = source.get("player_character")
        if character_id and str(character_id) in state.characters:
            return str(character_id)
    for source in sources:
        office_id = source.get("player_office")
        if office_id and str(office_id) in state.offices:
            return state.offices[str(office_id)].holder
    return None


AUDIENCE_MODES = {"in_person": "面见", "written": "书面奏对", "envoy": "遣使问对"}


def court_region(state: WorldState, settings: dict[str, Any] | None = None) -> str | None:
    """御前所在：玩家本人（君主）所在地区；无玩家人物时取 settings / rules 的 capital_region。"""
    player = player_character_id(state, settings)
    if player and player in state.characters:
        return state.characters[player].region
    for source in (state.rules, settings or {}):
        region = source.get("capital_region")
        if region and str(region) in state.regions:
            return str(region)
    return None


def audience_mode(state: WorldState, character_id: str, settings: dict[str, Any] | None = None) -> dict[str, str]:
    """召对方式：人在御前（与君主同地、不在途）为「面见」；远在他处为「书面奏对」（以奏疏问答）；在途为「遣使问对」。

    返回 {mode: in_person|written|envoy, label, note}；note 为给扮演者的一句交代（不含内部 id）。
    """
    character = state.characters[character_id]
    court = court_region(state, settings)
    court_name = state.regions[court].name if court in state.regions else "京师"
    where = state.regions[character.region].name if character.region in state.regions else character.region
    if character.in_transit:
        return {
            "mode": "envoy",
            "label": AUDIENCE_MODES["envoy"],
            "note": f"{character.name}正在途中（{travel_note(state, character)}），不在御前：遣使赍问，就地书面回奏，非当面应对。",
        }
    if court is None or character.region == court:
        return {"mode": "in_person", "label": AUDIENCE_MODES["in_person"], "note": f"{character.name}在{court_name}，入对御前，当面奏对。"}
    return {
        "mode": "written",
        "label": AUDIENCE_MODES["written"],
        "note": f"{character.name}人在{where}，不在御前（{court_name}）：以书面奏对，驿递往还，非当面应对，召对并不令其入京。",
    }


TURN_UNIT_LABELS = {"month": "月", "season": "季", "year": "年"}


def turn_unit_label(state: WorldState) -> str:
    """回合单位的显示名（rules.turn_unit_label，或按 rules.turn_unit 推断；旧存档缺省为「回合」）。"""
    label = state.rules.get("turn_unit_label")
    if label:
        return str(label)
    return TURN_UNIT_LABELS.get(str(state.rules.get("turn_unit", "")), "回合")


def turn_label(state: WorldState, turn: Any, short: bool = False) -> str:
    """玩家可见的回合标签：「天宝十四载冬（第19回合）」；short 只给历法（「天宝十四载冬」）。

    历法由 rules.calendar_config / calendar_start_index（开局写入）推算；推算不了（无年号、非按季计时、旧存档）时退回「第19回合」。
    """
    number = int(float(turn))
    base = ""
    if number == state.turn and state.calendar.label:
        base = state.calendar.label
    else:
        config = state.rules.get("calendar_config")
        index = state.rules.get("calendar_start_index")
        if isinstance(config, dict) and config.get("eras") and isinstance(index, int):
            seasons = config.get("seasons") or ["春", "夏", "秋", "冬"]
            try:
                base = format_calendar(number, seasons[(index + number) % len(seasons)], config, index)
            except (KeyError, ValueError, IndexError):
                base = ""
    if not base:
        return f"第{number}回合"
    return base if short else f"{base}（第{number}回合）"


def delay_phrase(due_turn: Any = None) -> str:
    """延期条款的统一说法：「延至下季结算时执行（第 n 回合）」（回执、事实、简报、复盘日志都用它）。"""
    return f"延至下季结算时执行（第 {due_turn} 回合）" if due_turn is not None else "延至下季结算时执行"


def mover_faction(state: WorldState, faction: str | None) -> str | None:
    return faction or (str(state.rules["player_faction"]) if state.rules.get("player_faction") else None)


def find_route(state: WorldState, origin: str, destination: str, faction: str | None = None) -> tuple[list[str], int] | None:
    """按邻接表寻路：行程回合数最短；同样短时优先途经本方控制的地区，再取途经地区最少、id 字典序最小者（确定性）。

    返回 (含起点与终点的路径, 总回合数)；不可达返回 None。faction 为行进者所属势力（缺省为玩家势力）。
    """
    if origin not in state.regions or destination not in state.regions:
        return None
    if origin == destination:
        return [origin], 0
    own = mover_faction(state, faction)

    def foreign(region_id: str) -> int:
        controller = state.regions[region_id].controller
        return 0 if own is None or controller in (None, own) else 1

    frontier: list[tuple[int, int, int, list[str]]] = [(0, 0, 0, [origin])]
    best: dict[str, tuple[int, int, int]] = {origin: (0, 0, 0)}
    while frontier:
        turns, foreign_count, hops, path = heapq.heappop(frontier)
        here = path[-1]
        if here == destination:
            return path, turns
        if best.get(here, (turns, foreign_count, hops)) < (turns, foreign_count, hops):
            continue
        for neighbor, cost in sorted(state.regions[here].adjacency.items()):
            if neighbor not in state.regions or neighbor in path:
                continue
            key = (turns + max(0, int(cost)), foreign_count + foreign(neighbor), hops + 1)
            if neighbor in best and best[neighbor] <= key:
                continue
            best[neighbor] = key
            heapq.heappush(frontier, (*key, [*path, neighbor]))
    return None


def travel_note(state: WorldState, mover: Any) -> str:
    """在途者的行程说明：「自A赴B，约n季后到」（不在途返回空串）。mover.region 在抵达前始终是出发地。"""
    transit = getattr(mover, "in_transit", None)
    if not transit:
        return ""
    origin = state.regions.get(str(transit.get("from_region") or mover.region))
    target = state.regions.get(str(transit.get("to_region")))
    left = int(transit.get("eta_turn", state.turn)) - state.turn
    when = f"约{left}{turn_unit_label(state)}后到" if left > 0 else "即将抵达"
    origin_name = origin.name if origin else str(mover.region)
    target_name = target.name if target else str(transit.get("to_region"))
    return f"自{origin_name}赴{target_name}，{when}"


def transit_label(state: WorldState, mover: Any) -> str:
    """表格用的行程：「在途：A→B，约n季后到」（不在途返回空串）。在途者尚未抵达，不能写成「驻B」。"""
    transit = getattr(mover, "in_transit", None)
    if not transit:
        return ""
    origin = state.regions.get(str(transit.get("from_region") or mover.region))
    target = state.regions.get(str(transit.get("to_region")))
    left = int(transit.get("eta_turn", state.turn)) - state.turn
    when = f"约{left}{turn_unit_label(state)}后到" if left > 0 else "即将抵达"
    return f"在途：{origin.name if origin else mover.region}→{target.name if target else transit.get('to_region')}，{when}"


def army_whereabouts(state: WorldState, army: Any) -> str:
    """军队现在何处：在途为「在途：A→B，约n季后到」，否则「驻A」。"""
    region = state.regions.get(str(army.region))
    return transit_label(state, army) or f"驻{region.name if region else army.region}"


def traveller_label(state: WorldState, mover: Any) -> str:
    """「某某（自A赴B，约n季后到）」；不在途时只给名字。"""
    note = travel_note(state, mover)
    return f"{mover.name}（{note}）" if note else str(mover.name)


def army_station(army: Any) -> str:
    """军队的落脚地：在途时为目的地，否则为驻地。"""
    return str(army.in_transit.get("to_region")) if army.in_transit else str(army.region)


def commander_presence(state: WorldState, army: Any) -> dict[str, Any]:
    """统帅是否在军中：{commander, name, present, note}。

    统帅与军队同驻一地（或同一行程同时抵达）为在军；否则「赴任中 / 不在军中」，作战时由副将暂摄。
    """
    commander = state.characters.get(army.commander) if army.commander else None
    if commander is None:
        return {"commander": None, "name": "", "present": False, "note": "无统帅"}
    if army.in_transit or commander.in_transit:
        together = bool(army.in_transit and commander.in_transit) and (
            army.in_transit.get("to_region") == commander.in_transit.get("to_region")
            and army.in_transit.get("eta_turn") == commander.in_transit.get("eta_turn")
        )
    else:
        together = commander.region == army.region
    if together:
        return {"commander": commander.id, "name": commander.name, "present": True, "note": ""}
    if commander.in_transit and commander.in_transit.get("to_region") == army_station(army):
        note = f"主帅{commander.name}赴任中（{travel_note(state, commander)}），由副将暂摄"
    else:
        where = state.regions.get(commander.region)
        note = f"主帅{commander.name}遥领，不在军中（今在{where.name if where else commander.region}），由副将暂摄"
    return {"commander": commander.id, "name": commander.name, "present": False, "note": note}


def office_posting(state: WorldState, office_id: str) -> dict[str, str]:
    """官职的任所定义（rules.office_postings，来自剧本 offices 的 army / seat）；京官、虚衔为空字典。"""
    postings = state.rules.get("office_postings") or {}
    row = postings.get(office_id) if isinstance(postings, dict) else None
    return dict(row) if isinstance(row, dict) else {}


def office_army(state: WorldState, office_id: str) -> str | None:
    """此职所统军队 id（剧本写了 army 且军队存在时）。"""
    army = office_posting(state, office_id).get("army")
    return army if army and army in state.armies else None


def office_station(state: WorldState, office_id: str) -> str | None:
    """此职的任所地区：seat，否则所统军队的落脚地；京官、虚衔为 None。"""
    seat = office_posting(state, office_id).get("seat")
    if seat and seat in state.regions:
        return seat
    army = office_army(state, office_id)
    return army_station(state.armies[army]) if army else None


def posting_plan(state: WorldState, character: Any, station: str, dispatch: bool, *, army: Any = None) -> dict[str, Any]:
    """任命（任帅 / 有任所的官职）之后此人是否赴任，以及回执与事实统一使用的说明（不改状态）。

    返回 {kind, text, route}：kind 为 present（已在任所）/ en_route（已在赴任途中）/ dispatch（启程赴任，route 为
    (路径, 回合数)）/ remote（遥领）。text 为事实后缀（不含前导分号），一律写成「已启程赴任（…）」或「遥领，不赴任（…）」，
    统兵时附「由副将暂摄」。
    """
    deputy = "，由副将暂摄" if army is not None else ""
    unit = turn_unit_label(state)
    destination = state.regions[station].name if station in state.regions else station
    transit = character.in_transit
    if transit is None and character.region == station:
        return {"kind": "present", "text": "人在军中，即日视事" if army is not None else "人在任所，即日视事", "route": None}
    if transit and transit.get("to_region") == station:
        return {"kind": "en_route", "text": f"已在赴任途中（{travel_note(state, character)}）{deputy}", "route": None}
    if dispatch and transit is None and character.status == "alive":
        found = find_route(state, character.region, station, character.faction)
        if found is not None:
            path, turns = found
            turns = max(1, turns)
            origin = state.regions[character.region].name if character.region in state.regions else character.region
            via = [state.regions[region_id].name for region_id in path[1:-1]]
            detail = f"自{origin}" + (f"经{'、'.join(via)}" if via else "") + f"，约{turns}{unit}抵{destination}"
            kept, released = left_behind_armies(state, character, army)
            behind = left_behind_text(kept, released) + _elsewhere_text(state, character, army, kept, released)
            return {"kind": "dispatch", "text": f"已启程赴任（{detail}），到任前由副将暂摄{behind}" if army is not None
                    else f"已启程赴任（{detail}）{behind}", "route": (path, turns), "release": [row.id for row in released]}
        return {"kind": "remote", "text": f"遥领，不赴任（无路可达{destination}）{deputy}", "route": None}
    if transit:
        where = f"正在途中：{travel_note(state, character)}"
    else:
        region = state.regions.get(character.region)
        where = f"今在{region.name if region else character.region}"
    return {"kind": "remote", "text": f"遥领，不赴任（{where}）{deputy}{_elsewhere_text(state, character, army, [], [])}", "route": None}


def _elsewhere_text(state: WorldState, character: Any, army: Any, kept: list[Any], released: list[Any]) -> str:
    """此人另统、且不在身边的军队（遥领之军）照旧由其兼统：「；仍兼领安西军（不在军中，副将暂摄）」，免得回执只说一军而状态里是两军。

    只列此刻确不在军中的军队：与此人同驻（在军）的军队这次并无变化——遥领新职时人不动，原军仍在其身边，
    不能说成「不在军中」（gpt-08：兼领河西的预览误称陇右军也无主帅在军）。"""
    skip = {row.id for row in (*kept, *released)} | ({army.id} if army is not None else set())
    names = [
        row.name
        for row in state.armies.values()
        if row.commander == character.id and row.id not in skip and row.strength > 0 and not commander_presence(state, row)["present"]
    ]
    return f"；仍兼领{'、'.join(names)}（不在军中，副将暂摄）" if names else ""


def left_behind_armies(state: WorldState, character: Any, army: Any = None) -> tuple[list[Any], list[Any]]:
    """此人启程他往时留在原地的所统军队，分为 (仍兼统, 解除统领)。

    一条规则：人离军而去，原统之军即解除其统帅（army.commander = None，副将摄事）；唯此军是他仍担任的官职所统
    （offices 的 army），官在军在，仍由其兼统（人不在军中，副将暂摄）。只看与此人同驻一地、不在途的军队。
    """
    tied = {office_army(state, office) for office in held_offices(character)}
    kept: list[Any] = []
    released: list[Any] = []
    for row in state.armies.values():
        if row.commander != character.id or row is army or row.in_transit or row.region != character.region:
            continue
        (kept if row.id in tied else released).append(row)
    return kept, released


def left_behind_text(kept: list[Any], released: list[Any]) -> str:
    """留下之军的说明（事实 / 回执后缀，带前导分号）。"""
    parts = []
    if released:
        parts.append(f"其原统{'、'.join(row.name for row in released)}不再由其统领（暂无主帅，副将摄事）")
    if kept:
        parts.append(f"仍兼统{'、'.join(row.name for row in kept)}（人不在军中，副将暂摄）")
    return "".join(f"；{part}" for part in parts)


def coalesce_postings(ops: list[Any], state: WorldState) -> list[Any]:
    """同一组操作（奏疏选项、抉择效果、事件效果）里的任命与易帅合并为一条最终态，避免先说「遥领，不赴任」又说「已启程赴任」。

    规则：appoint 某人为带 army 的官职（未写 dispatch 或为 false），其后同组又有 set_army_commander 令此人统该军且
    dispatch: true → appoint 改为 dispatch: true（启程赴任、兼统该军），并删去那条多余的 set_army_commander。
    其余操作原样保留，顺序不变；不改传入的列表。
    """
    result = list(ops)
    for index, raw in enumerate(result):
        if not isinstance(raw, dict) or raw.get("op") != "appoint" or raw.get("dispatch"):
            continue
        army_id = office_army(state, str(raw.get("office")))
        if army_id is None:
            continue
        for later in range(index + 1, len(result)):
            other = result[later]
            if (
                isinstance(other, dict)
                and other.get("op") == "set_army_commander"
                and other.get("army") == army_id
                and other.get("character") == raw.get("character")
                and other.get("dispatch")
            ):
                result[index] = {**raw, "dispatch": True}
                del result[later]
                break
    return result


def initial_offices(scenario: dict[str, Any] | None, character_id: str) -> list[str] | None:
    """剧本开局时此人的官职（characters.yaml 的 office / offices）；剧本里找不到此人时为 None。"""
    for row in (scenario or {}).get("characters", []) or []:
        if isinstance(row, dict) and str(row.get("id")) == character_id:
            held = [str(item) for item in row.get("offices") or []]
            if row.get("office") and str(row["office"]) not in held:
                held.insert(0, str(row["office"]))
            return held
    return None


def current_title(state: WorldState, character_id: str, scenario: dict[str, Any] | None = None) -> tuple[str, str]:
    """(当前头衔, 旧衔)：官职与开局不同（新任、改任、罢免）时，头衔取现任官职名，静态 title 作为旧衔（履历）；未变时照旧。"""
    character = state.characters[character_id]
    held = held_offices(character)
    start = initial_offices(scenario, character_id)
    static = str(character.title or "")
    if start is None or set(start) == set(held):
        return static, ""
    names = [state.offices[office].name for office in held if office in state.offices]
    title = "、".join(names) if names else "无现职"
    return title, static if static and static != title else ""


def max_concurrent_offices(state: WorldState) -> int:
    try:
        return max(1, int(state.rules.get("max_concurrent_offices", 1)))
    except (TypeError, ValueError):
        return 1


def validate(op_value: StateOp | dict[str, Any], state: WorldState) -> Ok | Rejected:
    try:
        op = parse_op(op_value)
    except (ValueError, TypeError) as exc:
        return Rejected("INVALID_TARGET", describe_op_error(exc, op_value))
    if state.outcome is not None:
        return Rejected("GAME_OVER", f"本局已终局（{state.outcome.title}），不能再改变世界状态")

    if op.op == "adjust_resource" and op.resource not in state.resources:
        return Rejected("UNKNOWN_ENTITY", f"未知资源：{op.resource}")
    if op.op == "add_ledger" and op.category not in state.resources:
        return Rejected("UNKNOWN_ENTITY", f"账目资源不存在：{op.category}")
    if op.op == "cancel_ledger" and not any(row.id == op.ledger_id for row in state.ledger):
        return Rejected("UNKNOWN_ENTITY", f"账目不存在：{op.ledger_id}")
    if op.op in {"appoint", "dismiss"}:
        character = state.characters.get(op.character)
        office = state.offices.get(op.office)
        if character is None or office is None:
            return Rejected("UNKNOWN_ENTITY", "人物或官职不存在")
        if op.op == "appoint":
            if character.status != "alive" or character.in_transit:
                return Rejected("CHARACTER_UNAVAILABLE", "人物当前不能任职")
            if office.holder not in (None, op.character):
                return Rejected("OFFICE_OCCUPIED", "该官职已有任职者")
            held = held_offices(character)
            limit = max_concurrent_offices(state)
            if op.office not in held and len(held) >= limit:
                if limit == 1:
                    return Rejected("OFFICE_HELD", "人物已担任其他官职，需先免去原职")
                return Rejected("OFFICE_HELD", f"人物已兼任 {len(held)} 职，达到上限 {limit}")
            if any(
                character.traits.get(key, 0) < value
                for key, value in office.requires.items()
                if isinstance(value, (int, float))
            ):
                return Rejected("OFFICE_REQUIREMENT", "人物不满足任职条件")
        elif op.office not in held_offices(character) or office.holder != op.character:
            return Rejected("INVALID_TARGET", "该人物并未担任此官职")
    if op.op == "set_character_status" and op.character not in state.characters:
        return Rejected("UNKNOWN_ENTITY", f"人物不存在：{op.character}")
    if op.op == "move_character":
        character = state.characters.get(op.character)
        if character is None or op.to_region not in state.regions:
            return Rejected("UNKNOWN_ENTITY", "人物或目标地区不存在")
        if character.status != "alive":
            return Rejected("CHARACTER_UNAVAILABLE", "人物当前不能行动")
        if character.in_transit:
            if character.in_transit.get("to_region") == op.to_region:
                return Rejected("INVALID_TARGET", f"{character.name}已在途中（{travel_note(state, character)}），无须再下令")
            return Rejected("CHARACTER_UNAVAILABLE", f"{character.name}正在途中（{travel_note(state, character)}），途中不能另行调遣")
        if op.to_region == character.region:
            return Rejected("INVALID_TARGET", f"{character.name}已在{state.regions[op.to_region].name}")
        if find_route(state, character.region, op.to_region, character.faction) is None:
            return Rejected("OUT_OF_RANGE", f"{character.name}无路可达{state.regions[op.to_region].name}")
    if op.op in {"move_army", "march"}:
        army = state.armies.get(op.army)
        if army is None or op.to_region not in state.regions:
            return Rejected("UNKNOWN_ENTITY", "军队或目标地区不存在")
        if army.in_transit:
            return Rejected("CHARACTER_UNAVAILABLE", f"{army.name}正在行军（{travel_note(state, army)}），途中不能另行调遣")
        if op.to_region == army.region:
            return Rejected("INVALID_TARGET", f"{army.name}已驻{state.regions[op.to_region].name}")
        if find_route(state, army.region, op.to_region, army.faction) is None:
            return Rejected("OUT_OF_RANGE", f"{army.name}无路可达{state.regions[op.to_region].name}")
    if op.op == "detach":
        army = state.armies.get(op.army)
        if army is None or op.to_region not in state.regions:
            return Rejected("UNKNOWN_ENTITY", "军队或目标地区不存在")
        if army.in_transit:
            return Rejected("CHARACTER_UNAVAILABLE", f"{army.name}正在行军（{travel_note(state, army)}），途中不能分兵")
        if not op.strength > 0:
            return Rejected("INVALID_TARGET", "分兵数须为正数")
        if op.strength >= army.strength:
            return Rejected(
                "INVALID_TARGET",
                f"{army.name}现有兵力 {army.strength:g}，分兵 {op.strength:g} 须少于全军；全军调动请用 move_army",
            )
        if op.to_region != army.region and find_route(state, army.region, op.to_region, army.faction) is None:
            return Rejected("OUT_OF_RANGE", f"{army.name}无路可达{state.regions[op.to_region].name}")
        if op.new_army_name is not None and not op.new_army_name.strip():
            return Rejected("INVALID_TARGET", "新军名不能为空")
        if op.commander is not None:
            leader = state.characters.get(op.commander)
            if leader is None:
                return Rejected("UNKNOWN_ENTITY", f"人物不存在：{op.commander}")
            if leader.status != "alive":
                return Rejected("CHARACTER_UNAVAILABLE", f"{leader.name}不在世或不能统兵")
    if op.op == "start_project":
        if op.region not in state.regions or op.turns <= 0:
            return Rejected("INVALID_TARGET", "工程地区无效或工期必须大于零")
        for resource, cost in op.cost_per_turn.items():
            current = state.resources.get(resource)
            if current is None:
                return Rejected("UNKNOWN_ENTITY", f"工程资源不存在：{resource}")
            committed = sum(project.cost_per_turn.get(resource, 0) for project in state.projects)
            if current.amount - committed < cost:
                return Rejected("INSUFFICIENT_RESOURCE", f"{current.name}不足，无法支付首期工程款")
    if op.op == "adjust_region" and (
        op.region not in state.regions or op.stat not in state.regions[op.region].stats
    ):
        return Rejected("INVALID_TARGET", "地区或地区指标不存在")
    if op.op == "adjust_relation":
        entities = set(state.characters) | set(state.factions)
        if op.a not in entities or op.b not in entities:
            return Rejected("UNKNOWN_ENTITY", "关系对象不存在")
    if op.op == "cancel_standing_order" and not any(
        row.id == op.id for row in state.standing_orders
    ):
        return Rejected("UNKNOWN_ENTITY", f"常设命令不存在：{op.id}")
    if op.op == "remember" and op.character not in state.characters:
        return Rejected("UNKNOWN_ENTITY", f"人物不存在：{op.character}")
    if op.op in {"adjust_army", "set_army_faction", "set_army_commander", "march"} and op.army not in state.armies:
        return Rejected("UNKNOWN_ENTITY", f"军队不存在：{op.army}")
    if op.op == "set_army_faction" and op.faction not in state.factions:
        return Rejected("UNKNOWN_ENTITY", f"势力不存在：{op.faction}")
    if op.op == "set_character_faction":
        if op.character not in state.characters:
            return Rejected("UNKNOWN_ENTITY", f"人物不存在：{op.character}")
        if op.faction not in state.factions:
            return Rejected("UNKNOWN_ENTITY", f"势力不存在：{op.faction}")
    if op.op == "set_army_commander" and op.character is not None:
        commander = state.characters.get(op.character)
        if commander is None:
            return Rejected("UNKNOWN_ENTITY", f"人物不存在：{op.character}")
        if commander.status != "alive":
            return Rejected("CHARACTER_UNAVAILABLE", f"{commander.name}不在世或不能统兵")
    if op.op == "adjust_trait" and op.character not in state.characters:
        return Rejected("UNKNOWN_ENTITY", f"人物不存在：{op.character}")
    if op.op == "set_region_controller":
        if op.region not in state.regions:
            return Rejected("UNKNOWN_ENTITY", f"地区不存在：{op.region}")
        if state.factions and op.faction not in state.factions:
            return Rejected("UNKNOWN_ENTITY", f"势力不存在：{op.faction}")
    if op.op == "choose_event_option":
        choice = next((row for row in state.pending_choices if row.id == op.choice_id), None)
        if choice is None:
            return Rejected("UNKNOWN_ENTITY", f"待决事件不存在或已处置：{op.choice_id}")
        option = next((row for row in choice.options if row.id == op.option_id), None)
        if option is None:
            return Rejected("UNKNOWN_ENTITY", f"「{choice.title}」没有选项：{op.option_id}")
        if not matches(option.conditions, state):
            return Rejected("CONDITION_FAILED", f"选项「{option.text}」的前提条件尚不满足")
    return Ok(op)
