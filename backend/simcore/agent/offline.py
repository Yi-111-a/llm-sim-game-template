"""离线（mock / 无 API key）路径的确定性文本：纪事、召对答话、廷议台词。

全部只用引擎给出的事实与数字：纪事取自（已脱敏的）TurnReport 与本季诏令来源，召对与廷议取自该人物可知的
StateDigest；措辞按种子在模板间轮换。剧本可以覆盖模板：scenario.yaml 的 chronicle_templates / audience_templates
块，或剧本 prompts/ 目录下的 chronicle_templates.yaml / audience_templates.yaml（同名键整组替换默认模板）。
"""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Any

import yaml

from simcore.domain.models import TurnReport, WorldState
from simcore.domain.rules import current_title, initial_offices
from simcore.domain.visibility import format_number

CHRONICLE_TEMPLATES: dict[str, list[str]] = {
    "opening": ["是季，", "是季，", "是时，"],
    "events": ["{items}，相继以闻", "有{items}之事", "{items}，事闻于朝"],
    "battle": ["{attacker}与{defender}战于{region}，{winner}胜", "{attacker}兵犯{region}，{defender}拒之，{winner}胜"],
    "battle_captured": ["{attacker}与{defender}战于{region}，{attacker}胜，遂取其地", "{attacker}攻{region}，{defender}不能守，地入于{attacker}"],
    "stalemate": ["{attacker}攻{region}，{defender}据险固守，相持不下", "{attacker}兵临{region}，{defender}坚壁，两军相持"],
    "loss": ["{army}自{before}折至{after}", "{army}由{before}减为{after}"],
    "edicts": ["诏行{items}", "朝廷从{items}之议", "{items}，有诏施行"],
    "edicts_plain": ["另有诏令颁行", "又颁诏令"],
    "choice": ["「{title}」一事，诏从「{option}」", "处分「{title}」，从「{option}」"],
    "appointments": ["{items}", "人事有更：{items}", "除拜有差：{items}"],
    "movements": ["{items}", "道路之间，{items}"],
    "ledger": ["有司会计，{items}", "度支所记，{items}", "计簿所载，{items}"],
    "ledger_item": ["{name}净{direction}{amount}{unit}"],
    "others": ["{items}", "又，{items}"],
    "outcome": ["事遂至「{title}」", "终至「{title}」"],
    "closing": ["余无足纪。", "他事从略。", "余事如常，不具书。"],
    "empty": ["是季，簿书所载，别无大事。余无足纪。", "是季，四方奏报，未见大故。他事从略。"],
}

AUDIENCE_TEMPLATES: dict[str, list[str]] = {
    "address": ["陛下"],
    "opener": ["{addr}垂问，{self}敢不尽言。", "{self}谨对。", "{addr}既问及此，{self}斗胆直陈。"],
    "support": ["{self}以为此事可行。", "依{self}愚见，此议可从。", "{self}以为可。"],
    "oppose": ["{self}窃以为不可。", "{self}以为此事未可轻动。", "依{self}看来，此事尚须三思。"],
    "neutral": ["此事{self}未敢遽断。", "利害参半，{self}不敢妄言。", "{self}以为宜从容计议。"],
    "self_interest": ["事涉{self}本职，不敢不言。"],
    "fact_news": ["近闻{fact}。", "据{self}所知，{fact}。", "今{fact}，不可不察。"],
    "fact_memory": ["{self}犹记，{fact}。", "{self}记得，{fact}。"],
    "fact_state": ["今{fact}。", "据{self}所知，{fact}。"],
    "fact_rumor": ["风闻{fact}。", "人言{fact}。"],
    "fact_more": ["又，{fact}。", "且{fact}。"],
    "close_support": ["伏惟{addr}圣裁。", "愿{addr}早定。"],
    "close_oppose": ["愿{addr}三思。", "伏乞{addr}慎之。"],
    "close_neutral": ["唯{addr}圣断。", "伏候{addr}裁夺。"],
    "agree_previous": ["{prev}所言甚是。", "{prev}之论，{self}亦以为然。"],
    "differ_previous": ["{prev}之言，{self}未敢苟同。", "{prev}所论，{self}以为未尽。"],
    "debate_support": ["{self}附议。", "此议甚善，{self}以为可行。", "{self}以为当从此议。"],
    "debate_oppose": ["{self}以为不可。", "此议{self}不敢苟同。", "{self}窃以为未可轻动。"],
    "debate_neutral": ["此事利害相参，{self}以为宜再详议。", "{self}未敢遽断。", "{self}以为当从容计议。"],
}

SELF_TERMS = ("臣胡人", "老奴", "儿臣", "奴婢", "末将", "下官", "老臣", "微臣", "小臣", "本王", "妾", "朕", "孤", "臣")
ADDRESS_TERMS = ("父皇", "大家", "三郎", "陛下", "主上", "官家", "大王", "殿下")

_DIGITS = re.compile(r"\d+(?:\.\d+)?")
_ARROW = re.compile(r"（-?\d+(?:\.\d+)?→-?\d+(?:\.\d+)?）")
_DELTA_FACT = re.compile(r"^(?P<name>.+?) (?P<sign>[+−-])(?P<num>\d+(?:\.\d+)?)(?:（(?P<cause>[^（）]+)）)?$")
_CN_DIGITS = "零一二三四五六七八九"
_TEMPLATE_CACHE: dict[tuple[str, str], dict[str, list[str]]] = {}


# ---- 通用 ---------------------------------------------------------------------


def is_offline(provider: Any) -> bool:
    """provider（或其包装层 .provider）是否为内置离线 provider（mock）。"""
    for _ in range(5):
        if provider is None:
            return False
        if getattr(provider, "offline", False) is True:
            return True
        provider = getattr(provider, "provider", None)
    return False


def templates(scenario: dict[str, Any] | None, block: str, defaults: dict[str, list[str]]) -> dict[str, list[str]]:
    """默认模板 + 剧本覆盖（scenario.yaml 的同名块，或 prompts/<block>.yaml）。"""
    scenario = scenario or {}
    root = str(scenario.get("root") or "")
    key = (root, block)
    if key not in _TEMPLATE_CACHE:
        override: dict[str, Any] = {}
        if root:
            path = Path(root) / "prompts" / f"{block}.yaml"
            if path.is_file():
                try:
                    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
                except (OSError, yaml.YAMLError):
                    loaded = None
                if isinstance(loaded, dict):
                    override.update(loaded)
        _TEMPLATE_CACHE[key] = {
            str(name): [str(item) for item in value] if isinstance(value, list) else [str(value)]
            for name, value in override.items()
            if isinstance(value, (list, str)) and value
        }
    definition = scenario.get("scenario", scenario) or {}
    inline = definition.get(block) if isinstance(definition, dict) else None
    merged = {**defaults, **_TEMPLATE_CACHE[key]}
    if isinstance(inline, dict):
        for name, value in inline.items():
            if isinstance(value, list) and value:
                merged[str(name)] = [str(item) for item in value]
            elif isinstance(value, str) and value:
                merged[str(name)] = [value]
    return merged


def _pick(rng: random.Random, options: list[str], **values: Any) -> str:
    template = options[rng.randrange(len(options))] if options else ""
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return template


def cn_number(value: int) -> str:
    """十以内（含）的整数写成汉字，避免在叙事里引入阿拉伯数字。"""
    if 0 <= value <= 10:
        return "十" if value == 10 else _CN_DIGITS[value]
    return "数"


def humanize_fact(text: str) -> str:
    """把引擎事实行改成叙述体：去掉「（前→后）」；「国库 −50（西洱河之败）」→「以西洱河之败，国库减50」。"""
    text = _ARROW.sub("", str(text)).strip().rstrip("。；;")
    match = _DELTA_FACT.match(text)
    if match:
        direction = "增" if match.group("sign") == "+" else "减"
        cause = match.group("cause")
        body = f"{match.group('name')}{direction}{match.group('num')}"
        return f"以{cause}，{body}" if cause else body
    return text


def merge_causes(items: list[str]) -> list[str]:
    """同一原因的叙述合并：「以甲，A」「以甲，B」→「以甲，A，B」（保持首次出现的顺序）。"""
    merged: dict[str, list[str]] = {}
    order: list[str] = []
    for item in items:
        match = re.match(r"^以([^，]+)，(.+)$", item)
        key, body = (match.group(1), match.group(2)) if match else ("", item)
        slot = key or f"#{len(order)}"
        if slot not in merged:
            merged[slot] = []
            order.append(slot)
        merged[slot].append(body)
    return [(f"以{slot}，" if not slot.startswith("#") else "") + "，".join(merged[slot]) for slot in order]


def _join(items: list[str], limit: int, sep: str = "，") -> str:
    shown = [item for item in items if item][:limit]
    text = sep.join(shown)
    return text + ("等" if len([item for item in items if item]) > limit else "")


def _sentence(text: str) -> str:
    text = text.strip().rstrip("，；;,")
    return text if text.endswith(("。", "！", "？")) else text + "。"


# ---- 纪事 ---------------------------------------------------------------------


def offline_chronicle(
    report: TurnReport,
    state: WorldState,
    scenario: dict[str, Any] | None = None,
    seed: int = 0,
    context: dict[str, Any] | None = None,
) -> str:
    """模板史笔：把本季玩家可见的事实按类归并成二至五句（「是季，……。」）。

    report 应是 redacted_report（已去掉内部事实、隐藏数值）；context 为本季诏令来源与抉择
    （{"edicts": [来源说明], "plain_edicts": bool, "choices": [{"title", "option"}]}）。
    """
    tpl = templates(scenario, "chronicle_templates", CHRONICLE_TEMPLATES)
    rng = random.Random(f"chronicle:{seed}:{report.turn}")
    context = context or {}
    facts = [fact for fact in report.facts if not fact.internal]
    by_kind: dict[str, list[Any]] = {}
    for fact in facts:
        by_kind.setdefault(fact.kind, []).append(fact)
    clauses: list[str] = []

    titles = [f"「{fact.text}」" for fact in by_kind.get("event", [])]
    if titles:
        clauses.append(_pick(rng, tpl["events"], items="、".join(titles[:3]) + ("等" if len(titles) > 3 else "")))

    battle_items: list[str] = []
    for battle in report.battles[:2]:
        names = {
            "attacker": battle.get("attacker_name") or _faction(state, battle.get("attacker")),
            "defender": battle.get("defender_name") or _faction(state, battle.get("defender")),
            "region": battle.get("region_name") or _region(state, battle.get("region")),
        }
        if battle.get("result") == "stalemate":
            battle_items.append(_pick(rng, tpl["stalemate"], **names))
            continue
        winner = names["attacker"] if battle.get("result") == "attacker_won" else names["defender"]
        key = "battle_captured" if battle.get("captured") else "battle"
        battle_items.append(_pick(rng, tpl[key], winner=winner, **names))
    if not battle_items:
        battle_items = [humanize_fact(fact.text) for fact in by_kind.get("battle", [])[:2]]
    for fact in by_kind.get("army", []):
        if len(battle_items) >= 3:
            break
        if fact.text.endswith(("战损", "败绩", "溃散")) and _numeric(fact.before) and _numeric(fact.after):
            army = fact.text.removesuffix("战损").removesuffix("败绩").removesuffix("无路可退，溃散")
            battle_items.append(_pick(rng, tpl["loss"], army=army, before=format_number(fact.before), after=format_number(fact.after)))
    if not report.battles:
        battle_items.extend(humanize_fact(fact.text) for fact in by_kind.get("region_controller", [])[:1])
    if battle_items:
        clauses.append("，".join(battle_items))

    edict_parts: list[str] = []
    labels = [str(label) for label in context.get("edicts", []) if label]
    if labels:
        edict_parts.append(_pick(rng, tpl["edicts"], items="、".join(f"「{label}」" for label in labels[:3])))
    if context.get("plain_edicts"):
        edict_parts.append(_pick(rng, tpl["edicts_plain"]))
    for row in list(context.get("choices", []))[:2]:
        edict_parts.append(_pick(rng, tpl["choice"], title=row.get("title", ""), option=row.get("option", "")))
    for fact in by_kind.get("choice_default", [])[:1]:
        edict_parts.append(humanize_fact(fact.text))
    if edict_parts:
        clauses.append("，".join(edict_parts))

    people = [humanize_fact(fact.text) for kind in ("appointment", "status", "army_commander") for fact in by_kind.get(kind, [])]
    if people:
        clauses.append(_pick(rng, tpl["appointments"], items=_join(people, 3)))

    moves = [humanize_fact(fact.text) for kind in ("movement", "arrival", "retreat", "ai_hold") for fact in by_kind.get(kind, [])]
    if moves:
        clauses.append(_pick(rng, tpl["movements"], items=_join(moves, 3)))

    ledger_items = _ledger_highlights(report, state, tpl, rng)
    if ledger_items:
        clauses.append(_pick(rng, tpl["ledger"], items="，".join(ledger_items)))

    others = [
        humanize_fact(fact.text)
        for kind in ("region", "relation", "trait", "army_faction", "character_faction", "project", "project_complete", "standing_order")
        for fact in by_kind.get(kind, [])
    ]
    others.extend(humanize_fact(fact.text) for fact in by_kind.get("army", []) if not fact.text.endswith(("战损", "败绩", "溃散")))
    if report.battles:
        others.extend(humanize_fact(fact.text) for fact in by_kind.get("region_controller", []) if "攻占" not in fact.text)
    others = merge_causes([item for item in others if item and "有所变化" not in item])
    if others:
        clauses.append(_pick(rng, tpl["others"], items=_join(others, 2)))

    outcome = next((fact for fact in by_kind.get("outcome", [])), None)
    limit = 4 if outcome else 5
    clauses = [clause for clause in clauses if clause.strip()][:limit]
    if outcome is not None:
        clauses.append(_pick(rng, tpl["outcome"], title=outcome.text.removeprefix("终局：")))
    if not clauses:
        return _pick(rng, tpl["empty"])
    sentences = [_sentence(clause) for clause in clauses]
    sentences[0] = _pick(rng, tpl["opening"]) + sentences[0]
    if len(sentences) < 2:
        sentences.append(_pick(rng, tpl["closing"]))
    return "".join(sentences)


def _ledger_highlights(report: TurnReport, state: WorldState, tpl: dict[str, list[str]], rng: random.Random) -> list[str]:
    """账目要点：按资源合计本季账目（已去掉隐藏资源），取变动最大的两项；再补一条变动最大的资源事实。"""
    totals: dict[str, float] = {}
    for line in report.ledger_lines:
        if line.resource in state.resources and not state.resources[line.resource].hidden:
            totals[line.resource] = totals.get(line.resource, 0.0) + float(line.amount)
    ranked = sorted(((abs(value), key, value) for key, value in totals.items() if round(value, 1) != 0), reverse=True)
    items = []
    for _, key, value in ranked[:2]:
        resource = state.resources[key]
        items.append(_pick(
            rng,
            tpl["ledger_item"],
            name=resource.name,
            direction="增" if value > 0 else "减",
            amount=format_number(abs(value)),
            unit=resource.unit,
        ))
    resource_facts = [
        fact for fact in report.facts
        if fact.kind == "resource" and not fact.internal and _numeric(fact.before) and _numeric(fact.after)
    ]
    if resource_facts:
        biggest = max(resource_facts, key=lambda fact: abs(float(fact.after) - float(fact.before)))
        items.append(humanize_fact(biggest.text))
    return items


def chronicle_numbers(report: TurnReport) -> set[float]:
    """纪事可以引用的数字：事实文本中的数、数值事实的前后值与差值、账目行金额及按资源的合计（含一位小数的取整）。"""
    allowed: set[float] = set()

    def add(value: float) -> None:
        allowed.add(float(value))
        allowed.add(round(float(value), 1))
        allowed.add(abs(round(float(value), 1)))

    for fact in report.facts:
        for item in _DIGITS.findall(fact.text):
            add(float(item))
        values = [value for value in (fact.before, fact.after) if _numeric(value)]
        for value in values:
            add(float(value))
        if len(values) == 2:
            add(float(values[1]) - float(values[0]))
    totals: dict[str, float] = {}
    for line in report.ledger_lines:
        add(float(line.amount))
        totals[line.resource] = totals.get(line.resource, 0.0) + float(line.amount)
    for value in totals.values():
        add(value)
    return allowed


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _faction(state: WorldState, faction_id: Any) -> str:
    faction = state.factions.get(str(faction_id)) if faction_id else None
    return faction.name if faction else "敌"


def _region(state: WorldState, region_id: Any) -> str:
    region = state.regions.get(str(region_id)) if region_id else None
    return region.name if region else "边地"


# ---- 人物：召对与廷议 -------------------------------------------------------------


def character_card(scenario: dict[str, Any] | None, character_id: str) -> dict[str, Any]:
    """剧本 characters.yaml 中该人物的 card 块（greeting、examples 等；引擎状态不读取）。"""
    for row in (scenario or {}).get("characters", []) or []:
        if isinstance(row, dict) and str(row.get("id")) == character_id:
            card = row.get("card")
            return dict(card) if isinstance(card, dict) else {}
    return {}


def _subsequence(short: str, long: str) -> bool:
    chars = iter(long)
    return all(char in chars for char in short)


def refresh_greeting(greeting: str, state: WorldState, character_id: str, scenario: dict[str, Any] | None = None) -> str:
    """开场白里的旧官衔改为现任官职（「臣常清，安西判官。」→「臣常清，安西四镇节度使。」）；官职未变时原样返回。

    旧衔识别：整句是静态 title 的缩写（字序相同、至少 3 字），或句中含静态 title / 开局所任而今已不任的官职名。
    已无官职时删去该句。
    """
    title, former = current_title(state, character_id, scenario)
    if not former or not greeting:
        return greeting
    from simcore.domain.rules import held_offices

    character = state.characters[character_id]
    held = held_offices(character)
    names = [state.offices[office].name for office in held if office in state.offices]
    stale = [former] + [
        state.offices[office].name for office in initial_offices(scenario, character_id) or [] if office not in held and office in state.offices
    ]
    replacement = names[0] if names else ""
    parts = re.split(r"([，。！？；,.!?;])", greeting)
    result: list[str] = []
    skip_punct = False
    for part in parts:
        if skip_punct and part and re.fullmatch(r"[，。！？；,.!?;]", part):
            skip_punct = False
            continue
        skip_punct = False
        clause = part.strip()
        if len(clause) >= 3 and not re.fullmatch(r"[，。！？；,.!?;]", clause) and any(_subsequence(clause, name) for name in stale if name):
            if replacement:
                result.append(part.replace(clause, replacement))
            else:
                skip_punct = True
            continue
        for name in stale:
            if name and name in part:
                part = part.replace(name, replacement or "")
        result.append(part)
    return "".join(result).strip()


def _earliest(text: str, terms: tuple[str, ...]) -> str | None:
    best: tuple[int, int, str] | None = None
    for term in terms:
        index = text.find(term)
        if index >= 0 and (best is None or (index, -len(term)) < (best[0], best[1])):
            best = (index, -len(term), term)
    return best[2] if best else None


def self_term(speech_style: str, card: dict[str, Any]) -> str:
    """人物的自称：言谈风格里的「自称「X」」，否则开场白 / 示例台词里最先出现的自称，缺省为「臣」。"""
    explicit = re.search(r"自称「([^」]{1,4})」", speech_style or "")
    if explicit:
        return explicit.group(1)
    for source in (str(card.get("greeting") or ""), *[str(item) for item in card.get("examples", []) or []], speech_style or ""):
        term = _earliest(source, SELF_TERMS)
        if term:
            return term
    return "臣"


def address_term(card: dict[str, Any], default: str) -> str:
    """人物对君主的称呼：开场白中最先出现的称呼（陛下、大家、父皇……），缺省用模板的 address。"""
    return _earliest(str(card.get("greeting") or ""), ADDRESS_TERMS) or default


def digest_items(digest: str) -> list[tuple[str, str]]:
    """把 StateDigest 拆成 [(段名, 条目)]：【国势】A；B → (国势, A)、(国势, B)。"""
    items: list[tuple[str, str]] = []
    for line in digest.splitlines():
        match = re.match(r"^【([^】]+)】(.*)$", line.strip())
        if not match:
            continue
        section, body = match.group(1), match.group(2)
        if section in {"时", "本人", "官职", "他处", "待决", "常设令"}:
            continue
        for item in body.split("；"):
            if item.strip():
                items.append((section, item.strip()))
    return items


SECTION_BONUS = {"本季事实": 0.4, "记忆": 0.3, "军队": 0.2, "地区": 0.2, "外域": 0.1, "工程": 0.1, "国势": 0.0}


def pick_facts(
    digest: str,
    topic_names: list[str],
    own_names: list[str],
    limit: int = 2,
    prefer_numbers: bool = False,
    exclude: list[str] | None = None,
) -> list[tuple[str, str]]:
    """从摘要中挑与议题最相关的条目：点到议题实体 / 资源 +3；本季事实与记忆中涉及本人 +1，本人驻地一行 +0.5；
    都不沾边时退回首条本季事实或国势。exclude 中已被前面发言引用过的条目尽量不再重复。"""
    scored: list[tuple[float, int, str, str]] = []
    used = [text for text in exclude or [] if text]
    for order, (section, item) in enumerate(digest_items(digest)):
        score = 0.0
        if any(name and name in item for name in topic_names):
            score += 3
        if any(name and name in item for name in own_names):
            score += 1 if section in {"本季事实", "记忆", "军队"} else 0.5
        if score <= 0:
            continue
        if any(_fact_phrase(section, item) in text for text in used):
            score -= 2.5
        score += SECTION_BONUS.get(section, 0.0)
        if prefer_numbers and _DIGITS.search(item):
            score += 0.5
        scored.append((score, -order, section, item))
    scored.sort(reverse=True)
    picked = [(section, item) for _, _, section, item in scored[:limit]]
    if not picked:
        fallback = [(section, item) for section, item in digest_items(digest) if section in {"本季事实", "国势"}]
        if prefer_numbers:
            fallback.sort(key=lambda row: (row[0] != "国势" or not _DIGITS.search(row[1]), row[0] != "本季事实"))
        picked = fallback[:1]
    return picked


def _fact_phrase(section: str, item: str, own_faction: str = "", keep_numbers: bool = False) -> str:
    """把摘要条目改写成口语：军队、地区、外域不照搬「河东军55000，驻河东，属唐廷，统帅安禄山」这类原始行。

    keep_numbers=True（廷议引数据）时兵数、指标保留摘要里的阿拉伯数字，否则兵数说成「五万余」、指标说成「颇佳 / 尚可」。
    """
    text = humanize_fact(item)
    if section == "军队":
        return _army_phrase(text, own_faction, keep_numbers) or text.replace(" ", "")
    if section == "地区":
        return _region_phrase(text, keep_numbers)
    if section == "外域":
        return _foreign_phrase(text)
    text = re.sub(r" (?=约?\d|约 )", "", text)
    return text.replace("（风闻）", "")


_ARMY_ROW = re.compile(r"^(?P<name>\S+?) (?P<num>\d+(?:\.\d+)?)，驻(?P<region>[^，（]+)(?P<rest>.*)$")
_LEVELS = ((70, "颇佳"), (50, "尚可"), (30, "欠佳"), (-1e9, "堪忧"))


COMMON_SURNAMES = frozenset(
    "李王张刘陈杨赵黄周吴徐孙胡朱高林何郭马罗梁宋郑谢韩唐冯于董萧程曹袁邓许傅沈曾彭吕苏卢蒋蔡贾丁魏薛叶阎余潘杜戴夏钟汪田任姜范方石姚谭廖邹熊金陆郝孔白崔康毛邱秦江史顾侯邵孟龙万段雷钱汤尹黎易常武乔贺赖龚文"
    "安颜封哥裴房狄褚长鱼"
)


def given_name(name: str) -> str:
    """口语里的称呼：常见单姓的三字名去姓（安禄山→禄山），其余（郭将军、哥舒翰之类）用全名。"""
    if len(name) == 3 and name[0] in COMMON_SURNAMES and not name.endswith(("军", "相", "公", "王", "侍", "帅", "度")):
        return name[1:]
    return name


def cn_amount(value: float) -> str:
    """兵数的口语：55000 → 五万余，8000 → 八千，120000 → 十二万。只用汉字，不引入阿拉伯数字。"""
    number = int(value)
    for unit, label in ((10000, "万"), (1000, "千")):
        if number >= unit:
            count, rest = divmod(number, unit)
            return f"{cn_int(count)}{label}{'余' if rest else ''}"
    return "数百" if number >= 100 else "寥寥"


def cn_int(value: int) -> str:
    if value <= 10:
        return cn_number(value)
    if value < 100:
        tens, ones = divmod(value, 10)
        return f"{'' if tens == 1 else cn_number(tens)}十{cn_number(ones) if ones else ''}"
    return "数百"


def _army_phrase(text: str, own_faction: str = "", keep_numbers: bool = False) -> str:
    match = _ARMY_ROW.match(text)
    if not match:
        return ""
    name, region, rest = match.group("name"), match.group("region"), match.group("rest")
    base = f"{name[:-1]}之兵" if name.endswith("军") and len(name) > 2 else name
    amount = match.group("num") if keep_numbers else cn_amount(float(match.group("num")))
    faction = re.search(r"，属([^，（]+)", rest)
    commander = re.search(r"，统帅([^，（]+)", rest)
    transit = re.search(r"，在途（([^）]+)）", rest)
    clauses = [f"{base}{amount}"]
    if transit:
        clauses.append(f"正{transit.group(1)}")
    elif region not in name:
        clauses.append(f"屯于{region}")
    if commander:
        who = commander.group(1)
        if "未到任" in who or "不在军中" in who:
            clauses.append(f"主帅{given_name(who.split('未到任')[0].split('不在军中')[0])}未至，副将暂摄")
        else:
            clauses.append(f"皆在{given_name(who)}麾下")
    else:
        clauses.append("尚无主帅")
    if faction and own_faction and faction.group(1) != own_faction:
        clauses.append(f"隶{faction.group(1)}")
    return "，".join(clauses)


def _region_phrase(text: str, keep_numbers: bool = False) -> str:
    parts = text.split(" ")
    name, stats = parts[0], []
    for part in parts[1:]:
        match = re.match(r"^(\D+?)(-?\d+(?:\.\d+)?)$", part)
        if match:
            stats.append((match.group(1), float(match.group(2))))
    if not stats:
        return name
    notable = sorted(stats, key=lambda row: -abs(row[1] - 50))[:2]
    words = [
        f"{label}{format_number(value) if keep_numbers else next(word for floor, word in _LEVELS if value >= floor)}"
        for label, value in notable
    ]
    return f"{name}{'、'.join(words)}"


def _foreign_phrase(text: str) -> str:
    match = re.match(r"^(?P<region>[^（]+)（(?P<holder>[^）]+)）(?:驻(?P<garrison>.+))?$", text.replace(" ", ""))
    if not match:
        return text.replace(" ", "")
    phrase = f"{match.group('region')}今为{match.group('holder')}所据"
    if match.group("garrison"):
        phrase += f"，{match.group('garrison')}屯驻其地"
    return phrase


def _fact_sentence(
    rng: random.Random,
    tpl: dict[str, list[str]],
    section: str,
    item: str,
    self_name: str,
    first: bool,
    own_faction: str = "",
    keep_numbers: bool = False,
) -> str:
    fact = _fact_phrase(section, item, own_faction, keep_numbers)
    if not first:
        key = "fact_more"
    elif "风闻" in item:
        key = "fact_rumor"
    elif section == "记忆":
        key = "fact_memory"
    elif section == "本季事实":
        key = "fact_news"
    else:
        key = "fact_state"
    return _pick(rng, tpl[key], fact=fact, self=self_name)


def _usable_example(card: dict[str, Any], rng: random.Random) -> str:
    examples = [str(item).strip() for item in card.get("examples", []) or [] if str(item).strip() and not _DIGITS.search(str(item))]
    return examples[rng.randrange(len(examples))] if examples else ""


FISCAL_WORDS = ("国用", "财用", "钱粮", "度支", "岁入", "库藏", "府库", "用度")
FISCAL_CHARS = ("库", "帑", "财")


def topic_names(state: WorldState, text: str, exclude_id: str | None = None) -> list[str]:
    """问话 / 议题点到的显示名：实体（人物、势力、官职、地区、军队）、资源名；问及国用、钱粮时补上库藏类资源。"""
    from simcore.agent.debate import topic_entities

    names = [row["name"] for row in topic_entities(state, text) if row["id"] != exclude_id]
    for resource in state.resources.values():
        if len(resource.name) >= 2 and resource.name in text:
            names.append(resource.name)
        elif any(word in text for word in FISCAL_WORDS) and any(char in resource.name for char in FISCAL_CHARS):
            names.append(resource.name)
        elif "粮" in text and "粮" in resource.name:
            names.append(resource.name)
    return list(dict.fromkeys(names))


def offline_minister_reply(
    character_id: str,
    state: WorldState,
    user_text: str,
    scenario: dict[str, Any] | None,
    seed: int,
    digest: str,
) -> str:
    """离线召对：人设卡（自称、称呼、开场白、示例台词）+ 引擎给定的立场 + 摘要中一两条相关事实。不引入摘要以外的数字。

    只有问话是一项提议（有褒贬用词、点到实体或对应奏疏）时才表态；泛泛之问只陈所知，可附示例台词。
    """
    from simcore.agent.debate import decide_stances, match_memorial, topic_polarity

    character = state.characters[character_id]
    card = character_card(scenario, character_id)
    tpl = templates(scenario, "audience_templates", AUDIENCE_TEMPLATES)
    rng = random.Random(f"audience:{seed}:{state.turn}:{character_id}:{user_text}")
    me = self_term(character.speech_style, card)
    addr = address_term(card, tpl["address"][0] if tpl.get("address") else "陛下")
    names = topic_names(state, user_text, character_id)
    try:
        memorial = match_memorial(scenario or {}, state, user_text) if user_text.strip() else None
    except Exception:  # noqa: BLE001 - 奏疏匹配失败只影响是否表态
        memorial = None
    proposal = bool(user_text.strip()) and (
        topic_polarity(user_text) != 0 or memorial is not None or _names_entities(state, user_text, character_id)
    )
    stance_row = decide_stances(state, scenario or {}, user_text or "召对", [character_id], seed=seed)[0]
    stance = stance_row["stance"] if proposal else "neutral"
    greeting = refresh_greeting(str(card.get("greeting") or "").strip(), state, character_id, scenario)
    generic = len(user_text.strip()) < 6 and not names
    parts: list[str] = []
    if greeting and not _DIGITS.search(greeting) and (generic or rng.random() < 0.35):
        parts.append(_sentence(greeting))
    else:
        parts.append(_pick(rng, tpl["opener"], addr=addr, self=me))
    if proposal:
        if stance_row.get("reason") in {"事关本人权位", "事关本人"}:
            parts.append(_pick(rng, tpl["self_interest"], self=me, addr=addr))
        parts.append(_pick(rng, tpl[stance], self=me, addr=addr))
    own_region = state.regions.get(character.region)
    own = [character.name] + ([own_region.name] if own_region else [])
    picked = pick_facts(digest, names, own, limit=2 if names else 1, prefer_numbers=bool(names))
    picked = picked[:1] + [row for row in picked[1:] if any(name in row[1] for name in names)]
    own_faction = _own_faction_name(state)
    for index, (section, item) in enumerate(picked):
        parts.append(_fact_sentence(rng, tpl, section, item, me, index == 0, own_faction))
    example = _usable_example(card, rng)
    if example and not proposal and rng.random() < 0.6:
        parts.append(_sentence(example))
    parts.append(_fresh_close(rng, tpl[f"close_{stance}"], parts, addr=addr, self=me))
    return "".join(_dedupe_sentences(parts))


def _fresh_close(rng: random.Random, options: list[str], parts: list[str], **values: Any) -> str:
    """结语：避开已说过的句子（开场白 / 示例台词里已有「伏惟陛下圣裁」时换一句，全都说过就不再收尾）。"""
    said = "".join(parts)
    rendered = [_pick(random.Random(0), [option], **values) for option in options]
    fresh = [text for text in rendered if text and text.rstrip("。") not in said]
    if not fresh:
        return ""
    return fresh[rng.randrange(len(fresh))]


def _dedupe_sentences(parts: list[str]) -> list[str]:
    """同一句话不说两遍（按句号切分后去重）。"""
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        pieces = [piece for piece in re.split(r"(?<=[。！？])", part) if piece]
        kept = [piece for piece in pieces if piece.strip() not in seen]
        seen.update(piece.strip() for piece in pieces)
        if kept:
            result.append("".join(kept))
    return result


def _own_faction_name(state: WorldState) -> str:
    faction = state.factions.get(str(state.rules.get("player_faction") or ""))
    return faction.name if faction else ""


def _names_entities(state: WorldState, text: str, exclude_id: str) -> bool:
    from simcore.agent.debate import topic_entities

    return any(row["id"] != exclude_id for row in topic_entities(state, text))


def offline_debate_line(
    character_id: str,
    stance_row: dict[str, Any],
    topic: str,
    state: WorldState,
    scenario: dict[str, Any] | None,
    seed: int,
    digest: str,
    previous: list[dict[str, Any]] | None = None,
) -> str:
    """离线廷议台词：按引擎给定立场开口，回应上一位发言人，引摘要中一条与议题相关、前人未引过的事实（优先带数字），
    可附示例台词。"""
    character = state.characters[character_id]
    card = character_card(scenario, character_id)
    tpl = templates(scenario, "audience_templates", AUDIENCE_TEMPLATES)
    rng = random.Random(f"debate-line:{seed}:{state.turn}:{topic}:{character_id}")
    me = self_term(character.speech_style, card)
    addr = address_term(card, tpl["address"][0] if tpl.get("address") else "陛下")
    stance = str(stance_row.get("stance", "neutral"))
    if stance not in {"support", "oppose", "neutral"}:
        stance = "neutral"
    parts: list[str] = []
    last = (previous or [])[-1] if previous else None
    opener = True
    if last is not None and last.get("name") and last.get("name") != character.name:
        if last.get("stance") == stance:
            parts.append(_pick(rng, tpl["agree_previous"], prev=last["name"], self=me, addr=addr))
        else:
            parts.append(_pick(rng, tpl["differ_previous"], prev=last["name"], self=me, addr=addr))
            opener = stance != "oppose"  # 驳前议即已表明反对，不再重复
    if stance_row.get("reason") in {"事关本人权位", "事关本人"}:
        parts.append(_pick(rng, tpl["self_interest"], self=me, addr=addr))
    if opener:
        parts.append(_pick(rng, tpl[f"debate_{stance}"], self=me, addr=addr))
    own_region = state.regions.get(character.region)
    own = [character.name] + ([own_region.name] if own_region else [])
    cited = [str(row.get("text", "")) for row in previous or []]
    for section, item in pick_facts(digest, topic_names(state, topic, character_id), own, limit=1, prefer_numbers=True, exclude=cited):
        parts.append(_fact_sentence(rng, tpl, section, item, me, True, _own_faction_name(state), keep_numbers=True))
    example = _usable_example(card, rng)
    if example and stance == "neutral" and rng.random() < 0.5:
        parts.append(_sentence(example))
    else:
        parts.append(_fresh_close(rng, tpl[f"close_{stance}"], parts, addr=addr, self=me))
    return "".join(_dedupe_sentences(parts))
