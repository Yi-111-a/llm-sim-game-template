"""政令卡（第 9 轮，CONTRACT §7.4）：剧本 `policy_cards.yaml` 的卡片，玩家选参数即展开为玩家层条款。

卡片：`{id, category: 军务|内政|人事|外交|财计, title, summary, params: [...], ops: [StateOp 模板], cost_hint, requires?, cooldown?}`。
- 参数 `{name, kind: character|office|army|region|amount|choice, label?, filter?, choices?, min?, max?}`；
  ops 模板中字符串恰为 `{name}` 的值换成参数值（amount 为数），字符串中的 `{name}` 换成参数值、`{name.name}` 换成显示名。
- filter（按当前状态过滤可选值）：
  - character：`faction: own|any|<势力>`（缺省 own）、`present: bool`（在京且不在途）、`idle: bool`（不在途）、
    `holds_office: bool`、`can_hold_office: bool`（未达兼职上限）、`commands_army: bool`、`region: id|[id]`、`bloc: id|[id]`、
    `min_trait: {公开特质: 值}`（隐藏特质不可用，validate 报错）。玩家本人与不在世者一律不列。
  - office：`vacant: bool`、`with_army: bool`、`ids: [id]`。
  - army：`faction: own|any|<势力>`（缺省 own）、`idle: bool`、`has_commander: bool`、`min_strength: 数`、`region: id|[id]`。
  - region：`controller: own|enemy|any|<势力>`（缺省 any）、`has_own_army: bool`、`not_capital: bool`、`ids: [id]`。
  - amount：`min` / `max`；choice：`choices: [{value, label}]` 或字符串列表。
- `requires`：条件 DSL（events.matches），不满足时卡片不可用（说明只写阈值，见 describe）。
- `cooldown`：用后 n 季内不可再用；确认诏书时写旗标 `card:<id>` = 当季回合。
"""

from __future__ import annotations

import re
from typing import Any

from simcore.domain.battle import hostile
from simcore.domain.court import capital_region
from simcore.domain.describe import describe_condition
from simcore.domain.events import matches
from simcore.domain.models import WorldState
from simcore.domain.rules import held_offices, max_concurrent_offices, mover_faction, office_army, player_character_id, travel_note

CARD_CATEGORIES = ("军务", "内政", "人事", "外交", "财计")
PARAM_KINDS = ("character", "office", "army", "region", "amount", "choice")
FILTER_KEYS: dict[str, set[str]] = {
    "character": {"faction", "present", "idle", "holds_office", "can_hold_office", "commands_army", "region", "bloc", "min_trait"},
    "office": {"vacant", "with_army", "ids"},
    "army": {"faction", "idle", "has_commander", "min_strength", "region"},
    "region": {"controller", "has_own_army", "not_capital", "ids"},
    "amount": set(),
    "choice": set(),
}
CARD_KEYS = {"id", "category", "title", "summary", "params", "ops", "cost_hint", "requires", "cooldown", "illustration", "tags"}
PARAM_KEYS = {"name", "kind", "label", "filter", "choices", "min", "max", "default"}
_PLACEHOLDER = re.compile(r"\{(\w+)(\.name)?\}")


KIND_LABELS = {"character": "人物", "office": "官职", "army": "军队", "region": "地区", "amount": "数额", "choice": "选项"}


def param_label(param: dict[str, Any]) -> str:
    """参数的显示名：剧本写的 label，缺省按 kind（「军队」「地区」……）。"""
    return str(param.get("label") or KIND_LABELS.get(str(param.get("kind")), param.get("name")))


class CardError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def card_flag(card_id: str) -> str:
    return f"card:{card_id}"


def scenario_cards(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    rows = scenario.get("policy_cards") or []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _as_set(value: Any) -> set[str]:
    if value is None:
        return set()
    return {str(item) for item in value} if isinstance(value, list) else {str(value)}


def _faction_ok(state: WorldState, faction: str | None, wanted: Any) -> bool:
    own = mover_faction(state, None)
    wanted = "own" if wanted is None else str(wanted)
    if wanted == "any":
        return True
    if wanted == "own":
        return own is None or faction == own
    if wanted == "enemy":
        return own is not None and faction is not None and hostile(state, faction, own, state.rules.get("wars") or [])
    return faction == wanted


def param_options(state: WorldState, param: dict[str, Any]) -> list[dict[str, Any]]:
    """参数的可选值 [{id, name, note}]（amount 返回空表，范围见 min / max）。"""
    kind = str(param.get("kind"))
    raw_filter = param.get("filter")
    rule: dict[str, Any] = raw_filter if isinstance(raw_filter, dict) else {}
    rows: list[dict[str, Any]] = []
    if kind == "character":
        player = player_character_id(state)
        capital = capital_region(state)
        for row in state.characters.values():
            if row.id == player or row.status != "alive" or not _faction_ok(state, row.faction, rule.get("faction")):
                continue
            present = not row.in_transit and (capital is None or row.region == capital)
            commands = any(army.commander == row.id for army in state.armies.values() if army.strength > 0)
            checks = {
                "present": present,
                "idle": not row.in_transit,
                "holds_office": bool(held_offices(row)),
                "can_hold_office": len(held_offices(row)) < max_concurrent_offices(state),
                "commands_army": commands,
            }
            if any(key in rule and bool(rule[key]) != value for key, value in checks.items()):
                continue
            if "region" in rule and row.region not in _as_set(rule["region"]):
                continue
            if "bloc" in rule and str(row.bloc) not in _as_set(rule["bloc"]):
                continue
            minimum = rule.get("min_trait") if isinstance(rule.get("min_trait"), dict) else {}
            if any(float(row.traits.get(str(key), 0)) < float(value) for key, value in minimum.items()):
                continue
            where = travel_note(state, row) if row.in_transit else (state.regions[row.region].name if row.region in state.regions else row.region)
            offices = "、".join(state.offices[office].name for office in held_offices(row) if office in state.offices)
            rows.append({"id": row.id, "name": row.name, "note": "；".join(part for part in (offices, where) if part)})
    elif kind == "office":
        for row in state.offices.values():
            if "vacant" in rule and bool(rule["vacant"]) != (row.holder is None):
                continue
            if "with_army" in rule and bool(rule["with_army"]) != bool(office_army(state, row.id)):
                continue
            if "ids" in rule and row.id not in _as_set(rule["ids"]):
                continue
            holder = state.characters.get(str(row.holder)) if row.holder else None
            rows.append({"id": row.id, "name": row.name, "note": f"现任：{holder.name}" if holder else "出缺"})
    elif kind == "army":
        for row in state.armies.values():
            if row.strength <= 0 or not _faction_ok(state, row.faction, rule.get("faction")):
                continue
            if "idle" in rule and bool(rule["idle"]) != (not row.in_transit):
                continue
            if "has_commander" in rule and bool(rule["has_commander"]) != bool(row.commander):
                continue
            if "min_strength" in rule and row.strength < float(rule["min_strength"]):
                continue
            if "region" in rule and row.region not in _as_set(rule["region"]):
                continue
            station = state.regions[row.region].name if row.region in state.regions else row.region
            where = f"在途：{travel_note(state, row)}" if row.in_transit else f"驻{station}"
            rows.append({"id": row.id, "name": row.name, "note": f"兵 {row.strength:g}；{where}"})
    elif kind == "region":
        own = mover_faction(state, None)
        capital = capital_region(state)
        for row in state.regions.values():
            wanted = rule.get("controller", "any")
            if wanted != "any" and not _faction_ok(state, row.controller, wanted):
                continue
            if rule.get("not_capital") and row.id == capital:
                continue
            if "ids" in rule and row.id not in _as_set(rule["ids"]):
                continue
            if rule.get("has_own_army") and not any(
                army.region == row.id and not army.in_transit and army.strength > 0 and (own is None or army.faction == own)
                for army in state.armies.values()
            ):
                continue
            controller = state.factions.get(str(row.controller)) if row.controller else None
            rows.append({"id": row.id, "name": row.name, "note": f"归{controller.name}" if controller else ""})
    elif kind == "choice":
        for item in param.get("choices") or []:
            if isinstance(item, dict):
                rows.append({"id": str(item.get("value", item.get("id"))), "name": str(item.get("label", item.get("value"))), "note": ""})
            else:
                rows.append({"id": str(item), "name": str(item), "note": ""})
    return rows


def card_status(state: WorldState, card: dict[str, Any]) -> tuple[bool, str, int]:
    """(可用, 不可用的原因, 冷却余季)。"""
    cooldown = int(card.get("cooldown") or 0)
    used = state.flags.get(card_flag(str(card.get("id"))))
    left = 0
    if cooldown and isinstance(used, (int, float)) and not isinstance(used, bool):
        left = max(0, cooldown - (state.turn - int(used)))
    if left:
        return False, f"冷却中：尚需 {left} 季方可再用", left
    requires = card.get("requires")
    if requires not in (None, {}, []) and not matches(requires, state):
        return False, f"条件未备：{describe_condition(requires, state)}", 0
    return True, "", 0


def cards_view(state: WorldState, scenario: dict[str, Any]) -> list[dict[str, Any]]:
    """GET /policy_cards：每张卡的公开信息、可用状态与各参数的可选值。"""
    result = []
    for card in scenario_cards(scenario):
        available, reason, left = card_status(state, card)
        params = []
        for param in card.get("params") or []:
            if not isinstance(param, dict):
                continue
            row: dict[str, Any] = {"name": param.get("name"), "kind": param.get("kind"), "label": param_label(param)}
            if param.get("kind") == "amount":
                row.update({"min": param.get("min"), "max": param.get("max"), "default": param.get("default")})
            else:
                row["options"] = param_options(state, param)
            params.append(row)
        result.append({
            "id": card.get("id"),
            "category": card.get("category"),
            "title": card.get("title", card.get("id")),
            "summary": card.get("summary", ""),
            "cost_hint": card.get("cost_hint", ""),
            "cooldown": int(card.get("cooldown") or 0),
            "cooldown_left": left,
            "available": available and all(row.get("options", [None]) for row in params),
            "unavailable_reason": reason or next(
                (f"「{row['label']}」当前无可选之人 / 地" for row in params if "options" in row and not row["options"]), ""
            ),
            "params": params,
            "illustration": card.get("illustration"),
        })
    return result


def _substitute(value: Any, values: dict[str, Any], names: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _substitute(item, values, names) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, values, names) for item in value]
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value)
        if whole and whole.group(1) in values and not whole.group(2):
            return values[whole.group(1)]
        return _PLACEHOLDER.sub(
            lambda match: str(names.get(match.group(1), match.group(0)) if match.group(2) else values.get(match.group(1), match.group(0))),
            value,
        )
    return value


def expand_card(state: WorldState, scenario: dict[str, Any], card_id: str, params: dict[str, Any] | None) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """展开一张卡：返回 (卡片, ops, 参数摘要「禁军→潼关」)。不可用 / 参数不合法时抛 CardError。"""
    card = next((row for row in scenario_cards(scenario) if str(row.get("id")) == str(card_id)), None)
    if card is None:
        ids = "、".join(str(row.get("id")) for row in scenario_cards(scenario)) or "无"
        raise CardError("UNKNOWN_CARD", f"政令卡不存在：{card_id}（可用：{ids}）")
    available, reason, _ = card_status(state, card)
    if not available:
        raise CardError("CARD_UNAVAILABLE", f"「{card.get('title', card_id)}」暂不可用：{reason}")
    given = dict(params or {})
    values: dict[str, Any] = {}
    names: dict[str, str] = {}
    for param in card.get("params") or []:
        if not isinstance(param, dict):
            continue
        name = str(param.get("name"))
        label = param_label(param)
        raw = given.get(name, param.get("default"))
        if raw is None or raw == "":
            raise CardError("CARD_PARAM_MISSING", f"「{card.get('title', card_id)}」缺少参数「{label}」（{name}）")
        if param.get("kind") == "amount":
            try:
                number = float(raw)
            except (TypeError, ValueError) as exc:
                raise CardError("INVALID_CARD_PARAM", f"「{label}」须为数") from exc
            low, high = param.get("min"), param.get("max")
            if (low is not None and number < float(low)) or (high is not None and number > float(high)):
                raise CardError("INVALID_CARD_PARAM", f"「{label}」须在 {low if low is not None else '-∞'}～{high if high is not None else '∞'} 之间")
            values[name] = int(number) if number.is_integer() else number
            names[name] = f"{values[name]:g}" if isinstance(values[name], float) else str(values[name])
            continue
        options = param_options(state, param)
        match = next((row for row in options if row["id"] == str(raw)), None)
        if match is None:
            listing = "、".join(f"{row['id']}（{row['name']}）" for row in options[:12]) or "无"
            raise CardError("INVALID_CARD_PARAM", f"「{label}」不可选 {raw}；可选：{listing}")
        values[name] = match["id"]
        names[name] = match["name"]
    ops = [_substitute(op, values, names) for op in card.get("ops") or [] if isinstance(op, dict)]
    summary = "，".join(
        f"{param_label(param)}：{names[str(param.get('name'))]}"
        for param in card.get("params") or []
        if isinstance(param, dict) and str(param.get("name")) in names
    )
    return card, ops, summary


def card_errors(data: dict[str, Any]) -> list[str]:
    """validate_scenario：policy_cards.yaml 的结构与引用检查。"""
    from simcore.domain.permissions import allowed_player_ops

    rows = data.get("policy_cards")
    if rows is None:
        return []
    if not isinstance(rows, list):
        return ["policy_cards.yaml 顶层必须是列表"]
    scenario = data.get("scenario") if isinstance(data.get("scenario"), dict) else {}
    settings = scenario.get("settings") if isinstance(scenario.get("settings"), dict) else {}
    allowed = allowed_player_ops(settings.get("player_ops"))
    hidden = {str(item) for item in settings.get("hidden_traits") or []}
    errors: list[str] = []
    seen: set[str] = set()
    for index, card in enumerate(rows):
        where = f"policy_cards[{index}]"
        if not isinstance(card, dict):
            errors.append(f"{where} 必须是对象")
            continue
        card_id = str(card.get("id") or "")
        where = f"政令卡 {card_id or index}"
        if not card_id:
            errors.append(f"{where} 缺少 id")
        elif card_id in seen:
            errors.append(f"{where} id 重复")
        seen.add(card_id)
        unknown = sorted(set(card) - CARD_KEYS)
        if unknown:
            errors.append(f"{where} 含未知键：{'、'.join(unknown)}")
        if card.get("category") not in CARD_CATEGORIES:
            errors.append(f"{where} 的 category 须为 {'|'.join(CARD_CATEGORIES)}")
        if not card.get("title"):
            errors.append(f"{where} 缺少 title")
        names: set[str] = set()
        for param in card.get("params") or []:
            if not isinstance(param, dict) or not param.get("name"):
                errors.append(f"{where} 的参数须为带 name 的对象")
                continue
            name = str(param["name"])
            names.add(name)
            kind = param.get("kind")
            if kind not in PARAM_KINDS:
                errors.append(f"{where} 参数 {name} 的 kind 须为 {'|'.join(PARAM_KINDS)}")
                continue
            extra = sorted(set(param) - PARAM_KEYS)
            if extra:
                errors.append(f"{where} 参数 {name} 含未知键：{'、'.join(extra)}")
            rule = param.get("filter")
            if rule is not None:
                if not isinstance(rule, dict):
                    errors.append(f"{where} 参数 {name} 的 filter 须为对象")
                else:
                    bad = sorted(set(rule) - FILTER_KEYS[str(kind)])
                    if bad:
                        errors.append(f"{where} 参数 {name} 的 filter 含未知键：{'、'.join(bad)}")
                    traits = rule.get("min_trait") if isinstance(rule.get("min_trait"), dict) else {}
                    secret = sorted(str(key) for key in traits if str(key) in hidden)
                    if secret:
                        errors.append(f"{where} 参数 {name} 的 filter 不能按隐藏特质过滤：{'、'.join(secret)}")
            if kind == "choice" and not param.get("choices"):
                errors.append(f"{where} 参数 {name} 为 choice，须写 choices")
        ops = card.get("ops")
        if not isinstance(ops, list) or not ops:
            errors.append(f"{where} 须有 ops")
            continue
        for op in ops:
            if not isinstance(op, dict) or not op.get("op"):
                errors.append(f"{where} 的 ops 须为带 op 的对象")
                continue
            if op["op"] not in allowed:
                errors.append(f"{where} 用了非玩家级操作：{op['op']}（政令卡只能用玩家层白名单内的操作）")
            for placeholder in _PLACEHOLDER.findall(repr(op)):
                if placeholder[0] not in names:
                    errors.append(f"{where} 的 ops 引用了未定义的参数：{{{placeholder[0]}}}")
        if card.get("requires") not in (None, {}, []):
            from simcore.domain.events import condition_errors

            errors.extend(condition_errors(card.get("requires"), f"{where}.requires"))
    return errors
