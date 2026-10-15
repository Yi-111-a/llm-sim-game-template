"""廷议：立场由引擎根据 bloc、忠诚与利益决定；台词每人一次 LLM 调用，只带该发言人的人设。"""

from __future__ import annotations

import re
from typing import Any

from simcore.domain.digest import digest_numbers, render_digest
from simcore.domain.events import relation_value
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


def decide_stances(
    state: WorldState, scenario: dict[str, Any], topic: str, participants: list[str]
) -> list[dict[str, Any]]:
    """确定性地给出每位参与者的立场与理由。

    优先级：场景 debate_topics 的人物/派系立场表 → 利益（议题牵涉本人或其亲疏之人）→ 忠诚。
    """
    definition = scenario.get("scenario", scenario) or {}
    topics = definition.get("debate_topics") or []
    matched = next(
        (
            row for row in topics
            if isinstance(row, dict)
            and (row.get("id") == topic or any(str(keyword) in topic for keyword in row.get("keywords", []) or []))
        ),
        None,
    )
    polarity = topic_polarity(topic)
    result: list[dict[str, Any]] = []
    for character_id in participants:
        character = state.characters[character_id]
        stance: str | None = None
        reason = ""
        if matched is not None:
            table = matched.get("stances", {}) or {}
            if character_id in table:
                stance, reason = str(table[character_id]), "素有成见"
            elif character.bloc and character.bloc in table:
                stance, reason = str(table[character.bloc]), f"{character.bloc}一系的共同立场"
        if stance is None and polarity != 0:
            if _mentions_self(state, character_id, topic):
                stance = "support" if polarity > 0 else "oppose"
                reason = "事关本人权位"
            else:
                for other in state.characters.values():
                    if other.id == character_id or other.name not in topic:
                        continue
                    affinity = relation_value(state, character_id, other.id)
                    if character.bloc and character.bloc == other.bloc:
                        affinity += 50
                    if affinity >= 20:
                        stance = "support" if polarity > 0 else "oppose"
                        reason = f"与{other.name}同气连枝"
                        break
                    if affinity <= -20:
                        stance = "oppose" if polarity > 0 else "support"
                        reason = f"与{other.name}素有嫌隙"
                        break
        if stance is None:
            loyalty = float(character.traits.get("loyalty", 5))
            if loyalty >= 8:
                stance, reason = "support", "忠谨，唯上意是从"
            elif loyalty <= 3:
                stance, reason = "oppose", "素怀异见"
            else:
                stance, reason = "neutral", "持两端，未肯表态"
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
    return result


def _mentions_self(state: WorldState, character_id: str, topic: str) -> bool:
    character = state.characters[character_id]
    if character.name in topic:
        return True
    for office_id in held_offices(character):
        office = state.offices.get(office_id)
        if office and office.name in topic:
            return True
    return any(army.commander == character_id and army.name in topic for army in state.armies.values())


def debate_summary(stances: list[dict[str, Any]]) -> str:
    groups: dict[str, list[str]] = {"support": [], "oppose": [], "neutral": []}
    for row in stances:
        groups[row["stance"]].append(row["name"])
    parts = [
        f"{STANCE_LABELS[key]} {len(names)} 人" + (f"（{'、'.join(names)}）" if names else "")
        for key, names in groups.items()
    ]
    return "；".join(parts) + "。廷议结论只作谏言，须经诏书方能施行。"


def persona_card(state: WorldState, character_id: str) -> dict[str, Any]:
    character = state.characters[character_id]
    return {
        "character": character.id,
        "name": character.name,
        "title": character.title,
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
    for row in stances:
        character = state.characters[row["character"]]
        digest = render_digest(state, seed, character.id, report, scenario)
        prompt = render_prompt(
            "court_debate",
            {
                "name": character.name,
                "persona": character.persona,
                "speech_style": character.speech_style,
                "stance": row["stance"],
                "stance_label": row["stance_label"],
                "reason": row["reason"],
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
        allowed = digest_numbers(digest) | {float(item) for item in _NUMBER.findall(topic)}
        if not text or any(float(item) not in allowed for item in _NUMBER.findall(text)):
            text = template_speech(character.name, row["stance"], digest)
        speeches.append({"character": character.id, "name": character.name, "stance": row["stance"], "reason": row["reason"], "text": text})
        previous.append(f"{character.name}（{row['stance_label']}）：{text}")
    return speeches


def template_speech(name: str, stance: str, digest: str) -> str:
    """不依赖模型的模板台词：只引用摘要中的第一条国势。"""
    opener = {"support": "臣以为此议可行", "oppose": "臣以为此议不可", "neutral": "臣以为此事宜再详议"}[stance]
    national = next((line for line in digest.splitlines() if line.startswith("【国势】")), "")
    first = national.removeprefix("【国势】").split("；")[0] if national else ""
    cite = f"今{first}，不可不察。" if first else ""
    return f"{opener}。{cite}"
