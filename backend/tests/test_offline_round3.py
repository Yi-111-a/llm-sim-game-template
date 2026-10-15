"""第三轮：离线（mock）纪事 / 召对 / 廷议模板、显示名、内部事件、区间显示。"""

from __future__ import annotations

import asyncio
import random
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from simcore.agent.debate import debate_speeches, decide_stances
from simcore.agent.offline import chronicle_numbers, offline_chronicle, self_term
from simcore.agent.roles import narrator, offline_minister_text
from simcore.domain.battle import resolve_battles
from simcore.domain.digest import digest_numbers, render_digest
from simcore.domain.models import TurnReport
from simcore.domain.turn import resolve_turn
from simcore.domain.visibility import format_range, format_value, public_report, redacted_report
from simcore.llm.mock import MockProvider
from simcore.mcp.server import _turn_text
from simcore.scenario.loader import build_initial_state, load_scenario
from simcore.service import GameService

CANNED = "臣已记录所议"
DIGITS = re.compile(r"\d+(?:\.\d+)?")
TIANBAO = Path(__file__).resolve().parents[3] / "scenarios" / "tianbao_751"


def _advance(scenario: dict[str, Any], turns: int, seed: int = 7) -> tuple[Any, TurnReport]:
    state = build_initial_state(scenario)
    report = TurnReport(turn=0)
    for _ in range(turns):
        state, report = resolve_turn(state, random.Random(seed + state.turn), scenario)
    return state, report


def _sentences(text: str) -> int:
    return len([part for part in text.split("。") if part.strip()])


# ---- 1. 离线纪事 -------------------------------------------------------------------


def test_offline_chronicle_is_deterministic_grouped_and_number_safe(mech_scenario) -> None:
    state, report = _advance(mech_scenario, 1)
    redacted = redacted_report(report, state)
    context = {"edicts": ["减赋之议"], "plain_edicts": True, "choices": []}
    first = offline_chronicle(redacted, state, mech_scenario, seed=3, context=context)
    assert first == offline_chronicle(redacted, state, mech_scenario, seed=3, context=context)
    assert first.startswith(("是季，", "是时，")) and 2 <= _sentences(first) <= 5
    assert "「减赋之议」" in first and CANNED not in first
    allowed = chronicle_numbers(redacted)
    assert all(any(abs(float(number) - value) < 1e-6 for value in allowed) for number in DIGITS.findall(first))
    # 换种子措辞会变（多季中至少一季不同），但事实与数字不变
    variants = set()
    for seed in range(8):
        variants.add(offline_chronicle(redacted, state, mech_scenario, seed=seed, context=context))
    assert len(variants) > 1


def test_narrator_uses_offline_chronicle_with_mock_and_passes_guard(mech_scenario) -> None:
    state, report = _advance(mech_scenario, 2)
    redacted = redacted_report(report, state)
    text = asyncio.run(narrator(redacted, MockProvider(mech_scenario), mech_scenario, state=state, seed=1))
    assert CANNED not in text and text.startswith(("是季", "是时"))
    assert text == offline_chronicle(redacted, state, mech_scenario, seed=1)  # 未被数字核对退回


def test_chronicle_templates_can_be_overridden_by_scenario(mech_scenario, tmp_path: Path) -> None:
    state, report = _advance(mech_scenario, 1)
    inline = {**mech_scenario, "scenario": {**mech_scenario["scenario"], "chronicle_templates": {"opening": ["是岁，"]}}}
    assert offline_chronicle(redacted_report(report, state), state, inline, seed=0).startswith("是岁，")
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "chronicle_templates.yaml").write_text("opening: [当是时，]\nclosing: [余皆如故。]\n", encoding="utf-8")
    from_file = {**mech_scenario, "root": str(tmp_path)}
    assert offline_chronicle(redacted_report(report, state), state, from_file, seed=0).startswith("当是时，")


def test_empty_season_chronicle_still_has_two_sentences(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    text = offline_chronicle(TurnReport(turn=1), state, mech_scenario, seed=0)
    assert text.startswith("是季，") and _sentences(text) == 2


# ---- 2. 离线召对与廷议 ---------------------------------------------------------------


def test_offline_minister_reply_is_in_character_and_cites_only_digest_numbers(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    digest = render_digest(state, 5, "chancellor", None, mech_scenario)
    reply = offline_minister_text("chancellor", state, "国库如何？", mech_scenario, seed=5)
    assert reply == offline_minister_text("chancellor", state, "国库如何？", mech_scenario, seed=5)
    assert CANNED not in reply and "国库" in reply
    assert {float(item) for item in DIGITS.findall(reply)} <= digest_numbers(digest)
    replies = {offline_minister_text("chancellor", state, "国库如何？", mech_scenario, seed=seed) for seed in range(8)}
    assert len(replies) > 1


def test_self_term_comes_from_speech_style_or_greeting() -> None:
    assert self_term("自称「老奴」，话少", {}) == "老奴"
    assert self_term("娇憨亲昵，称「三郎」或「陛下」", {"greeting": "陛下来得迟了，妾等了半日。"}) == "妾"
    assert self_term("称「臣」", {"greeting": "臣胡人禄山，叩见陛下！"}) == "臣胡人"
    assert self_term("", {}) == "臣"


def test_offline_debate_lines_follow_engine_stance(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    stances = decide_stances(state, mech_scenario, "边镇增兵议", ["chancellor", "general"])
    speeches = asyncio.run(debate_speeches(state, mech_scenario, "边镇增兵议", stances, MockProvider(mech_scenario), seed=3))
    assert [row["stance"] for row in speeches] == ["support", "oppose"]
    assert "未敢苟同" in speeches[1]["text"] or "未尽" in speeches[1]["text"] or "不可" in speeches[1]["text"]
    assert speeches[1]["text"].startswith("李相")  # 回应上一位发言人
    for row in speeches:
        assert CANNED not in row["text"]
        digest = render_digest(state, 3, row["character"], None, mech_scenario)
        assert {float(item) for item in DIGITS.findall(row["text"])} <= digest_numbers(digest)


def test_audience_sse_streams_offline_reply(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SIM_SCENARIOS_DIR", str(Path(__file__).parent / "fixtures" / "scenarios"))
    monkeypatch.setenv("LLM_SIM_HOME", str(tmp_path))
    client = TestClient(create_app())
    session_id = client.post("/api/sessions", json={"scenario": "mech_test", "seed": 2}).json()["id"]
    response = client.post(f"/api/sessions/{session_id}/chat/chancellor", json={"text": "国库如何？"})
    assert response.status_code == 200
    pieces = re.findall(r'event: message\ndata: (\{.*?\})\n', response.text)
    assert len(pieces) > 1
    import json

    text = "".join(json.loads(piece)["text"] for piece in pieces)
    assert CANNED not in text and "国库" in text and "event: done" in response.text


# ---- 3. 显示名 -----------------------------------------------------------------------


def test_battle_report_carries_display_names_and_text(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    state.armies["rebel_army"].region = "pass"
    state.regions["pass"].stats["defense"] = 0  # 不据险，必分胜负
    battles, facts = resolve_battles(state, random.Random(1), mech_scenario["scenario"].get("wars"), set())
    battle = battles[0]
    assert battle["attacker_name"] == "叛军" and battle["defender_name"] == "朝廷"
    assert battle["winner_name"] in {"叛军", "朝廷"} and battle["text"].startswith("叛军攻朝廷")
    assert battle["attacker_loss"] > 0 and battle["defender_loss"] > 0
    assert not any(token in fact.text for fact in facts for token in ("rebels", "court", "pass"))


def _entity_ids(scenario: dict[str, Any], state: Any) -> set[str]:
    ids: set[str] = set()
    for table in (state.characters, state.factions, state.regions, state.armies, state.offices, state.resources):
        ids.update(str(key) for key in table)
    ids.update(str(row.get("id")) for row in scenario.get("events", []) or [] if isinstance(row, dict))
    ids.update(str(row.get("id")) for row in scenario.get("scenario", {}).get("stats", []) or [] if isinstance(row, dict))
    return {item for item in ids if len(item) >= 2}


def _raw_ids(text: str, ids: set[str]) -> list[str]:
    cleaned = re.sub(r"`[^`]*`|\[[^\]\n]*\]|session_id\S*", "", text)
    return sorted({token for token in re.findall(r"[A-Za-z][A-Za-z0-9_]*", cleaned) if token in ids})


@pytest.mark.skipif(not TIANBAO.is_dir(), reason="天宝剧本不在仓库中")
def test_full_tianbao_game_texts_contain_no_raw_entity_ids(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SIM_SCENARIOS_DIR", str(TIANBAO.parent))
    monkeypatch.setenv("LLM_SIM_HOME", str(tmp_path))
    service = GameService(lambda scenario, kind: MockProvider(scenario))
    session_id, session = service.create_session("tianbao_751", 11)
    ids = _entity_ids(session.scenario, session.state)
    leaks: dict[str, list[str]] = {}

    def scan(where: str, text: str) -> None:
        found = _raw_ids(text, ids)
        if found:
            leaks.setdefault(where, []).extend(found)

    async def play() -> None:
        for turn in range(60):
            state = session.state
            scan(f"briefing@{turn}", service.briefing(session_id))
            people = [cid for cid, row in state.characters.items() if row.status == "alive" and cid != service.player_character(session)]
            if people:
                reply = await service.chat(session_id, people[turn % len(people)], "边事与国用如何？")
                scan(f"audience@{turn}", reply["content"])
            if turn % 3 == 0 and len(people) >= 2:
                debate = await service.debate(session_id, f"议{state.characters[people[0]].name}之任", people[:3])
                for row in debate["speeches"]:
                    scan(f"debate@{turn}", row["text"])
            memorials = service.view(session_id)["memorials"]
            if memorials:
                receipt = await service.draft_edict(
                    session_id, {}, [{"memorial_id": memorials[0]["id"], "option_id": memorials[0]["options"][0]["id"]}], []
                )
                service.confirm_edict(session_id, receipt["id"])
            result = await service.end_turn(session_id)
            report = result["report"]
            scan(f"memorial@{turn}", result["memorial"])
            for fact in report["facts"]:
                scan(f"fact@{turn}", f"{fact['text']} {fact['display']}")
            for line in report["ledger_lines"]:
                scan(f"ledger@{turn}", line["display"])
            for battle in report["battles"]:
                scan(f"battle@{turn}", battle["text"])
            for event in report["events"]:
                scan(f"event@{turn}", f"{event['title']} {event['text']}")
            scan(f"warnings@{turn}", "；".join(report.get("warnings", [])))
            scan(f"mcp@{turn}", _turn_text(result))
            if result["outcome"]:
                break
        else:
            raise AssertionError("60 季内未终局")

    asyncio.run(play())
    assert not leaks, leaks


# ---- 4. 内部事件 -----------------------------------------------------------------------


def test_internal_events_are_logged_but_not_featured(mech_scenario) -> None:
    scenario = {
        **mech_scenario,
        "events": [
            {"id": "setup_books", "title": "岁计定额", "kind": "system", "when": {"turn": 1}, "gravity": 1.0, "text": "定岁计。",
             "effects": [{"op": "add_ledger", "name": "边费", "category": "treasury", "amount": -1, "kind": "recurring"}]},
            {"id": "quiet_flag", "title": "暗记", "when": {"turn": 1}, "gravity": 1.0, "effects": [{"op": "set_flag", "key": "q", "value": True}]},
            {"id": "border_clash", "title": "边衅", "when": {"turn": 1}, "gravity": 1.0, "text": "边军小挫。",
             "effects": [{"op": "adjust_army", "army": "pass_army", "delta": -100}], "illustration": "event.clash"},
            *mech_scenario.get("events", []),
        ],
    }
    state, report = _advance(scenario, 1)
    assert {"setup_books", "quiet_flag", "border_clash", "petition"} <= set(report.events_fired)  # 日志里都在
    internal = {row["id"] for row in report.events if row["internal"]}
    assert internal == {"setup_books", "quiet_flag"}
    assert all(fact.internal for fact in report.facts if fact.kind == "ledger")
    public = public_report(report, state, 0) or {}
    assert "setup_books" not in public["events_fired"] and "quiet_flag" not in public["events_fired"]
    assert [row["id"] for row in public["events"]] == ["border_clash", "petition"]  # 兵事 > 带抉择
    assert public["featured_event"] == {
        "id": "border_clash", "title": "边衅", "text": "边军小挫。", "illustration": "event.clash", "importance": 2,
    }
    assert not any(fact["text"] in {"岁计定额", "暗记"} for fact in public["facts"])
    assert "岁计" not in offline_chronicle(redacted_report(report, state), state, scenario, seed=0)


def test_outcome_is_featured_above_everything(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    state.regions["capital"].controller = "rebels"
    _, report = resolve_turn(state, random.Random(0), mech_scenario)
    assert report.outcome is not None
    public = public_report(report, state, 0) or {}
    assert public["featured_event"]["id"] == report.outcome.id and public["featured_event"]["importance"] == 3


# ---- 5. 区间显示 ---------------------------------------------------------------------


def test_range_display_strings_are_compact() -> None:
    assert format_range(55.3, 59.8) == "约 55–60"
    assert format_range(55.0, 55.0) == "约 55"
    assert format_range(1.24, 1.26) == "约 1.2–1.3"
    assert format_range(0.3, 0.9, signed=True) == "+0.3～+0.9"
    assert format_range(-0.9, -0.3, signed=True) == "-0.9～-0.3"
    assert format_range(0.5, 0.52, signed=True) == "+0.5"
    assert format_range(-3.2, 2.1) == "约 -4～3"
    assert format_value([55.3, 59.8]) == "约 55–60" and format_value(12.34) == "12.3"
    assert format_value(0.4, signed=True) == "+0.4"


def test_view_crisis_display_has_no_malformed_ranges(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 1)
    crisis = service.view(session_id)["crisis"]
    for text in [crisis["display"], *(term["display"] for term in crisis["terms"])]:
        assert "–-" not in text and "约约" not in text and "（风闻）" not in text


@pytest.mark.skipif(not TIANBAO.is_dir(), reason="天宝剧本不在仓库中")
def test_tianbao_opening_ledger_is_internal() -> None:
    scenario = load_scenario(TIANBAO)
    state, report = _advance(scenario, 1)
    assert "opening_ledger" in report.events_fired
    public = public_report(report, state, 0) or {}
    assert "opening_ledger" not in public["events_fired"]
    assert all(row["id"] != "opening_ledger" for row in public["events"])
