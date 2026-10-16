"""Scenario-agnostic content lint for scenario packs ("does the text match the state?").

`sim validate` checks structure and references; this script checks the *writing* against the
rules -- the class of bugs that playtests with GPT / Claude kept finding while building 《天宝》:
stale claims in memorial text, raw ids leaking into player-facing text, events that pre-empt the
memorial that was supposed to decide them, option labels that promise what the ops do not do.

    cd backend
    python ../scripts/content_lint.py ../scenarios/_template
    python ../scripts/content_lint.py _template --strict      # warnings also fail (exit 1)

Errors (exit 1):
  E1  unknown references in ops / advisors / memory (character, region, army, office, resource, faction)
  E2  raw ids or snake_case keys in player-facing text (titles, summaries, lines, labels, hints, endings, tutorial, cards)
  E4  memorial variants (same title, or `variant_of`) that are not mutually exclusive via `memorial:<other>` guards
  E5  policy card placeholders that are undefined / params never used / empty cost_hint
  E6  tutorial steps: bad target format, non-consecutive step numbers, or a memorial target not visible that season
Warnings:
  W1  flags read in conditions but never written by any op (typo?), `memorial:<id>` flags for unknown memorials
  W2  text asserts "<office><person>" or "<office>出缺" without a matching office_holder guard (state-guard every claim)
  W3  option label promises an appointment / detachment / march that its ops do not perform
  W4  options that set `intel:*` flags without saying the intel narrows the range only once (unless settings.intel_min_width)
  W5  last decision season: every option moves troops and none lets the player stand pat
  W6  options that raise own army strength without mentioning upkeep (when formulas.upkeep is used)
  W7  hard-coded travel time in text (行程 / 抵达 … n 季) -- path-finding decides it, write "见回执"
  W8  hidden resource named together with a number in text (leaks the hidden value)
  W9  an option with state-changing ops but no `hint`
  W10 far-region memorials (courier late) whose `conditions` contain a turn test (put the season on `turn:` instead)
  W11 regions without `courier_days` while settings.courier / agenda are used
  W12 an event and a memorial option write the same flag, the event can fire no later than the memorial
      appears, and the event does not look at `memorial:<id>` -- the event may pre-empt the decision
      (memorials must precede the events they decide)
  W13 turn codes ("T19", "至 T23") in player-facing text -- write the calendar date instead
  W14 an option / choice changes an army by >= settings.troop_hint_min (default 2000) but its label / hint /
      text does not give that number (5000 / 5,000 / 五千 / 0.5万 all count)

The rules are deliberately conservative; scenario-specific checks (named historical events, faction
politics) belong in the game's own lint, cf. tianbao's tools/content_lint.py.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]

# ---- configurable vocabularies (Chinese defaults; adjust for other languages) ----
APPOINT_WORDS = re.compile(r"任命|拜为|擢为|改任|另择|易帅|换帅|为新帅")
APPOINT_EXEMPT = re.compile(r"不任命|留任|不另任|暂缓|另用政令卡")
DETACH_WORDS = re.compile(r"分兵|偏师|一部|半数")
MARCH_WORDS = re.compile(r"出征|开赴|驰援|移驻|移屯|进军|发兵")
INTEL_CAP_WORDS = re.compile(r"只减半一次|不叠加|不再收窄")
UPKEEP_WORDS = re.compile(r"养兵|军饷|军粮|粮饷|兵费")
TRAVEL_CLAIM = re.compile(r"(行程|抵达|到达|赴任|可至)[^。；，,]{0,6}?(\d+|[一二两三四五六七八九十]+)\s*(季|个月|月|年)")
VACANCY_WORDS = r"(出缺|缺员|空缺|久缺|无人)"
SNAKE_ID = re.compile(r"(?<![A-Za-z0-9_])[a-z][a-z0-9]*_[a-z0-9_]+(?![A-Za-z0-9_])")
RAW_KEY = re.compile(r"\b(memorial|intel|card|_internal):[A-Za-z0-9_:]+")
TARGET_FORMAT = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_*-]+)+$")
MOVE_OPS = {"move_army", "march", "detach"}
APPOINT_OPS = {"appoint", "set_army_commander"}
STATE_OPS_INTERNAL = {"set_flag", "adjust_counter", "set_counter", "remember"}
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?:\.name)?\}")
CARD_CATEGORIES = {"军务", "内政", "人事", "外交", "财计"}


# ---------------------------------------------------------------------------------------------
def load_pack(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {"root": path}
    for name in ("scenario", "characters", "regions", "factions", "armies", "events", "memorials", "policy_cards", "tutorial"):
        file = path / f"{name}.yaml"
        data[name] = (yaml.safe_load(file.read_text(encoding="utf-8")) or []) if file.is_file() else []
    if not isinstance(data["scenario"], dict):
        raise SystemExit(f"{path / 'scenario.yaml'} 不是对象")
    return data


def rows(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key) or []
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def leaves(cond: Any, negated: bool = False) -> Iterator[tuple[dict[str, Any], bool]]:
    """Yield (leaf condition, negated?) for a condition tree."""
    if isinstance(cond, list):
        for item in cond:
            yield from leaves(item, negated)
    elif isinstance(cond, dict):
        if "all" in cond:
            yield from leaves(cond["all"], negated)
        elif "any" in cond:
            yield from leaves(cond["any"], negated)
        elif "not" in cond:
            yield from leaves(cond["not"], not negated)
        elif "kind" in cond:
            yield cond, negated


def ops_in(value: Any) -> Iterator[dict[str, Any]]:
    """All ops in a list, including nested project on_complete and standing-order ops."""
    for op in value or []:
        if not isinstance(op, dict):
            continue
        yield op
        yield from ops_in(op.get("on_complete"))
        yield from ops_in(op.get("ops"))


def earliest_turn(item: dict[str, Any]) -> int:
    """Earliest turn an item can be visible / fire, from `turn`, `when.turn` or positive turn conditions."""
    candidates = [0]
    if isinstance(item.get("turn"), int):
        candidates.append(item["turn"])
    when = item.get("when")
    if isinstance(when, dict) and isinstance(when.get("turn"), int):
        candidates.append(when["turn"])
    for leaf, negated in leaves(item.get("conditions")):
        if leaf.get("kind") == "turn" and not negated and isinstance(leaf.get("value"), int):
            operator = leaf.get("operator", "eq")
            value = int(leaf["value"])
            candidates.append(value + 1 if operator == "gt" else value if operator in ("eq", "gte") else 0)
    return max(candidates)


def guards(item: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [leaf for leaf, negated in leaves(item.get("conditions")) if leaf.get("kind") == kind and not negated]


def flag_guards(item: dict[str, Any], negated_only: bool) -> set[str]:
    return {
        str(leaf.get("key"))
        for leaf, negated in leaves(item.get("conditions"))
        if leaf.get("kind") == "flag" and (negated if negated_only else not negated)
        and (not negated_only or leaf.get("operator", "eq") in ("exists", "eq"))
    }


def memory_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


TURN_CODE = re.compile(r"(?<![A-Za-z_\d])T\d{1,2}(?!\d)")
CN_DIGITS = "零一二三四五六七八九"


def troop_forms(amount: float) -> set[str]:
    """How a troop number may be written: 5000 / 5,000 / 五千 / 0.5万; 15000 / 一万五千 / 1.5万; 20000 / 二万 / 两万."""
    n = int(abs(amount))
    forms = {str(n), f"{n:,}"}
    if n % 1000 == 0 and n < 100000:
        wan, qian = divmod(n // 1000, 10)
        for two in ("二", "两"):
            digits = CN_DIGITS[:2] + two + CN_DIGITS[3:]
            forms.add((digits[wan] + "万" if wan else "") + (digits[qian] + "千" if qian else ""))
        forms.add(f"{n / 10000:g}万")
    return forms


def troop_changes(value: Any, minimum: float) -> Iterator[tuple[str, float]]:
    """(army, delta) for ops that change an army's strength by at least `minimum`: adjust_army, and the troops a detach takes."""
    for op in ops_in(value):
        if op.get("op") == "adjust_army" and isinstance(op.get("delta"), (int, float)) and abs(op["delta"]) >= minimum:
            yield str(op.get("army")), float(op["delta"])
        elif op.get("op") == "detach" and isinstance(op.get("strength"), (int, float)) and op["strength"] >= minimum:
            yield str(op.get("army")), -float(op["strength"])


# ---------------------------------------------------------------------------------------------
class Linter:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.scenario = data["scenario"]
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.characters = {str(row["id"]): row for row in rows(data, "characters") if "id" in row}
        self.regions = {str(row["id"]): row for row in rows(data, "regions") if "id" in row}
        self.armies = {str(row["id"]): row for row in rows(data, "armies") if "id" in row}
        self.factions = {str(row["id"]) for row in rows(data, "factions") if "id" in row}
        self.offices = {str(row["id"]): row for row in self.scenario.get("offices") or [] if isinstance(row, dict)}
        self.resources = {str(row["id"]): row for row in self.scenario.get("resources") or [] if isinstance(row, dict)}
        self.memorials = {str(row["id"]): row for row in rows(data, "memorials") if "id" in row}
        self.events = {str(row["id"]): row for row in rows(data, "events") if "id" in row}
        settings = self.scenario.get("settings") or {}
        self.settings = settings if isinstance(settings, dict) else {}
        self.player_faction = self.settings.get("player_faction") or next(iter(self.factions), None)
        self.all_ids = set(self.characters) | set(self.regions) | set(self.armies) | self.factions | set(self.offices) | set(self.resources) \
            | set(self.memorials) | set(self.events)

    def error(self, code: str, where: str, message: str) -> None:
        self.errors.append(f"[{code}] {where}：{message}")

    def warn(self, code: str, where: str, message: str) -> None:
        self.warnings.append(f"[{code}] {where}：{message}")

    # ---- E1 references --------------------------------------------------------------------
    def check_op_refs(self, ops: Any, where: str) -> None:
        for op in ops_in(ops):
            for key in ("character", "commander"):
                if op.get(key) is not None and str(op[key]) not in self.characters and "{" not in str(op[key]):
                    self.error("E1", where, f"{op.get('op')} 的人物不存在：{op[key]}")
            for key in ("region", "to_region"):
                if op.get(key) is not None and str(op[key]) not in self.regions and "{" not in str(op[key]):
                    self.error("E1", where, f"{op.get('op')} 的地区不存在：{op[key]}")
            if op.get("army") is not None and str(op["army"]) not in self.armies and "{" not in str(op["army"]):
                self.error("E1", where, f"{op.get('op')} 的军队不存在：{op['army']}")
            if op.get("office") is not None and str(op["office"]) not in self.offices and "{" not in str(op["office"]):
                self.error("E1", where, f"{op.get('op')} 的官职不存在：{op['office']}")
            if op.get("op") == "adjust_resource" and str(op.get("resource")) not in self.resources:
                self.error("E1", where, f"adjust_resource 的资源不存在：{op.get('resource')}")
            if op.get("faction") is not None and str(op["faction"]) not in self.factions:
                self.error("E1", where, f"{op.get('op')} 的势力不存在：{op['faction']}")

    # ---- texts ----------------------------------------------------------------------------
    def player_texts(self) -> Iterator[tuple[str, str, dict[str, Any] | None]]:
        """(where, text, owning item) for every player-facing string."""
        for mid, item in self.memorials.items():
            for key in ("title", "summary"):
                if item.get(key):
                    yield f"奏疏 {mid}.{key}", str(item[key]), item
            for advisor in item.get("advisors") or []:
                if isinstance(advisor, dict) and advisor.get("line"):
                    yield f"奏疏 {mid} 谏言 {advisor.get('character')}", str(advisor["line"]), item
            for option in item.get("options") or []:
                if isinstance(option, dict):
                    for key in ("label", "hint"):
                        if option.get(key):  # option texts describe consequences: no state-claim check (owner None)
                            yield f"奏疏 {mid}.{option.get('id')}.{key}", str(option[key]), None
        for eid, event in self.events.items():
            for key in ("title", "text"):
                if event.get(key):
                    yield f"事件 {eid}.{key}", str(event[key]), event
            for choice in event.get("choices") or []:
                if isinstance(choice, dict) and choice.get("text"):
                    yield f"事件 {eid}.{choice.get('id')}", str(choice["text"]), event
        for ending in self.scenario.get("endings") or []:
            if isinstance(ending, dict):
                for key in ("title", "text"):
                    if ending.get(key):
                        yield f"结局 {ending.get('id')}.{key}", str(ending[key]), None
        for objective in self.scenario.get("objectives") or []:
            if isinstance(objective, dict):
                for key in ("title", "detail"):
                    if objective.get(key):
                        yield f"目标 {objective.get('id')}.{key}", str(objective[key]), None
        for index, step in enumerate(self.tutorial()):
            if step.get("text"):
                yield f"tutorial[{index}]", str(step["text"]), None
        for card in rows(self.data, "policy_cards"):
            for key in ("title", "summary", "cost_hint"):
                if card.get(key):
                    yield f"政令卡 {card.get('id')}.{key}", str(card[key]), None
        for cid, character in self.characters.items():
            for key in ("persona", "speech_style"):
                if character.get(key):
                    yield f"人物 {cid}.{key}", str(character[key]), None

    def check_texts(self) -> None:
        hidden = {row.get("name"): rid for rid, row in self.resources.items() if row.get("hidden")}
        for where, text, item in self.player_texts():
            # E2 raw ids / keys
            for match in RAW_KEY.finditer(text):
                self.error("E2", where, f"文字中出现内部键「{match.group(0)}」")
            for token in SNAKE_ID.findall(text):
                self.error("E2", where, f"文字中出现标识符「{token}」（应写显示名）")
            for token in re.findall(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_]*(?![A-Za-z0-9_])", text):
                if token in self.all_ids and len(token) > 2:
                    self.error("E2", where, f"文字中出现实体 id「{token}」（应写显示名）")
            # W7 hard-coded travel time
            match = TRAVEL_CLAIM.search(text)
            if match:
                self.warn("W7", where, f"写死了行程「{match.group(0)}」；实际行程由寻路决定，宜写「行程见回执」")
            # W8 hidden value with a number
            for name in hidden:
                if name and re.search(re.escape(str(name)) + r"\s*(约|为|达|至)?\s*\d", text):
                    self.warn("W8", where, f"隐藏数值「{name}」与具体数字同时出现，会泄露真值")
            # W2 office/person and vacancy claims
            if item is not None:
                self.check_claims(where, text, item)

    def check_claims(self, where: str, text: str, item: dict[str, Any]) -> None:
        holders = {(str(leaf.get("office")), leaf.get("character")) for leaf in guards(item, "office_holder")}
        fixed_t0 = item.get("turn") == 0 and not item.get("expires_after") and "conditions" not in item
        own_ops = list(ops_in(item.get("effects")))
        for choice in item.get("choices") or []:
            if isinstance(choice, dict):
                own_ops += list(ops_in(choice.get("effects", choice.get("ops"))))
        # an event's own effects may create the state it describes (「X 免职，某官出缺」)
        holders |= {(str(op.get("office")), op.get("character")) for op in own_ops if op.get("op") == "appoint"}
        holders |= {(str(op.get("office")), None) for op in own_ops if op.get("op") == "dismiss"}
        for oid, office in self.offices.items():
            name = str(office.get("name") or "")
            if not name:
                continue
            for cid, character in self.characters.items():
                person = str(character.get("name") or "")
                if person and f"{name}{person}" in text and (oid, cid) not in holders and not fixed_t0:
                    if not self.advisor_requires(item, cid, oid):
                        self.warn("W2", where, f"称「{name}{person}」，但条件中没有 office_holder {oid} = {cid} 的守卫")
            if re.search(re.escape(name) + VACANCY_WORDS, text) and (oid, None) not in holders and not fixed_t0:
                self.warn("W2", where, f"称「{name}」出缺，但条件中没有 office_holder {oid} = null 的守卫")

    @staticmethod
    def advisor_requires(item: dict[str, Any], character: str, office: str) -> bool:
        for advisor in item.get("advisors") or []:
            if isinstance(advisor, dict) and advisor.get("character") == character:
                required = (advisor.get("requires") or {}).get("office")
                if required == office or (isinstance(required, list) and office in required):
                    return True
        return False

    # ---- memorials ------------------------------------------------------------------------
    def check_memorials(self) -> None:
        upkeep = bool((self.scenario.get("formulas") or {}).get("upkeep"))
        length = self.scenario.get("game_length") or {}
        last_decision = int(length["last_turn"]) - 1 if isinstance(length, dict) and isinstance(length.get("last_turn"), int) else None
        intel_free = self.settings.get("intel_min_width") is not None
        for mid, item in self.memorials.items():
            for advisor in item.get("advisors") or []:
                if isinstance(advisor, dict) and str(advisor.get("character")) not in self.characters:
                    self.error("E1", f"奏疏 {mid}", f"谏言人不存在：{advisor.get('character')}")
            options = [option for option in item.get("options") or [] if isinstance(option, dict)]
            for option in options:
                where = f"奏疏 {mid}.{option.get('id')}"
                ops = option.get("ops") or []
                self.check_op_refs(ops, where)
                for memory in memory_rows(option.get("memory")):
                    if str(memory.get("character")) not in self.characters:
                        self.error("E1", where, f"memory 人物不存在：{memory.get('character')}")
                self.check_option_text(where, str(option.get("label") or ""), ops, option.get("appoints_via"))
                names = {op.get("op") for op in ops_in(ops)}
                if names - STATE_OPS_INTERNAL and not option.get("hint"):
                    self.warn("W9", where, "选项会改变局势，却没有 hint 说明代价")
                if any(op.get("op") == "set_flag" and str(op.get("key", "")).startswith("intel:") for op in ops_in(ops)):
                    if not intel_free and not INTEL_CAP_WORDS.search(str(option.get("hint") or "")):
                        self.warn("W4", where, "设情报旗标，提示宜写明「只减半一次 / 不叠加」")
                if upkeep and self.raises_own_army(ops) and not UPKEEP_WORDS.search(f"{option.get('label', '')}{option.get('hint', '')}"):
                    self.warn("W6", where, "给本方军队增兵，提示应写明此后的养兵负担")
            if last_decision is not None and options and isinstance(item.get("turn"), int):
                visible_last = item["turn"] <= last_decision <= item["turn"] + int(item.get("expires_after", 0) or 0)
                movers = [option for option in options if any(op.get("op") in MOVE_OPS for op in ops_in(option.get("ops")))]
                if visible_last and movers and len(movers) == len(options):
                    self.warn("W5", f"奏疏 {mid}", "末季仍可能出现，但每个选项都调兵；应另有「维持现状」的选项")
            if item.get("region") in self.regions and self.region_is_far(str(item["region"])):
                if any(leaf.get("kind") == "turn" for leaf, _ in leaves(item.get("conditions"))):
                    self.warn("W10", f"奏疏 {mid}", "远地奏疏晚一季奏到、奏到时条件须仍成立；季数宜写在 turn 上，不要写在 conditions 里")
        self.check_variants()

    def check_option_text(self, where: str, label: str, ops: Any, via: Any = None) -> None:
        """via: the option's `appoints_via: <event id>` -- the appointment happens in a follow-up event choice."""
        names = {op.get("op") for op in ops_in(ops)}
        if APPOINT_WORDS.search(label) and not APPOINT_EXEMPT.search(label) and not names & APPOINT_OPS and not via:
            self.warn("W3", where, f"选项「{label}」说任命 / 易帅，但没有 appoint / set_army_commander")
        if DETACH_WORDS.search(label) and "detach" not in names:
            self.warn("W3", where, f"选项「{label}」说分兵 / 一部，但没有 detach（整军调动请写「整军」）")
        if MARCH_WORDS.search(label) and not names & MOVE_OPS:
            self.warn("W3", where, f"选项「{label}」说出兵 / 移驻，但没有 march / move_army / detach")

    def raises_own_army(self, ops: Any) -> bool:
        for op in ops_in(ops):
            if op.get("op") == "adjust_army" and float(op.get("delta", 0) or 0) > 0:
                army = self.armies.get(str(op.get("army")))
                if army is None or army.get("faction") in (None, self.player_faction):
                    return True
        return False

    def check_variants(self) -> None:
        groups: dict[str, list[str]] = defaultdict(list)
        for mid, item in self.memorials.items():
            if not item.get("variant_of"):
                continue
            if str(item["variant_of"]) not in self.memorials:
                self.error("E4", f"奏疏 {mid}", f"variant_of 指向不存在的奏疏：{item['variant_of']}")
            groups[str(item["variant_of"])].append(mid)
        by_title: dict[str, list[str]] = defaultdict(list)
        for mid, item in self.memorials.items():
            by_title[str(item.get("title"))].append(mid)
        families: list[set[str]] = []
        for root, members in groups.items():
            families.append({root, *members})
        for title, members in by_title.items():
            if len(members) > 1:
                declared = any(set(members) <= family for family in families)
                if not declared:
                    self.error("E4", f"奏疏 {'、'.join(members)}", f"同标题「{title}」的几条奏疏须以 variant_of 声明为同题变体")
                families.append(set(members))
        for family in families:
            for mid in family:
                item = self.memorials.get(mid)
                if item is None:
                    continue
                excluded = flag_guards(item, negated_only=True)
                for other in family - {mid}:
                    if f"memorial:{other}" not in excluded:
                        self.error("E4", f"奏疏 {mid}", f"同题变体须在条件中排除另一支：{{not: {{kind: flag, key: \"memorial:{other}\", operator: exists}}}}")

    # ---- events ---------------------------------------------------------------------------
    def check_events(self) -> None:
        for eid, event in self.events.items():
            self.check_op_refs(event.get("effects"), f"事件 {eid}.effects")
            for choice in event.get("choices") or []:
                if isinstance(choice, dict):
                    self.check_op_refs(choice.get("effects", choice.get("ops")), f"事件 {eid}.{choice.get('id')}")
                    for memory in memory_rows(choice.get("memory")):
                        if str(memory.get("character")) not in self.characters:
                            self.error("E1", f"事件 {eid}.{choice.get('id')}", f"memory 人物不存在：{memory.get('character')}")
        # W12 memorials must precede the events they decide
        memorial_flags: dict[str, set[str]] = defaultdict(set)
        for mid, item in self.memorials.items():
            for option in item.get("options") or []:
                if isinstance(option, dict):
                    for op in ops_in(option.get("ops")):
                        if op.get("op") == "set_flag":
                            memorial_flags[str(op.get("key"))].add(mid)
        for eid, event in self.events.items():
            effects = list(ops_in(event.get("effects")))
            for choice in event.get("choices") or []:
                if isinstance(choice, dict):
                    effects += list(ops_in(choice.get("effects", choice.get("ops"))))
            read = {str(leaf.get("key")) for leaf, _ in leaves(event.get("conditions")) if leaf.get("key") is not None}
            for op in effects:
                # intel flags are meant to have several sources; clearing a flag or reacting to it is not a decision
                key = str(op.get("key", ""))
                if op.get("op") != "set_flag" or key.startswith("intel:") or not op.get("value") or key in read:
                    continue
                for mid in sorted(memorial_flags.get(str(op.get("key")), set())):
                    if f"memorial:{mid}" in read:
                        continue
                    if max(1, earliest_turn(event)) <= earliest_turn(self.memorials[mid]):
                        self.warn("W12", f"事件 {eid}", f"与奏疏 {mid} 同写旗标「{op.get('key')}」，且可能先于该奏疏发生；"
                                  f"事件应读取 memorial:{mid}（在奏疏之后结算），或给出更晚的回合条件")

    # ---- flags ----------------------------------------------------------------------------
    def check_flags(self) -> None:
        written: set[str] = set()

        def collect(ops: Any) -> None:
            for op in ops_in(ops):
                if op.get("op") == "set_flag":
                    written.add(str(op.get("key")))

        for item in self.memorials.values():
            for option in item.get("options") or []:
                if isinstance(option, dict):
                    collect(option.get("ops"))
        for event in self.events.values():
            collect(event.get("effects"))
            for choice in event.get("choices") or []:
                if isinstance(choice, dict):
                    collect(choice.get("effects", choice.get("ops")))
        for card in rows(self.data, "policy_cards"):
            collect(card.get("ops"))
        sources: list[tuple[str, Any]] = [(f"奏疏 {mid}", item.get("conditions")) for mid, item in self.memorials.items()]
        sources += [(f"事件 {eid}", event.get("conditions")) for eid, event in self.events.items()]
        sources += [(f"结局 {row.get('id')}", row.get("conditions")) for row in self.scenario.get("endings") or [] if isinstance(row, dict)]
        sources += [(f"目标 {row.get('id')}", row.get("conditions")) for row in self.scenario.get("objectives") or [] if isinstance(row, dict)]
        sources += [(f"目标 {row.get('id')}.fail", row.get("fail_conditions")) for row in self.scenario.get("objectives") or [] if isinstance(row, dict)]
        sources += [("ai_routes", row.get("conditions")) for row in self.scenario.get("ai_routes") or [] if isinstance(row, dict)]
        length = self.scenario.get("game_length") or {}
        overtime = length.get("overtime_flag") if isinstance(length, dict) else None
        if overtime:
            sources.append(("game_length.overtime_flag", {"kind": "flag", "key": overtime}))
        for where, cond in sources:
            for leaf, _ in leaves(cond):
                if leaf.get("kind") not in ("flag", "flag_age"):
                    continue
                key = str(leaf.get("key") or "")
                if key.startswith("memorial:"):
                    if key.split(":", 1)[1] not in self.memorials:
                        self.warn("W1", where, f"旗标「{key}」指向不存在的奏疏")
                elif key.startswith(("intel:", "card:", "_internal:", "event:", "choice:")):  # engine-written flags
                    continue
                elif key not in written:
                    self.warn("W1", where, f"读取旗标「{key}」，但没有任何 op 设置它（拼写错误？）")

    # ---- policy cards ---------------------------------------------------------------------
    def check_cards(self) -> None:
        seen: set[str] = set()
        for card in rows(self.data, "policy_cards"):
            cid = str(card.get("id"))
            where = f"政令卡 {cid}"
            if cid in seen:
                self.error("E5", where, "id 重复")
            seen.add(cid)
            if card.get("category") not in CARD_CATEGORIES:
                self.warn("E5", where, f"category 宜为 {'/'.join(sorted(CARD_CATEGORIES))}")
            if not str(card.get("cost_hint") or "").strip():
                self.error("E5", where, "cost_hint 不能为空（玩家需要知道代价）")
            params = {str(param.get("name")) for param in card.get("params") or [] if isinstance(param, dict)}
            used = set(PLACEHOLDER.findall(yaml.safe_dump(card.get("ops") or [], allow_unicode=True)))
            for name in sorted(used - params):
                self.error("E5", where, f"ops 用了未定义的参数 {{{name}}}")
            for name in sorted(params - used):
                self.error("E5", where, f"参数 {name} 没有在 ops 中用到")

    # ---- tutorial -------------------------------------------------------------------------
    def tutorial(self) -> list[dict[str, Any]]:
        steps = [row for row in self.scenario.get("tutorial") or [] if isinstance(row, dict)]
        steps += rows(self.data, "tutorial")
        return steps

    def check_tutorial(self) -> None:
        by_turn: dict[int, list[int]] = defaultdict(list)
        for index, step in enumerate(self.tutorial()):
            where = f"tutorial[{index}]"
            target = str(step.get("target") or "")
            if target and not TARGET_FORMAT.match(target):
                self.error("E6", where, f"target「{target}」须形如「画面.元素」")
            if isinstance(step.get("turn"), int) and isinstance(step.get("step"), int):
                by_turn[step["turn"]].append(step["step"])
            match = re.match(r"memorial\.(?:item|option)\.([A-Za-z0-9_]+)", target)
            if match:
                memorial = self.memorials.get(match.group(1))
                if memorial is None:
                    self.error("E6", where, f"target 指向不存在的奏疏 {match.group(1)}")
                elif isinstance(step.get("turn"), int) and earliest_turn(memorial) > step["turn"]:
                    self.error("E6", where, f"奏疏 {match.group(1)} 在第 {step['turn']} 回合尚不可见")
        for turn, steps in by_turn.items():
            if sorted(steps) != list(range(1, len(steps) + 1)):
                self.error("E6", f"tutorial 第 {turn} 回合", f"step 须从 1 连续编号，现为 {sorted(steps)}")

    # ---- courier --------------------------------------------------------------------------
    def region_is_far(self, region_id: str) -> bool:
        courier = self.settings.get("courier") if isinstance(self.settings.get("courier"), dict) else {}
        threshold = float(courier.get("late_threshold_days", 60))
        days = self.regions.get(region_id, {}).get("courier_days")
        return isinstance(days, (int, float)) and days > threshold

    def check_courier(self) -> None:
        if self.settings.get("courier") is None and self.settings.get("agenda") is None:
            return
        for rid, region in self.regions.items():
            if region.get("controller") not in (None, self.player_faction):
                continue
            if not isinstance(region.get("courier_days"), int):
                self.warn("W11", f"地区 {rid}", "未写 courier_days（引擎会按行程估算 3 / 15 / 40 / 70 日，显式写出更可控）")

    # ---- W13 turn codes / W14 troop numbers -------------------------------------------------
    def check_turn_codes(self) -> None:
        texts = list(self.player_texts())
        for objective in self.scenario.get("objectives") or []:
            if isinstance(objective, dict):
                for key in ("title", "detail"):
                    if objective.get(key):
                        texts.append((f"目标 {objective.get('id')}.{key}", str(objective[key]), None))
        for where, text, _ in texts:
            match = TURN_CODE.search(text)
            if match:
                self.warn("W13", where, f"玩家可见文字写了回合代号「{match.group(0)}」，应写成历法（某年某季）")

    def check_troop_hints(self) -> None:
        minimum = float(self.settings.get("troop_hint_min", 2000) or 2000)
        for mid, item in self.memorials.items():
            for option in item.get("options") or []:
                if not isinstance(option, dict):
                    continue
                text = f"{option.get('label', '')}｜{option.get('hint', '')}"
                for army, delta in troop_changes(option.get("ops"), minimum):
                    if not any(form in text for form in troop_forms(delta)):
                        self.warn("W14", f"奏疏 {mid}.{option.get('id')}", f"使 {army} 兵额变动 {delta:+g}，选项与提示未写这一数目")
        for eid, event in self.events.items():
            for choice in event.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                for army, delta in troop_changes(choice.get("effects"), minimum):
                    if not any(form in str(choice.get("text", "")) for form in troop_forms(delta)):
                        self.warn("W14", f"事件 {eid}.{choice.get('id')}", f"使 {army} 兵额变动 {delta:+g}，选项文字未写这一数目")

    def run(self) -> None:
        self.check_texts()
        self.check_memorials()
        self.check_events()
        self.check_flags()
        self.check_cards()
        self.check_tutorial()
        self.check_courier()
        self.check_turn_codes()
        self.check_troop_hints()


def resolve_pack(target: str) -> Path:
    path = Path(target)
    if not path.is_dir():
        path = ROOT / "scenarios" / target
    if not (path / "scenario.yaml").is_file():
        raise SystemExit(f"找不到场景：{target}")
    return path.resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description="Scenario-agnostic content lint")
    parser.add_argument("scenario", nargs="?", default="_template")
    parser.add_argument("--strict", action="store_true", help="警告也算失败")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    linter = Linter(load_pack(resolve_pack(args.scenario)))
    linter.run()
    for line in linter.errors:
        print("错误", line)
    for line in linter.warnings:
        print("警告", line)
    print(f"共 {len(linter.errors)} 个错误、{len(linter.warnings)} 个警告。")
    return 1 if linter.errors or (args.strict and linter.warnings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
