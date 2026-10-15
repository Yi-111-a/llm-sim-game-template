"""隐藏数值、公开视图、奏疏、StateDigest、廷议立场。"""

import asyncio
import json
import random
import re

import pytest

from simcore.agent.debate import debate_speeches, decide_stances, template_speech
from simcore.agent.roles import interpret_edict, minister_chat_messages
from simcore.domain.apply import apply
from simcore.domain.digest import digest_numbers, render_digest
from simcore.domain.memorials import MemorialError, adopted_ops, visible_memorials
from simcore.domain.turn import resolve_turn
from simcore.domain.visibility import estimate_interval, public_crisis, public_report, public_view
from simcore.llm.base import ChatResult
from simcore.llm.mock import MockProvider
from simcore.scenario.loader import build_initial_state


def test_public_view_replaces_hidden_resources_with_stable_interval(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    state.resources["ambition"].amount = 47.3
    view = public_view(state, seed=11)
    ambition = view["resources"]["ambition"]
    assert ambition["hidden"] is True and "amount" not in ambition
    low, high = ambition["estimate"]
    assert low <= 47.3 <= high
    assert high - low == pytest.approx(2 * 8 * 1.4, abs=0.2)  # 半宽 = 8 ×（1 + 积弊 40/100）
    assert public_view(state, seed=11)["resources"]["ambition"] == ambition
    assert "47.3" not in json.dumps(view, ensure_ascii=False)
    assert "ambition" not in view["characters"]["plotter"]["traits"]
    assert "rng_state" not in view


def test_intel_flag_halves_width_and_noise_changes_by_turn(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    low, high = estimate_interval(state, "ambition", 3)
    state.flags["intel:ambition"] = True
    narrow_low, narrow_high = estimate_interval(state, "ambition", 3)
    assert (narrow_high - narrow_low) == pytest.approx((high - low) / 2, abs=0.2)
    centers = set()
    for turn in range(6):
        state.turn = turn
        lo, hi = estimate_interval(state, "ambition", 3)
        assert lo <= 40 <= hi
        centers.add(round((lo + hi) / 2, 2))
    assert len(centers) > 1


def test_public_report_and_crisis_never_expose_hidden_values(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    state, _ = apply({"op": "adjust_resource", "resource": "ambition", "delta": 7.7, "reason": "加官"}, state)
    updated, report = resolve_turn(state, random.Random(1), mech_scenario)
    crisis = mech_scenario["scenario"]["crisis"]
    public = public_report(report, updated, 5, crisis)
    text = json.dumps(public, ensure_ascii=False)
    true_value = updated.resources["ambition"].amount
    assert str(true_value) not in text
    assert all(line["amount"] is None for line in public["ledger_lines"] if line["resource"] == "ambition")
    terms = {row["id"]: row for row in public["crisis"]["terms"]}
    assert terms["amb"]["estimated"] is True and isinstance(terms["amb"]["value"], list)
    exact_term = 0.1 * (updated.resources["ambition"].amount - 40)
    assert terms["amb"]["value"][0] <= exact_term + 0.6 and terms["amb"]["value"][1] >= exact_term - 0.6
    assert terms["cor"]["estimated"] is False
    view_crisis = public_crisis(crisis, updated, 5)
    assert view_crisis["value_or_estimate"] == pytest.approx(updated.resources["risk"].amount)
    assert view_crisis["band"] == "承平"


def test_memorial_visibility_window_conditions_and_adoption(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    assert [item["id"] for item in visible_memorials(mech_scenario, state)] == ["m_tax"]
    item = visible_memorials(mech_scenario, state)[0]
    assert item["advisors"][0]["name"] == "李相"
    assert item["options"][0]["ops_summary"] == ["国库 −50", "记入档案（影响后续事件）"]
    ops = adopted_ops(mech_scenario, state, "m_tax", "approve")
    assert ops[0]["op"] == "adjust_resource"
    with pytest.raises(MemorialError) as missing:
        adopted_ops(mech_scenario, state, "m_tax", "ghost")
    assert missing.value.code == "UNKNOWN_OPTION"
    with pytest.raises(MemorialError) as hidden:
        adopted_ops(mech_scenario, state, "m_border", "reinforce")
    assert hidden.value.code == "MEMORIAL_UNAVAILABLE"
    state.flags["petition_seen"] = True
    assert {item["id"] for item in visible_memorials(mech_scenario, state)} == {"m_tax", "m_border"}
    state.turn = 2
    assert [item["id"] for item in visible_memorials(mech_scenario, state)] == ["m_border"]
    state.flags["memorial:m_border"] = "reinforce"
    assert visible_memorials(mech_scenario, state) == []


def test_edict_draft_combines_memorial_client_and_text_sources(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    receipt = asyncio.run(
        interpret_edict(
            {"interior": "国库增加 10"},
            mech_scenario,
            state,
            None,
            seed=1,
            adopted=[{"memorial_id": "m_tax", "option_id": "approve"}, {"memorial_id": "m_tax", "option_id": "deny"}, {"memorial_id": "zz", "option_id": "x"}],
            client_ops=[{"op": "appoint", "character": "chancellor", "office": "censor"}, {"op": "move_army", "army": "guard", "to_region": "capital"}],
        )
    )
    sources = [(clause["source"], clause["status"], clause.get("code")) for clause in receipt["clauses"]]
    assert sources == [
        ("memorial", "applied", None),
        ("memorial", "applied", None),
        ("memorial", "rejected", "DUPLICATE_MEMORIAL"),
        ("memorial", "rejected", "UNKNOWN_MEMORIAL"),
        ("client", "applied", None),
        ("client", "rejected", "INVALID_TARGET"),
        ("text", "narrative_only", None),
    ]
    assert receipt["adopted"] == [{"memorial_id": "m_tax", "option_id": "approve"}]
    assert state.resources["treasury"].amount == 1000


def test_digest_uses_public_values_and_character_scope(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    state.resources["ambition"].amount = 43.21
    digest = render_digest(state, 2, None, None, mech_scenario)
    assert "异志 约" in digest and "（风闻）" in digest
    assert "43.21" not in digest and "43.2" not in digest
    assert "北镇" in digest and "叛军主力" in digest
    personal = render_digest(state, 2, "chancellor", None, mech_scenario)
    assert "【本人】李相" in personal and "宰相" in personal
    region_line = next(line for line in personal.splitlines() if line.startswith("【地区】"))
    assert "北镇" not in region_line  # 京畿只与雄关相邻：北镇只在「他处」一行里出现名字
    assert "【他处】" in personal and "北镇" in personal.split("【他处】")[1].splitlines()[0]
    assert "禁军" in personal and "叛军主力" not in personal
    messages = minister_chat_messages("chancellor", state, "边事如何？", mech_scenario, 2)
    assert "StateDigest" in messages[0]["content"] and "李相" in messages[0]["content"]


def test_debate_stances_from_topic_table_interest_relation_and_loyalty(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    table = decide_stances(state, mech_scenario, "边镇增兵议", ["chancellor", "general", "eunuch", "plotter"])
    stances = {row["character"]: row["stance"] for row in table}
    assert stances == {"chancellor": "support", "general": "oppose", "eunuch": "support", "plotter": "oppose"}
    interest = decide_stances(state, mech_scenario, "罢李相宰相之职", ["chancellor", "plotter", "general"])
    by_id = {row["character"]: row for row in interest}
    assert by_id["chancellor"]["stance"] == "oppose" and by_id["chancellor"]["reason"] == "事关本人权位"
    assert by_id["plotter"]["stance"] == "support" and "嫌隙" in by_id["plotter"]["reason"]
    assert by_id["general"]["stance"] == "support"  # 忠诚 9：唯上意是从
    assert decide_stances(state, mech_scenario, "议礼", ["chancellor"])[0]["stance"] == "neutral"


def test_mock_debate_speeches_cite_only_digest_numbers(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    stances = decide_stances(state, mech_scenario, "边镇增兵议", ["chancellor", "general"])
    speeches = asyncio.run(debate_speeches(state, mech_scenario, "边镇增兵议", stances, MockProvider(mech_scenario), seed=3))
    assert [row["stance"] for row in speeches] == ["support", "oppose"]
    for row in speeches:
        digest = render_digest(state, 3, row["character"], None, mech_scenario)
        numbers = {float(item) for item in re.findall(r"\d+(?:\.\d+)?", row["text"])}
        assert numbers and numbers <= digest_numbers(digest)


def test_debate_falls_back_to_template_when_model_invents_numbers(mech_scenario) -> None:
    class Inventive:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def chat(self, messages, **_):
            self.prompts.append(messages[0]["content"])
            return ChatResult(content="臣闻国库仅余 12345 万贯。")

    state = build_initial_state(mech_scenario)
    stances = decide_stances(state, mech_scenario, "边镇增兵议", ["chancellor"])
    provider = Inventive()
    speeches = asyncio.run(debate_speeches(state, mech_scenario, "边镇增兵议", stances, provider, seed=3))
    digest = render_digest(state, 3, "chancellor", None, mech_scenario)
    assert speeches[0]["text"] == template_speech("李相", "support", digest)
    assert len(provider.prompts) == 1
    assert "李相" in provider.prompts[0] and "郭将军" not in provider.prompts[0].split("事实摘要")[0]
