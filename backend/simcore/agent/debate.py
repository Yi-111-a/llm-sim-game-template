"""廷议：立场由引擎根据议题决定——议题对应当季奏疏时用奏疏的谏言立场，否则按议题点名的实体
（人物、势力、官职、地区、军队）与发言人的亲疏、派系利益与忠诚推定，平局用种子决断；
台词每人一次 LLM 调用，只带该发言人的人设。"""

from __future__ import annotations

import random
import re
from typing import Any

from simcore.agent.offline import character_card, current_title, is_offline, offline_debate_line, refresh_greeting
from simcore.domain.digest import digest_numbers, render_digest
from simcore.domain.events import relation_value
from simcore.domain.memorials import MemorialError, is_visible, memorial_item
from simcore.domain.models import WorldState
from simcore.domain.rules import held_offices
from simcore.llm.base import LLMProvider
from simcore.prompts.loader import render_prompt

STANCE_LABELS = {"support": "赞成", "oppose": "反对", "neutral": "持中"}
NEGATIVE_WORDS = ("罢", "免", "削", "夺", "贬", "诛", "裁", "撤", "禁", "驳", "拒", "问罪", "追究", "弹劾", "抑", "分其", "收其", "停", "减")
POSITIVE_WORDS = ("授", "加", "赏", "擢", "兼", "封", "赐", "准", "升", "拜", "厚", "许", "增", "予")
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])-?\d+(?:\.\d+)?")


def topic_polarity(topic: str) -> int:
    negative = sum(topic.count(word) for word in NEGATIVE_WORDS)
    positive = sum(topic.count(word) for word in POSITIVE_WORDS)
    if negative > positive:
        return -1
    if positive > negative:
        return 1
    return 0


ENTITY_KIND_LABELS = {"character": "人物", "faction": "势力", "office": "官职", "region": "地区", "army": "军队"}
AFFINITY_THRESHOLD = 20.0
SELF_INTEREST = 100.0
MEMORIAL_MATCH_THRESHOLD = 0.5


def topic_entities(state: WorldState, topic: str) -> list[dict[str, Any]]:
    """议题中按显示名点到的实体：[{kind, id, name}]，按在议题中首次出现的位置排序（单字名不计）。"""
    found: list[tuple[int, int, dict[str, Any]]] = []
    tables: list[tuple[str, dict[str, Any]]] = [
        ("character", state.characters),
        ("faction", state.factions),
        ("office", state.offices),
        ("region", state.regions),
        ("army", state.armies),
    ]
    for order, (kind, table) in enumerate(tables):
        for row in table.values():
            name = str(getattr(row, "name", ""))
            if len(name) >= 2 and name in topic:
                found.append((topic.index(name), order, {"kind": kind, "id": row.id, "name": name}))
    found.sort(key=lambda item: (item[0], item[1], item[2]["id"]))
    return [item[2] for item in found]


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"[\s，。、；：！？「」『』（）()\-—·,.!?:;]", "", text)
    return {cleaned[index:index + 2] for index in range(len(cleaned) - 1)}


def memorial_match_score(item: dict[str, Any], topic: str) -> float:
    """议题与奏疏的相似度：标题互相包含或关键词命中为 1，否则为标题二字组被议题覆盖的比例。"""
    title = str(item.get("title", ""))
    if title and (title in topic or topic in title):
        return 1.0
    if any(str(keyword) and str(keyword) in topic for keyword in item.get("keywords", []) or []):
        return 1.0
    grams = _bigrams(title)
    return len(grams & _bigrams(topic)) / len(grams) if grams else 0.0


def match_memorial(
    scenario: dict[str, Any], state: WorldState, topic: str, memorial_id: str | None = None
) -> dict[str, Any] | None:
    """议题对应的奏疏：显式 memorial_id（须存在），否则在当季可见的奏疏里按标题 / keywords 模糊匹配。"""
    if memorial_id:
        item = memorial_item(scenario, memorial_id)
        if item is None:
            raise MemorialError("UNKNOWN_MEMORIAL", f"奏疏不存在：{memorial_id}")
        return item
    best: tuple[float, dict[str, Any]] | None = None
    for item in scenario.get("memorials") or []:
        if not isinstance(item, dict) or not is_visible(item, state):
            continue
        score = memorial_match_score(item, topic)
        if score >= MEMORIAL_MATCH_THRESHOLD and (best is None or score > best[0]):
            best = (score, item)
    return best[1] if best else None


def decide_stances(
    state: WorldState,
    scenario: dict[str, Any],
    topic: str,
    participants: list[str],
    *,
    memorial_id: str | None = None,
    seed: int = 0,
    proposal_option: str | None = None,
) -> list[dict[str, Any]]:
    """确定性地给出每位参与者的立场与理由。

    优先级：议题对应奏疏的谏言立场 → 场景 debate_topics 的人物/派系立场表
    → 议题点名实体的利害（本人权位、亲疏、本方势力、派系利益 bloc_interests）→ 忠诚 → 种子决断。
    """
    definition = scenario.get("scenario", scenario) or {}
    memorial = match_memorial(scenario, state, topic, memorial_id)
    advisors = {
        str(row.get("character")): row
        for row in (memorial or {}).get("advisors", []) or []
        if isinstance(row, dict)
    }
    topics = definition.get("debate_topics") or []
    matched = next(
        (
            row for row in topics
            if isinstance(row, dict)
            and (row.get("id") == topic or any(str(keyword) in topic for keyword in row.get("keywords", []) or []))
        ),
        None,
    )
    entities = topic_entities(state, topic)
    polarity = topic_polarity(topic)
    interests = definition.get("bloc_interests") or (definition.get("settings") or {}).get("bloc_interests") or {}
    if not isinstance(interests, dict) or not interests:
        interests = state.rules.get("bloc_interests") or {}
    result: list[dict[str, Any]] = []
    for character_id in participants:
        character = state.characters[character_id]
        stance: str | None = None
        reason = ""
        if character_id in advisors:
            stance = str(advisors[character_id].get("stance", "neutral"))
            reason = f"已就「{memorial.get('title', '') if memorial else ''}」上言"
        if stance is None and matched is not None:
            table = matched.get("stances", {}) or {}
            if character_id in table:
                stance, reason = str(table[character_id]), "素有成见"
            elif character.bloc and character.bloc in table:
                stance, reason = str(table[character.bloc]), f"{bloc_label(state, character.bloc)}的共同立场"
        if stance is None and entities:
            stance, reason = _entity_stance(state, character_id, topic, entities, polarity, interests)
        if stance is None:
            loyalty = float(character.traits.get("loyalty", 5))
            if loyalty >= 8:
                stance, reason = "support", "忠谨，唯上意是从"
            elif loyalty <= 3:
                stance, reason = "oppose", "素怀异见"
            else:
                stance, reason = _tie_break(seed, state.turn, topic, character_id)
        if stance not in STANCE_LABELS:
            stance = "neutral"
        result.append({
            "character": character_id,
            "name": character.name,
            "bloc": character.bloc,
            "stance": stance,
            "stance_label": STANCE_LABELS[stance],
            "reason": reason,
        })
    if memorial is not None and [row for row in memorial.get("options", []) or [] if isinstance(row, dict)]:
        proposal = assign_option_targets(state, memorial, result, interests, seed, proposal_option)
        for row in result:
            row.update(proposal)
    else:
        for row in result:
            row.update({
                "target_option_id": None,
                "target_option_label": None,
                "proposition": topic,
                "position": f"{row['stance_label']}「{topic}」",
            })
    return result


OPTION_LINE_WEIGHT = 15.0
OPTION_AFFINITY_WEIGHT = 0.5
PROPOSAL_MIN_HITS = 2
_NEGATIVE_SELF = ("不用", "不得", "不许", "罢", "免", "贬", "削", "夺", "诛", "下狱", "流", "拒", "驳", "斥")
_SEGMENT = re.compile(r"[\s，。、；：！？「」『』（）()\-—·,.!?:;~～+−\d]+")
# 不指向具体选项的泛泛理由（忠诚 / 种子决断）：议无定案时不能说成「附议所议之案」
GENERIC_REASONS = frozenset({"忠谨，唯上意是从", "素怀异见", "持两端，未肯表态", "揣度上意，姑且附议", "以为未可轻动"})


def _option_text(option: dict[str, Any]) -> str:
    memory = option.get("memory")
    rows = memory if isinstance(memory, list) else [memory] if isinstance(memory, dict) else []
    notes = "；".join(str(row.get("text") or "") for row in rows if isinstance(row, dict))
    return f"{option.get('label', option.get('id', ''))}；{option.get('hint', '')}；{notes}"


def _tokens(text: str) -> set[str]:
    """按标点 / 数字分句后的二字组（不跨句，免得「考课，不另究」拼出「课不」）。"""
    grams: set[str] = set()
    for part in _SEGMENT.split(text):
        grams.update(part[index:index + 2] for index in range(len(part) - 1))
    return grams


def _option_tokens(option: dict[str, Any]) -> set[str]:
    reasons = "；".join(str(op.get("reason") or "") for op in option.get("ops", []) or [] if isinstance(op, dict))
    return _tokens(f"{_option_text(option)}；{reasons}")


def distinctive_tokens(memorial: dict[str, Any], options: list[dict[str, Any]]) -> list[set[str]]:
    """每个选项独有的二字组：去掉别的选项也有的、奏疏标题里的（「考课」「范阳」这类各选项共有的词不算证据）。"""
    every = [_option_tokens(option) for option in options]
    title = _tokens(str(memorial.get("title", "")))
    result = []
    for index, tokens in enumerate(every):
        others: set[str] = set()
        for position, row in enumerate(every):
            if position != index:
                others |= row
        result.append(tokens - others - title)
    return result


def _option_index(options: list[dict[str, Any]], option_id: Any) -> int | None:
    if not option_id:
        return None
    return next((index for index, row in enumerate(options) if str(row.get("id")) == str(option_id)), None)


def _unique_best(scores: list[float], minimum: float) -> int | None:
    if not scores:
        return None
    best = max(range(len(scores)), key=lambda index: scores[index])
    if scores[best] < minimum or sum(1 for value in scores if value == scores[best]) > 1:
        return None
    return best


def infer_proposal(
    state: WorldState, memorial: dict[str, Any], options: list[dict[str, Any]], explicit: str | None = None
) -> tuple[int | None, str]:
    """「所议之案」（谏言立场 support / oppose 所针对的那个选项）及其来历；定不出时为 (None, "")。

    1. 调用方指定（court_debate 的 option_id）；2. 奏疏写明 `proposal: <option_id>`；
    3. 在世赞成者写明的 `option`（唯一时）；4. 奏疏摘要与在世赞成者谏言里、各选项独有的二字组命中 ≥ 2 且唯一最多者；
    5. 选项恰为准驳二议（只有一个选项标题首句不带驳斥 / 罢免之意，其余皆是）时取那一个。其余（多个并列方案）一律无定案，不默认取第一个选项。
    """
    index = _option_index(options, explicit)
    if index is not None:
        return index, "议题指定"
    index = _option_index(options, memorial.get("proposal"))
    if index is not None:
        return index, "奏疏所请"
    present = [
        row for row in memorial.get("advisors", []) or []
        if isinstance(row, dict) and str(row.get("character")) in state.characters and state.characters[str(row.get("character"))].status == "alive"
    ]
    picks = {
        found
        for row in present
        if row.get("stance") == "support" and (found := _option_index(options, row.get("option"))) is not None
    }
    if len(picks) == 1:
        return next(iter(picks)), "赞成者所上之案"
    unique = distinctive_tokens(memorial, options)
    evidence = _tokens(str(memorial.get("summary", "")))
    for row in present:
        if row.get("stance") == "support":
            evidence |= _tokens(str(row.get("line", "")))
    hits = [float(len(tokens & evidence)) for tokens in unique]
    index = _unique_best(hits, PROPOSAL_MIN_HITS)
    if index is not None:
        return index, "奏疏所请"
    polarity = [_lead_polarity(str(option.get("label", ""))) for option in options]
    accept = [index for index, value in enumerate(polarity) if value >= 0]
    if len(options) >= 2 and len(accept) == 1:
        return accept[0], "准驳二议，以准为案"
    return None, ""


def _self_effect(state: WorldState, character_id: str, option: dict[str, Any]) -> int:
    """选项对本人的利害：+1 有利（任命、统兵、被选中、恩宠上升）、−1 不利（免职、处置、恩宠下降、记忆里写着不用/被驳）、0 无涉。"""
    score = 0
    for op in option.get("ops", []) or []:
        if not isinstance(op, dict):
            continue
        name = op.get("op")
        if name in {"appoint", "set_army_commander"} and op.get("character") == character_id:
            score += 1
        elif name in {"dismiss"} and op.get("character") == character_id:
            score -= 1
        elif name == "set_character_status" and op.get("character") == character_id and op.get("status") != "alive":
            score -= 1
        elif name == "set_flag" and op.get("value") == character_id:
            score += 1
        elif name == "adjust_trait" and op.get("character") == character_id and op.get("trait") in {"favor", "loyalty"}:
            score += 1 if float(op.get("delta", 0) or 0) > 0 else -1
        elif name == "set_army_commander" and op.get("character") != character_id:
            army = state.armies.get(str(op.get("army")))
            if army is not None and army.commander == character_id:
                score -= 1  # 夺其兵权
    memory = option.get("memory")
    for row in memory if isinstance(memory, list) else [memory] if isinstance(memory, dict) else []:
        if isinstance(row, dict) and row.get("character") == character_id:
            score += -1 if any(word in str(row.get("text", "")) for word in _NEGATIVE_SELF) else 1
    character = state.characters[character_id]
    label = str(option.get("label", ""))
    if character.name in label or any(state.offices[office].name in label for office in held_offices(character) if office in state.offices):
        score += 1 if topic_polarity(label) >= 0 else -1
    return (score > 0) - (score < 0)


def _option_score(
    state: WorldState, character_id: str, option: dict[str, Any], interests: Any, line_hits: int = 0
) -> tuple[float, str]:
    """某人对某选项的好恶：本人利害 > 亲疏（选项点名的人物、官职在任者、军队统帅）> 派系利益 > 赞成者谏言与该选项独有措辞的相合。

    谏言措辞只对赞成者计分（反对者、持中者的话常常在复述所反对的方案，计入会把立场倒过来）。
    """
    character = state.characters[character_id]
    text = _option_text(option)
    label = str(option.get("label", option.get("id", "")))
    direction = topic_polarity(label) or 1
    parts: list[tuple[float, str]] = []
    own = _self_effect(state, character_id, option)
    if own:
        parts.append((SELF_INTEREST * own, "事关本人" + ("之进退" if own > 0 else "，不利于己")))
    mentioned: dict[str, dict[str, Any]] = {str(row["id"]): row for row in topic_entities(state, text)}
    for op in option.get("ops", []) or []:
        if isinstance(op, dict):
            for key in ("character", "a", "b"):
                if op.get(key) in state.characters:
                    mentioned.setdefault(str(op[key]), {"kind": "character", "id": op[key], "name": state.characters[str(op[key])].name})
            if op.get("op") == "set_flag" and op.get("value") in state.characters:
                picked = str(op["value"])
                mentioned.setdefault(picked, {"kind": "character", "id": picked, "name": state.characters[picked].name})
    for entity in mentioned.values():
        if entity["kind"] != "character" or entity["id"] == character_id:
            continue
        value = _affinity(state, character_id, str(entity["id"])) * OPTION_AFFINITY_WEIGHT * direction
        if abs(value) >= 1:
            parts.append((value, f"与{entity['name']}同气连枝" if value > 0 else f"与{entity['name']}素有嫌隙"))
    if character.bloc and isinstance(interests, dict) and isinstance(interests.get(character.bloc), dict):
        for key, weight in interests[character.bloc].items():
            hit = str(key) in mentioned or (len(str(key)) >= 2 and str(key) in text)
            if hit and isinstance(weight, (int, float)) and not isinstance(weight, bool):
                parts.append((float(weight) * direction, f"{bloc_label(state, character.bloc)}的利益所在"))
    if line_hits:
        parts.append((OPTION_LINE_WEIGHT * line_hits, "与所上之言相合"))
    total = sum(value for value, _ in parts)
    reason = max(parts, key=lambda item: abs(item[0]))[1] if parts else ""
    return total, reason


def _lead_polarity(label: str) -> int:
    """选项标题首句的褒贬（「驳回，另择…并厚赐」取「驳回」）：+1 准许 / 授予类，−1 驳斥 / 罢免类，0 不明。"""
    head = re.split(r"[，,；;。]", label, maxsplit=1)[0]
    return topic_polarity(head)


def assign_option_targets(
    state: WorldState,
    memorial: dict[str, Any],
    rows: list[dict[str, Any]],
    interests: Any,
    seed: int,
    proposal_option: str | None = None,
) -> dict[str, Any]:
    """廷议对应奏疏时，给每个立场定出所主张的选项：target_option_id / target_option_label（+ against_option_*）。

    1. 「所议之案」见 infer_proposal；定不出时**不**默认取第一个选项，而是「诸案并陈」。
    2. 奏疏谏言人写明 `option` 时即主张该选项；否则赞成者主张所议之案（或同一方中得分最高者）、反对者主张另一方得分最高者
       （against = 所议之案）、持中者取得分最高者（全无利害时以种子择一）。
    3. 诸案并陈时：各人按利害打分（赞成者另计其谏言与各选项独有措辞的相合）取最高者；全无利害者不主一议（target 为 None）。
       非谏言人的泛泛立场（忠诚、种子）此时改为持中「议无定案，候上裁决」，不再说「唯上意是从」。
    返回 {proposal_id, proposal_label, proposal_basis}（无定案时 proposal_id 为 None）。
    """
    options = [row for row in memorial.get("options", []) or [] if isinstance(row, dict)]
    advisors = {str(row.get("character")): row for row in memorial.get("advisors", []) or [] if isinstance(row, dict)}
    title = str(memorial.get("title", memorial.get("id", "")))
    polarity = [_lead_polarity(str(option.get("label", ""))) for option in options]
    unique = distinctive_tokens(memorial, options)
    proposal, basis = infer_proposal(state, memorial, options, proposal_option)

    def scores_for(character_id: str) -> list[tuple[float, str]]:
        # 赞成者的谏言措辞计入各选项；反对者的话或复述所驳之案（「恐骤加黜陟」）、或道出所主之案（「减其半亦足」），
        # 故只计入所议之案以外的选项；持中者与诸案并陈时的反对者不计措辞。
        advisor = advisors.get(character_id, {})
        stance = advisor.get("stance")
        line = _tokens(str(advisor.get("line", ""))) if stance == "support" or (stance == "oppose" and proposal is not None) else set()
        hits = [0 if stance == "oppose" and index == proposal else len(unique[index] & line) for index in range(len(options))]
        return [_option_score(state, character_id, option, interests, hits[index]) for index, option in enumerate(options)]

    def roll(character_id: str, candidates: list[int]) -> int:
        return candidates[random.Random(f"debate-option:{seed}:{state.turn}:{memorial.get('id')}:{character_id}").randrange(len(candidates))]

    same_side: list[int] = []
    other_side: list[int] = []
    if proposal is not None:
        same_side = [index for index in range(len(options)) if index == proposal or (polarity[proposal] != 0 and polarity[index] == polarity[proposal])]
        other_side = [index for index in range(len(options)) if index not in same_side] or [index for index in range(len(options)) if index != proposal]
    for row in rows:
        character_id = row["character"]
        scores = scores_for(character_id)
        stance = row["stance"]
        against: int | None = None
        pick: int | None
        explicit = _option_index(options, advisors[character_id].get("option")) if character_id in advisors else None
        if explicit is not None:
            pick, why = explicit, "所上之言即主此案"
            if stance == "oppose" and proposal is not None and proposal != explicit:
                against = proposal
        elif proposal is not None and stance == "support":
            pick = max(same_side, key=lambda index: (scores[index][0], index == proposal, -index))
            why = scores[pick][1] or ("附议所议之案" if pick == proposal else "同为一方之议")
        elif proposal is not None and stance == "oppose":
            against = proposal
            if all(abs(scores[index][0]) < 1e-9 for index in other_side):
                pick, why = roll(character_id, other_side), "力驳所议之案"
            else:
                pick = max(other_side, key=lambda index: (scores[index][0], -index))
                why = scores[pick][1]
        else:
            best = max(range(len(options)), key=lambda index: (scores[index][0], -index))
            if scores[best][0] > 1e-9:
                pick, why = best, scores[best][1]
            elif proposal is not None:
                pick, why = roll(character_id, list(range(len(options)))), "无所偏倚，姑从此议"
            else:
                pick, why = None, ""
            if proposal is None and character_id not in advisors and row.get("reason") in GENERIC_REASONS:
                row.update({"stance": "neutral", "stance_label": STANCE_LABELS["neutral"], "reason": "议无定案，候上裁决"})
        row["proposition"] = f"就「{title}」诸选项择一"
        if pick is None:
            row.update({"target_option_id": None, "target_option_label": None, "target_reason": "", "position": "未主一议，候圣裁"})
            continue
        option = options[pick]
        label = str(option.get("label", option.get("id")))
        row["target_option_id"] = option.get("id")
        row["target_option_label"] = label
        row["target_reason"] = why
        position = f"主张「{label}」（{option.get('id')}）"
        if against is not None:
            row["against_option_id"] = options[against].get("id")
            row["against_option_label"] = str(options[against].get("label", options[against].get("id")))
            position += f"，反对「{row['against_option_label']}」（{row['against_option_id']}）"
        row["position"] = position
    if proposal is None:
        return {"proposal_id": None, "proposal_label": None, "proposal_basis": ""}
    return {
        "proposal_id": options[proposal].get("id"),
        "proposal_label": str(options[proposal].get("label", options[proposal].get("id"))),
        "proposal_basis": basis,
    }


def bloc_label(state: WorldState, bloc: str) -> str:
    """派系的显示名：rules.bloc_names（场景 settings.bloc_names）里的名字 + 「一系」；未配置时不暴露内部 id。"""
    names = state.rules.get("bloc_names") or {}
    name = names.get(bloc) if isinstance(names, dict) else None
    return f"{name}一系" if name else "本派"


def _affinity(state: WorldState, character_id: str, other_id: str) -> float:
    """character 对另一人物的亲疏：关系值，同派系 +50。"""
    character, other = state.characters[character_id], state.characters.get(other_id)
    value = relation_value(state, character_id, other_id)
    if other is not None and character.bloc and character.bloc == other.bloc:
        value += 50
    return value


def _faction_affinity(state: WorldState, character_id: str, faction_id: str | None) -> float:
    character = state.characters[character_id]
    if faction_id is None:
        return 0.0
    if character.faction == faction_id:
        return 40.0
    value = float(character.traits.get(f"relation:{faction_id}", 0))
    if character.faction:
        value += relation_value(state, character.faction, faction_id)
    return value


def _entity_stance(
    state: WorldState,
    character_id: str,
    topic: str,
    entities: list[dict[str, Any]],
    polarity: int,
    interests: Any,
) -> tuple[str | None, str]:
    """按议题点名的实体给出立场：本人权位 > 最强的亲疏 / 派系利益。议题无褒贬时视为「为其请命」（+1）。"""
    character = state.characters[character_id]
    direction = polarity or 1
    if polarity != 0 and _mentions_self(state, character_id, topic):
        return ("support" if polarity > 0 else "oppose"), "事关本人权位"
    scored: list[tuple[float, str]] = []
    for entity in entities:
        kind, entity_id, name = entity["kind"], entity["id"], entity["name"]
        if kind == "character":
            if entity_id == character_id:
                scored.append((SELF_INTEREST, "事关本人"))
                continue
            value = _affinity(state, character_id, entity_id)
            scored.append((value, f"与{name}同气连枝" if value > 0 else f"与{name}素有嫌隙"))
        elif kind == "office":
            office = state.offices[entity_id]
            if entity_id in held_offices(character):
                scored.append((SELF_INTEREST, "事关本人权位"))
            elif office.holder and office.holder in state.characters:
                holder = state.characters[office.holder]
                value = _affinity(state, character_id, holder.id)
                scored.append((value, f"与{name}{holder.name}同气连枝" if value > 0 else f"与{name}{holder.name}素有嫌隙"))
        elif kind == "army":
            army = state.armies[entity_id]
            if army.commander == character_id:
                scored.append((SELF_INTEREST, f"{name}为本人所统"))
            elif army.commander and army.commander in state.characters:
                value = _affinity(state, character_id, army.commander)
                commander = state.characters[army.commander].name
                scored.append((value, f"与{name}统帅{commander}同气连枝" if value > 0 else f"与{name}统帅{commander}素有嫌隙"))
            else:
                value = _faction_affinity(state, character_id, army.faction)
                scored.append((value, f"{name}关乎本方" if value > 0 else f"视{name}为敌"))
        elif kind == "faction":
            value = _faction_affinity(state, character_id, entity_id)
            scored.append((value, f"心向{name}" if value > 0 else f"与{name}为敌"))
        elif kind == "region":
            region = state.regions[entity_id]
            if character.region == entity_id or any(
                army.commander == character_id and army.region == entity_id for army in state.armies.values()
            ):
                scored.append((60.0, f"{name}是本人驻地"))
            elif region.controller and region.controller != (character.faction or state.rules.get("player_faction")):
                value = _faction_affinity(state, character_id, region.controller)
                scored.append((value, f"{name}关乎本方" if value > 0 else f"{name}为敌所据"))
    if character.bloc and isinstance(interests, dict) and isinstance(interests.get(character.bloc), dict):
        for key, weight in interests[character.bloc].items():
            hit = any(entity["id"] == key for entity in entities) or (len(str(key)) >= 2 and str(key) in topic)
            if hit and isinstance(weight, (int, float)) and not isinstance(weight, bool):
                scored.append((float(weight), f"{bloc_label(state, character.bloc)}的利益所在"))
    total = sum(value for value, _ in scored)
    if abs(total) < AFFINITY_THRESHOLD:
        return None, ""
    reason = max(scored, key=lambda item: (abs(item[0]) if (item[0] > 0) == (total > 0) else -1.0))[1]
    return ("support" if total * direction > 0 else "oppose"), reason


def _tie_break(seed: int, turn: int, topic: str, character_id: str) -> tuple[str, str]:
    """无明确利害时用种子决断（同一局、同一季、同一议题结果稳定）：多半持中。"""
    roll = random.Random(f"debate:{seed}:{turn}:{topic}:{character_id}").random()
    if roll < 0.6:
        return "neutral", "持两端，未肯表态"
    if roll < 0.8:
        return "support", "揣度上意，姑且附议"
    return "oppose", "以为未可轻动"


def _mentions_self(state: WorldState, character_id: str, topic: str) -> bool:
    character = state.characters[character_id]
    if character.name in topic:
        return True
    for office_id in held_offices(character):
        office = state.offices.get(office_id)
        if office and office.name in topic:
            return True
    return any(army.commander == character_id and army.name in topic for army in state.armies.values())


TARGET_KEYS = ("target_option_id", "target_option_label", "against_option_id", "against_option_label", "proposition", "position")


def debate_summary(stances: list[dict[str, Any]]) -> str:
    groups: dict[str, list[str]] = {"support": [], "oppose": [], "neutral": []}
    for row in stances:
        groups[row["stance"]].append(row["name"])
    parts = [
        f"{STANCE_LABELS[key]} {len(names)} 人" + (f"（{'、'.join(names)}）" if names else "")
        for key, names in groups.items()
    ]
    targets: dict[str, list[str]] = {}
    for row in stances:
        if row.get("target_option_id"):
            targets.setdefault(f"「{row['target_option_label']}」", []).append(row["name"])
    tally = "；主张：" + "；".join(f"{label} {len(names)} 人（{'、'.join(names)}）" for label, names in targets.items()) if targets else ""
    return "；".join(parts) + tally + "。廷议结论只作谏言，须经诏书方能施行。"


def persona_card(state: WorldState, character_id: str, scenario: dict[str, Any] | None = None) -> dict[str, Any]:
    """人设卡；给了 scenario 时附剧本 card 中的开场白与示例台词。"""
    character = state.characters[character_id]
    card = character_card(scenario, character_id) if scenario else {}
    extra: dict[str, Any] = {}
    title, former = current_title(state, character_id, scenario)
    if former:
        extra["former_title"] = former
    if card.get("greeting"):
        extra["greeting"] = refresh_greeting(str(card["greeting"]), state, character_id, scenario)
    if card.get("examples"):
        extra["examples"] = [str(item) for item in card["examples"]]
    return {
        **extra,
        "character": character.id,
        "name": character.name,
        "title": title,
        "offices": [state.offices[office].name for office in held_offices(character) if office in state.offices],
        "bloc": character.bloc,
        "status": character.status,
        "persona": character.persona,
        "speech_style": character.speech_style,
        "knowledge": list(character.knowledge),
    }


async def debate_speeches(
    state: WorldState,
    scenario: dict[str, Any],
    topic: str,
    stances: list[dict[str, Any]],
    provider: LLMProvider,
    seed: int,
    report: Any = None,
) -> list[dict[str, Any]]:
    speeches: list[dict[str, Any]] = []
    previous: list[str] = []
    offline = is_offline(provider)
    for row in stances:
        character = state.characters[row["character"]]
        digest = render_digest(state, seed, character.id, report, scenario)
        allowed = digest_numbers(digest) | {float(item) for item in _NUMBER.findall(topic)}
        target = {key: row[key] for key in TARGET_KEYS if key in row}
        if offline:
            text = offline_debate_line(character.id, row, topic, state, scenario, seed, digest, speeches)
            if any(float(item) not in allowed for item in _NUMBER.findall(text)):
                text = template_speech(character.name, row["stance"], digest)
            speeches.append({"character": character.id, "name": character.name, "stance": row["stance"], "reason": row["reason"], "text": text, **target})
            previous.append(f"{character.name}（{row['stance_label']}）：{text}")
            continue
        prompt = render_prompt(
            "court_debate",
            {
                "name": character.name,
                "persona": character.persona,
                "speech_style": character.speech_style,
                "stance": row["stance"],
                "stance_label": row["stance_label"],
                "reason": f"{row['reason']}；{row['position']}" if row.get("position") else row["reason"],
                "position": row.get("position", ""),
                "topic": topic,
                "digest": digest,
                "previous": previous,
            },
            scenario.get("root"),
        )
        result = await provider.chat([
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"议题：{topic}"},
        ])
        text = result.content.strip()
        if not text or any(float(item) not in allowed for item in _NUMBER.findall(text)):
            text = template_speech(character.name, row["stance"], digest)
        speeches.append({"character": character.id, "name": character.name, "stance": row["stance"], "reason": row["reason"], "text": text, **target})
        previous.append(f"{character.name}（{row['stance_label']}）：{text}")
    return speeches


def template_speech(name: str, stance: str, digest: str) -> str:
    """不依赖模型的模板台词：只引用摘要中的第一条国势。"""
    opener = {"support": "臣以为此议可行", "oppose": "臣以为此议不可", "neutral": "臣以为此事宜再详议"}[stance]
    national = next((line for line in digest.splitlines() if line.startswith("【国势】")), "")
    first = national.removeprefix("【国势】").split("；")[0] if national else ""
    cite = f"今{first}，不可不察。" if first else ""
    return f"{opener}。{cite}"
