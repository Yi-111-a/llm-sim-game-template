"""第 6 轮（GPT gpt-04 / gpt-05 报告）引擎修复：廷议立场对应谏言所指选项、易帅回执与状态一致、任命回执合并为一条、
未确认草案不得悄然遗失（DRAFT_UNCONFIRMED）、本季新出奏疏提示、分兵 detach、养兵 upkeep、终局复盘要事、
人设头衔随官职更新、export_log 记 client 模式召对 / 廷议、延期标签。"""

import asyncio
import copy
import re

import pytest
from conftest import FIXTURE_SCENARIOS
from fastapi.testclient import TestClient

from simcore.agent.debate import decide_stances, persona_card
from simcore.agent.offline import refresh_greeting
from simcore.agent.tools import PLAYER_TIER, AgentTools
from simcore.domain.apply import OpRejected, apply
from simcore.domain.digest import render_digest
from simcore.domain.memorials import adopted_ops, summarize_ops
from simcore.domain.turn import resolve_turn
from simcore.mcp.server import build_server
from simcore.scenario.loader import build_initial_state, load_scenario, validate_scenario
from simcore.service import GameError, GameService


def _scenario() -> dict:
    scenario = load_scenario(FIXTURE_SCENARIOS / "mech_test")
    scenario["scenario"]["offices"].append({"id": "pass_jds", "name": "关镇节度", "army": "pass_army"})
    scenario["scenario"]["offices"].append({"id": "guard_jds", "name": "禁军统领", "army": "guard"})
    return scenario


def _service_with(scenario: dict) -> GameService:
    service = GameService()
    service.scenarios["mech_test"] = scenario
    return service


def _draft(service: GameService, session_id: str, **kwargs) -> dict:
    return asyncio.run(service.draft_edict(session_id, use_provider=False, **kwargs))


def _turn(service: GameService, session_id: str, **kwargs) -> dict:
    return asyncio.run(service.end_turn(session_id, narrate=False, **kwargs))


# ---- 1. 廷议立场对应谏言所指的选项 ----------------------------------------------------------------------


def _review_memorial() -> dict:
    """仿 gpt-04 T3「岁末考课」：赞成者的话与「依例考课」共用「考课」，反对者的话复述「骤加黜陟」。"""
    return {
        "id": "m_review",
        "turn": 0,
        "title": "岁末考课",
        "summary": "吏部、御史台奏岁末考课，诸州刺史多有贪墨不职者，请加黜陟。",
        "advisors": [
            {"character": "general", "stance": "support", "line": "考课不严，则吏不畏法。请黜其尤者以儆百官。"},
            {"character": "chancellor", "stance": "oppose", "line": "陛下圣明。然州郡多事，臣恐骤加黜陟，人人自危。"},
        ],
        "options": [
            {
                "id": "strict",
                "label": "严加黜陟",
                "hint": "积弊 −3",
                "ops": [{"op": "adjust_resource", "resource": "corruption", "delta": -3, "reason": "黜贪"}],
            },
            {
                "id": "lenient",
                "label": "依例考课，不另究",
                "hint": "积弊 +1",
                "ops": [{"op": "adjust_resource", "resource": "corruption", "delta": 1, "reason": "因循"}],
            },
        ],
    }


def test_debate_stance_maps_to_the_advisors_own_option() -> None:
    scenario = _scenario()
    scenario["memorials"].append(_review_memorial())
    state = build_initial_state(scenario)
    rows = {row["character"]: row for row in decide_stances(state, scenario, "", ["general", "chancellor", "eunuch"], memorial_id="m_review", seed=13)}
    # 赞成严考课者主张 strict；反对者主张 lenient 并反对 strict（gpt-04 #1 正好倒置）
    assert rows["general"]["stance"] == "support" and rows["general"]["target_option_id"] == "strict"
    assert rows["chancellor"]["stance"] == "oppose" and rows["chancellor"]["target_option_id"] == "lenient"
    assert rows["chancellor"]["against_option_id"] == "strict"
    assert rows["general"]["proposal_id"] == "strict" and rows["general"]["proposal_basis"] == "奏疏所请"


def test_free_choice_memorial_does_not_adopt_first_option_as_proposition(mech_env) -> None:
    scenario = _scenario()
    scenario["memorials"].append(
        {
            "id": "m_guest",
            "turn": 0,
            "title": "节度在京",
            "summary": "安节度奉召至京，去留之间，朝议纷纭。",
            "advisors": [{"character": "chancellor", "stance": "neutral", "line": "留之，兵谁管？放之，又如何收得回？"}],
            "options": [
                {"id": "honor", "label": "加官厚赐，遣还北镇", "ops": [{"op": "adjust_resource", "resource": "treasury", "delta": -100}]},
                {"id": "keep", "label": "留京宿卫，不夺其官", "ops": [{"op": "adjust_resource", "resource": "risk", "delta": -6}]},
                {"id": "detain", "label": "夺其节度，留京奉朝请", "ops": [{"op": "adjust_resource", "resource": "risk", "delta": -10}]},
            ],
        }
    )
    state = build_initial_state(scenario)
    rows = {row["character"]: row for row in decide_stances(state, scenario, "", ["eunuch", "chancellor"], memorial_id="m_guest", seed=31)}
    eunuch = rows["eunuch"]  # 忠诚 10、非谏言人：从前写成「赞成——唯上意是从」并附议首个选项
    assert eunuch["proposal_id"] is None
    assert eunuch["stance"] == "neutral" and eunuch["reason"] == "议无定案，候上裁决"
    assert eunuch["target_option_id"] is None and "附议" not in eunuch.get("target_reason", "")
    service = _service_with(scenario)
    session_id, _ = service.create_session("mech_test", 31)
    brief = service.debate_brief(session_id, "", ["eunuch", "chancellor"], "m_guest")
    assert "诸案并陈，未有定案" in brief["proposition"] and brief["proposal"] is None
    # 调用方可指定所议之案：赞成即主此案
    named = service.debate_brief(session_id, "", ["eunuch"], "m_guest", option_id="detain")
    assert named["proposal"]["id"] == "detain" and "所议之案为「夺其节度，留京奉朝请」" in named["proposition"]
    assert named["stances"][0]["target_option_id"] == "detain"
    with pytest.raises(GameError) as caught:
        service.debate_brief(session_id, "", ["eunuch"], "m_guest", option_id="nope")
    assert caught.value.code == "UNKNOWN_OPTION" and "honor" in caught.value.message


def test_advisor_option_binding_and_validation() -> None:
    scenario = _scenario()
    memorial = _review_memorial()
    memorial["advisors"][1]["option"] = "lenient"
    memorial["advisors"].append({"character": "eunuch", "stance": "neutral", "line": "此事宜缓。", "option": "lenient"})
    scenario["memorials"].append(memorial)
    assert validate_scenario(scenario) == []
    state = build_initial_state(scenario)
    rows = {row["character"]: row for row in decide_stances(state, scenario, "", ["chancellor", "eunuch"], memorial_id="m_review", seed=1)}
    assert rows["eunuch"]["target_option_id"] == "lenient" and rows["eunuch"]["target_reason"] == "所上之言即主此案"
    assert rows["chancellor"]["target_option_id"] == "lenient" and rows["chancellor"]["against_option_id"] == "strict"
    broken = copy.deepcopy(scenario)
    broken["memorials"][-1]["advisors"][0]["option"] = "nope"
    broken["memorials"][-1]["proposal"] = "nada"
    errors = validate_scenario(broken)
    assert any("option 不在选项中：nope" in line for line in errors) and any("proposal 不在选项中：nada" in line for line in errors)


# ---- 2. 易帅：人离军而去即解除原统之军（官职所统除外，兼统）——回执与状态一致 -----------------------------------


def test_dispatched_commander_releases_left_behind_army_in_state_and_text() -> None:
    state = build_initial_state(_scenario())
    updated, facts = apply({"op": "set_army_commander", "army": "pass_army", "character": "general", "dispatch": True}, state)
    assert "其原统禁军不再由其统领（暂无主帅，副将摄事）" in facts[0].text
    assert updated.armies["guard"].commander is None and updated.armies["pass_army"].commander == "general"
    # 禁军是他所任官职（禁军统领）所统：官在军在，仍兼统，回执写「仍兼统」
    held, _ = apply({"op": "appoint", "character": "general", "office": "guard_jds"}, state)
    moved, facts = apply({"op": "set_army_commander", "army": "pass_army", "character": "general", "dispatch": True}, held)
    assert "仍兼统禁军（人不在军中，副将暂摄）" in facts[0].text and moved.armies["guard"].commander == "general"
    # 事实摘要：统帅离军赴他处，不写成「未到任」
    digest = render_digest(moved, 0)
    assert "统帅郭将军离军他往（自京畿赴雄关" in digest


# ---- 3. 同一选项里的任命与易帅合并为一条 ------------------------------------------------------------------


def test_appointment_and_commander_ops_merge_into_one_coherent_line(mech_env) -> None:
    scenario = _scenario()
    ops = [
        {"op": "appoint", "character": "eunuch", "office": "pass_jds"},
        {"op": "set_army_commander", "army": "pass_army", "character": "eunuch", "dispatch": True},
    ]
    scenario["memorials"].append({"id": "m_post", "turn": 0, "title": "关镇择帅", "options": [{"id": "eunuch", "label": "以高内侍镇关", "ops": ops}]})
    scenario["events"].append(
        {
            "id": "pick",
            "title": "关镇择帅（事件）",
            "when": {"turn": 1},
            "gravity": 1,
            "choices": [{"id": "eunuch", "text": "高内侍", "effects": ops}],
        }
    )
    state = build_initial_state(scenario)
    lines = summarize_ops(ops, state)
    assert len(lines) == 1 and "已启程赴任" in lines[0] and "遥领" not in lines[0]
    merged = adopted_ops(scenario, state, "m_post", "eunuch")
    assert merged == [{"op": "appoint", "character": "eunuch", "office": "pass_jds", "dispatch": True}]
    service = _service_with(scenario)
    session_id, _ = service.create_session("mech_test", 1)
    _turn(service, session_id)
    result = service.choose(session_id, "choice-pick-1", "eunuch")
    texts = [fact["text"] for fact in result["facts"]]
    assert not any("遥领，不赴任" in text for text in texts)
    assert sum("已启程赴任" in text for text in texts) == 1


# ---- 4. 未确认的草案：end_turn 拒绝（DRAFT_UNCONFIRMED），可显式放弃 --------------------------------------------


def test_end_turn_refuses_unconfirmed_draft_and_confirm_lists_pending_ids(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 7)
    receipt = _draft(service, session_id, adopted=[{"memorial_id": "m_tax", "option_id": "approve"}])
    with pytest.raises(GameError) as caught:
        _turn(service, session_id)
    assert caught.value.code == "DRAFT_UNCONFIRMED" and caught.value.status == 409 and receipt["id"] in caught.value.message
    # gpt-05 T1：编号里混进了括注
    with pytest.raises(GameError) as caught:
        service.confirm_edict(session_id, f"{receipt['id']}（未生效；确认请调用")
    assert caught.value.code == "EDICT_NOT_FOUND" and f"本季待确认的草案：{receipt['id']}" in caught.value.message
    # 显式放弃后可结算；放弃的草案不能再确认
    result = _turn(service, session_id, discard_drafts=True)
    assert result["discarded_drafts"] == [receipt["id"]]
    with pytest.raises(GameError) as caught:
        service.confirm_edict(session_id, receipt["id"])
    assert caught.value.code == "EDICT_STALE"


def test_superseded_or_dead_drafts_do_not_block_end_turn(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 7)
    # 只有被拒条款的草案不算
    _draft(service, session_id, ops=[{"op": "adjust_resource", "resource": "grain", "delta": 5}])
    # 拟诏—改拟—确认改拟稿：旧稿已被取代
    _draft(service, session_id, adopted=[{"memorial_id": "m_tax", "option_id": "approve"}])
    second = _draft(
        service, session_id, adopted=[{"memorial_id": "m_tax", "option_id": "deny"}], ops=[{"op": "move_army", "army": "guard", "to_region": "pass"}]
    )
    service.confirm_edict(session_id, second["id"])
    assert _turn(service, session_id)["discarded_drafts"] == []


def test_rest_turn_mirrors_draft_guard(mech_env) -> None:
    from api.app import create_app

    client = TestClient(create_app())
    session_id = client.post("/api/sessions", json={"scenario": "mech_test", "seed": 3}).json()["id"]
    receipt = client.post(f"/api/sessions/{session_id}/edicts/draft", json={"ops": [{"op": "move_army", "army": "guard", "to_region": "pass"}]}).json()
    refused = client.post(f"/api/sessions/{session_id}/turn")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "DRAFT_UNCONFIRMED"
    assert receipt["id"] in refused.json()["detail"]["message"]
    missing = client.post(f"/api/sessions/{session_id}/edicts/edict-zzz/confirm")
    assert missing.status_code == 404 and receipt["id"] in missing.json()["detail"]["message"]
    done = client.post(f"/api/sessions/{session_id}/turn", json={"discard_drafts": True})
    assert done.status_code == 200 and done.json()["discarded_drafts"] == [receipt["id"]]


# ---- 5. 本季新出奏疏 -------------------------------------------------------------------------------------


def test_confirm_and_choose_report_newly_unlocked_memorials(mech_env) -> None:
    scenario = _scenario()
    scenario["memorials"].append(
        {
            "id": "m_unlock",
            "turn": 0,
            "title": "解兵",
            "options": [{"id": "go", "label": "解兵", "ops": [{"op": "set_flag", "key": "aftermath", "value": True}]}],
        }
    )
    scenario["memorials"].append(
        {"id": "m_aftermath", "conditions": {"kind": "flag", "key": "aftermath"}, "title": "善后", "options": [{"id": "ok", "label": "安抚", "ops": []}]}
    )
    scenario["memorials"].append(
        {"id": "m_grant", "conditions": {"kind": "flag", "key": "granted"}, "title": "饷后", "options": [{"id": "ok", "label": "知道了", "ops": []}]}
    )
    scenario["events"][0]["choices"][0]["effects"].append({"op": "set_flag", "key": "granted", "value": True})
    service = _service_with(scenario)
    session_id, _ = service.create_session("mech_test", 2)
    receipt = _draft(service, session_id, adopted=[{"memorial_id": "m_unlock", "option_id": "go"}])
    confirmed = service.confirm_edict(session_id, receipt["id"])
    assert receipt["clauses"][0]["status"] == "applied"  # 测试场景 base_delay 为 0
    assert confirmed["new_memorials"] == [{"id": "m_aftermath", "title": "善后"}]
    _turn(service, session_id)
    chosen = service.choose(session_id, "choice-petition-1", "grant")
    assert {"id": "m_grant", "title": "饷后"} in chosen["new_memorials"]

    async def run() -> str:
        from mcp.client.client import Client

        async with Client(build_server(_service_with(scenario), mode="client")) as client:
            result = await client.call_tool("new_game", {"scenario": "mech_test", "seed": 4})
            sid = re.search(r"session_id=([0-9a-f]+)", result.content[0].text).group(1)
            draft = await client.call_tool("draft_edict", {"session_id": sid, "adopted": [{"memorial_id": "m_unlock", "option_id": "go"}]})
            edict_id = re.search(r"edict_id=(edict-[0-9a-f]+)\n", draft.content[0].text).group(1)
            done = await client.call_tool("confirm_edict", {"session_id": sid, "edict_id": edict_id})
            return draft.content[0].text + "\n" + done.content[0].text

    text = asyncio.run(run())
    assert "[applied 可行（确认后本季即行）] (memorial)" in text and "[applied 已行] (memorial)" in text and "本季新出奏疏：[m_aftermath] 善后" in text


# ---- 6. 分兵 detach 与调兵摘要 ------------------------------------------------------------------------------


def test_detach_splits_army_and_moves_only_the_detachment() -> None:
    scenario = _scenario()
    state = build_initial_state(scenario)
    op = {"op": "detach", "army": "guard", "strength": 5000, "to_region": "pass", "new_army_name": "禁军前锋", "commander": "eunuch"}
    receipt = AgentTools(state).propose_ops([op], tier=PLAYER_TIER)[0]
    assert receipt["status"] == "applied"
    assert summarize_ops([op], state)[0] == "禁军 分兵 5000，留 15000，编为「禁军前锋」赴 雄关，高内侍统领"
    updated, facts = apply(op, state)
    new_id = next(army_id for army_id in updated.armies if army_id.startswith("guard_d"))
    detachment = updated.armies[new_id]
    assert detachment.name == "禁军前锋" and detachment.strength == 5000 and detachment.faction == "court"
    assert detachment.in_transit["to_region"] == "pass" and updated.armies["guard"].strength == 15000
    assert updated.armies["guard"].in_transit is None and updated.armies["guard"].commander == "general"
    assert updated.characters["eunuch"].in_transit == detachment.in_transit
    assert facts[0].text.startswith("禁军分兵 5000（留 15000），编为禁军前锋，自京畿开拔，赴雄关（约1季）") and "高内侍统领随行" in facts[0].text
    arrived, _ = resolve_turn(updated, __import__("random").Random(0), scenario)
    assert arrived.armies[new_id].region == "pass" and arrived.armies["guard"].region == "capital"
    # 整军调动的摘要写明全军兵力
    assert summarize_ops([{"op": "move_army", "army": "guard", "to_region": "pass"}], state)[0].startswith("禁军（全军 20000） 移驻 雄关")
    # 校验：分兵须少于全军；他方军队不可分
    with pytest.raises(OpRejected) as caught:
        apply({"op": "detach", "army": "guard", "strength": 20000, "to_region": "pass"}, state)
    assert caught.value.code == "INVALID_TARGET" and "move_army" in caught.value.message
    foreign = AgentTools(state).propose_ops([{"op": "detach", "army": "rebel_army", "strength": 100, "to_region": "plain"}], tier=PLAYER_TIER)[0]
    assert foreign["status"] == "rejected" and foreign["code"] == "OP_NOT_PERMITTED"
    # 主帅率偏师而去：原军按任命规则解除其统帅
    led, facts = apply({"op": "detach", "army": "guard", "strength": 3000, "to_region": "pass", "commander": "general"}, state)
    assert led.armies["guard"].commander is None and "其原统禁军不再由其统领" in facts[0].text


# ---- 7. 养兵 upkeep ---------------------------------------------------------------------------------------


def test_upkeep_charges_by_current_strength_and_is_itemised(mech_env) -> None:
    scenario = _scenario()
    scenario["scenario"]["formulas"]["upkeep"] = [{"resource": "treasury", "per": 10000, "amount": 5, "label": "军饷"}]
    assert validate_scenario(scenario) == []
    state = build_initial_state(scenario)
    import random

    _, report = resolve_turn(state, random.Random(0), scenario)
    lines = [line for line in report.ledger_lines if line.kind == "upkeep"]
    assert len(lines) == 1 and lines[0].amount == -15 and lines[0].group == "军饷（兵 30000，每 10000 兵 5）"
    bigger, _ = apply({"op": "adjust_army", "army": "guard", "delta": 10000}, state)
    _, report = resolve_turn(bigger, random.Random(0), scenario)
    assert [line.amount for line in report.ledger_lines if line.kind == "upkeep"] == [-20]
    service = _service_with(scenario)
    session_id, _ = service.create_session("mech_test", 0)
    assert "## 养兵经常开支" in service.briefing(session_id) and "军饷（兵 30000，每 10000 兵 5）：本季约 国库 −15" in service.briefing(session_id)
    result = _turn(service, session_id)
    treasury = next(row for row in result["report"]["reconciliation"] if row["resource"] == "treasury")
    assert "军饷（兵 30000，每 10000 兵 5） −15" in treasury["regular_display"]
    broken = copy.deepcopy(scenario)
    broken["scenario"]["formulas"]["upkeep"] = [{"resource": "gold", "per": 0, "amount": "x", "armies": ["nope"]}]
    errors = validate_scenario(broken)
    assert any("资源不存在：gold" in line for line in errors) and any(".per 须为正数" in line for line in errors)
    assert any("军队不存在：nope" in line for line in errors)


# ---- 8. 终局复盘要事 ---------------------------------------------------------------------------------------


def test_final_review_keeps_decisive_events(mech_env) -> None:
    scenario = _scenario()
    scenario["scenario"]["endings"].insert(
        0,
        {
            "id": "subdued",
            "kind": "ending",
            "title": "旧部归心",
            "priority": 0,
            "conditions": {
                "all": [
                    {"kind": "turn", "operator": "gte", "value": 3},
                    {"kind": "flag", "key": "loyal_again"},
                    {"kind": "resource", "id": "corruption", "operator": "lte", "value": 30},
                ]
            },
        },
    )
    scenario["events"].append(
        {
            "id": "loyal",
            "title": "旧部归心",
            "when": {"turn": 2},
            "gravity": 1,
            "importance": 0,
            "effects": [{"op": "set_flag", "key": "loyal_again", "value": True}],
        }
    )
    scenario["events"].append(
        {
            "id": "purge",
            "title": "大计黜陟",
            "when": {"turn": 3},
            "gravity": 1,
            "importance": 0,
            "effects": [{"op": "adjust_resource", "resource": "corruption", "delta": -20}],
        }
    )
    for index in range(30):  # 一堆琐事：不能把决定胜局的要事挤掉
        scenario["events"].append(
            {"id": f"noise{index}", "title": f"琐事{index}", "when": {"turn": 1 + index % 2}, "gravity": 1, "importance": 1, "text": "琐事"}
        )
    service = _service_with(scenario)
    session_id, _ = service.create_session("mech_test", 5)
    receipt = _draft(service, session_id, ops=[{"op": "set_army_commander", "army": "pass_army", "character": "eunuch"}])
    service.confirm_edict(session_id, receipt["id"])
    _turn(service, session_id, discard_drafts=True)
    service.choose(session_id, "choice-petition-1", "deny")
    _turn(service, session_id)
    result = _turn(service, session_id)
    assert result["outcome"]["id"] == "subdued"
    review = service.view(session_id)["review"]
    text = "\n".join(review["highlights"])
    assert "旧部归心" in text and "大计黜陟——积弊 ≤ 30（结局条件由此成立）" in text
    assert "抉择「边将请饷」→ 驳回" in text and "终局「旧部归心」" in text
    assert receipt["clauses"][0]["status"] == "applied"
    assert "关军由高内侍统领" in text and "高内侍抵达雄关，到军统关军" in text
    assert review["omitted"] > 0 and len(review["highlights"]) <= GameService.REVIEW_LIMIT
    assert all("（第" in line.split("：")[0] and not re.search(r"T\d", line) for line in review["highlights"])
    assert "另有" in service.briefing(session_id)


# ---- 9. 人设头衔随官职 ------------------------------------------------------------------------------------


def test_persona_title_and_greeting_follow_current_offices() -> None:
    scenario = _scenario()
    general = next(row for row in scenario["characters"] if row["id"] == "general")
    general["title"] = "安西节度判官"
    general["card"] = {"greeting": "臣某，安西判官。陛下要问什么，臣有账可查。"}
    state = build_initial_state(scenario)
    card = persona_card(state, "general", scenario)
    assert card["title"] == "安西节度判官" and "former_title" not in card and card["greeting"].startswith("臣某，安西判官。")
    appointed, _ = apply({"op": "appoint", "character": "general", "office": "pass_jds"}, state)
    card = persona_card(appointed, "general", scenario)
    assert card["title"] == "关镇节度" and card["former_title"] == "安西节度判官"
    assert card["greeting"] == "臣某，关镇节度。陛下要问什么，臣有账可查。"
    assert refresh_greeting("臣某，安西判官。", appointed, "general", scenario) == "臣某，关镇节度。"
    assert "【本人】郭将军（旧衔：安西节度判官）；关镇节度" in render_digest(appointed, 0, "general", None, scenario)


# ---- 10. export_log 记 client 模式召对 / 廷议；11. 延期标签 ----------------------------------------------------------


def test_export_log_records_client_audience_and_debate(mech_env) -> None:
    from mcp.client.client import Client

    async def run() -> dict:
        service = GameService()
        session_id, _ = service.create_session("mech_test", 3)
        async with Client(build_server(service, mode="client")) as client:
            await client.call_tool("audience", {"session_id": session_id, "character": "chancellor"})
            await client.call_tool("court_debate", {"session_id": session_id, "participants": ["chancellor", "eunuch"], "memorial_id": "m_tax"})
        return service.export_log(session_id)

    exported = asyncio.run(run())
    season = exported["seasons"][0]
    assert season["audiences"] == [{"character": "chancellor", "name": "李相", "mode": "in_person", "mode_label": "面见", "via": "client"}]
    debate = season["debates"][0]
    assert debate["memorial_id"] == "m_tax" and debate["participants"] == ["chancellor", "eunuch"] and debate["via"] == "client"
    assert "speeches" not in debate and "text" not in debate


def test_moves_arriving_after_the_time_limit_are_flagged(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 1)
    session = service.session(session_id)
    for _ in range(6):  # 推到 T6；时限 T8
        _turn(service, session_id, discard_drafts=True)
    receipt = _draft(
        service, session_id, ops=[{"op": "move_army", "army": "guard", "to_region": "north"}, {"op": "move_army", "army": "pass_army", "to_region": "capital"}]
    )
    assert "（预计至德二年夏（第9回合）抵达，晚于终局时限 至德二年春（第8回合），本局内到不了）" in receipt["clauses"][0]["summary"]
    assert "晚于终局时限" not in receipt["clauses"][1]["summary"]
    assert session.head_state().turn == 6


def test_scenario_initial_ledger_exists_from_turn_zero(mech_env) -> None:
    scenario = _scenario()
    scenario["scenario"]["ledger"] = [
        {"name": "宫廷用度", "category": "treasury", "amount": -45, "reason": "内廷"},
        {"id": "rice", "name": "京师禄米", "category": "grain", "amount": -6, "cancellable": True},
    ]
    assert validate_scenario(scenario) == []
    state = build_initial_state(scenario)
    assert [(row.name, row.amount, row.cancellable) for row in state.ledger] == [("宫廷用度", -45, False), ("京师禄米", -6, True)]
    assert state.ledger[1].id == "rice" and state.ledger[0].id.startswith("ledger-")
    service = _service_with(scenario)
    session_id, _ = service.create_session("mech_test", 0)
    assert len(service.op_reference(session_id, entity_kind="ledger")["rows"]) == 2
    result = _turn(service, session_id)
    treasury = next(row for row in result["report"]["reconciliation"] if row["resource"] == "treasury")
    assert "宫廷用度 −45" in treasury["regular_display"]
    broken = copy.deepcopy(scenario)
    broken["scenario"]["ledger"] = [{"name": "x", "category": "gold", "amount": "a", "kind": "weekly", "oops": 1}]
    errors = validate_scenario(broken)
    for text in ("资源不存在：gold", ".amount 须为数值", ".kind 须为", "含未知键：oops"):
        assert any(text in line for line in errors), text


def test_appoint_with_army_office_dispatch_is_one_line_and_mentions_every_army_still_held() -> None:
    state = build_initial_state(_scenario())
    # 郭将军：统禁军（同驻京畿），另遥领关军（任关镇节度，人不在雄关）
    state, _ = apply({"op": "appoint", "character": "general", "office": "pass_jds"}, state)
    assert state.armies["pass_army"].commander == "general" and state.armies["guard"].commander == "general"
    state, facts = apply({"op": "appoint", "character": "general", "office": "marshal"}, state)
    scenario = _scenario()
    scenario["scenario"]["offices"].append({"id": "north_jds", "name": "北镇节度", "army": "rebel_army"})
    moved_state = build_initial_state(scenario)
    moved_state, _ = apply({"op": "appoint", "character": "general", "office": "pass_jds"}, moved_state)
    moved_state.armies["rebel_army"].faction = "court"
    moved, facts = apply({"op": "set_army_commander", "army": "rebel_army", "character": "general", "dispatch": True}, moved_state)
    text = facts[0].text
    assert "已启程赴任" in text and "其原统禁军不再由其统领" in text and "仍兼领关军（不在军中，副将暂摄）" in text
    assert moved.armies["guard"].commander is None and moved.armies["pass_army"].commander == "general"
    one = summarize_ops([{"op": "appoint", "character": "eunuch", "office": "pass_jds", "dispatch": True}], build_initial_state(_scenario()))
    assert len(one) == 1 and "兼统关军；已启程赴任" in one[0]


def test_draft_summaries_follow_earlier_clauses_and_unchanged_commander_is_quiet(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 1)
    receipt = _draft(
        service,
        session_id,
        ops=[{"op": "detach", "army": "guard", "strength": 5000, "to_region": "pass"}, {"op": "move_army", "army": "guard", "to_region": "pass"}],
    )
    assert receipt["clauses"][1]["summary"].startswith("禁军（全军 15000） 移驻 雄关")
    state = build_initial_state(_scenario())
    state, _ = apply({"op": "appoint", "character": "general", "office": "pass_jds"}, state)
    _, facts = apply({"op": "set_army_commander", "army": "pass_army", "character": "general"}, state)
    assert facts[0].internal and facts[0].text.startswith("关军仍由郭将军统领")
