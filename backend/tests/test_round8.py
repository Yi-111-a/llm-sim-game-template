"""第 8 轮修复（gpt-08 / gpt-09 报告）：MCP 冷启动、敌方人物不可隔空拘拿、在途军队、地区指标范围、战报因素、
兼领预览只说实际变化。"""

import asyncio
import io
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

from conftest import FIXTURE_SCENARIOS
from mcp import Client

from simcore.mcp.meta import DESCRIPTIONS, TOOL_PARAMS, input_schema, tool_list
from simcore.mcp.server import build_server
from simcore.mcp.stdio import Engine, check_arguments, handle, serve
from simcore.service import GameService

BACKEND = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 1. MCP 冷启动


def test_static_tool_schemas_match_the_sdk_generated_ones(mech_env) -> None:
    async def run() -> list:
        async with Client(build_server(GameService())) as client:
            return (await client.list_tools()).tools

    tools = asyncio.run(run())
    assert [tool.name for tool in tools] == list(TOOL_PARAMS) == [row["name"] for row in tool_list()]
    for tool in tools:
        assert tool.input_schema == input_schema(tool.name), tool.name
        assert tool.description == DESCRIPTIONS[tool.name]


def test_cli_mcp_entry_does_not_import_heavy_modules() -> None:
    probe = (
        "import sys, cli.main, simcore.mcp.stdio;"
        "heavy = [m for m in ('mcp', 'httpx', 'uvicorn', 'textual', 'jinja2', 'fastapi', 'simcore.service', 'simcore.scenario.loader')"
        " if m in sys.modules];"
        "print(heavy)"
    )
    out = subprocess.run([sys.executable, "-c", probe], cwd=BACKEND, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_stdio_answers_initialize_and_tools_list_before_loading_the_engine(mech_env) -> None:
    engine = Engine()
    engine.warm_up = lambda: None  # type: ignore[method-assign]
    init = handle(engine, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["serverInfo"]["name"] == "llm-sim" and init["result"]["capabilities"]["tools"] == {"listChanged": False}
    assert handle(engine, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    listed = handle(engine, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert len(listed["result"]["tools"]) == len(TOOL_PARAMS)
    assert engine._tools is None  # 还没导入服务层
    unknown = handle(engine, {"jsonrpc": "2.0", "id": 3, "method": "nope"})
    assert unknown["error"]["code"] == -32601
    assert handle(engine, {"jsonrpc": "2.0", "id": 4, "method": "ping"})["result"] == {}


def test_stdio_tool_calls_return_engine_text_and_readable_errors(mech_env) -> None:
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "new_game", "arguments": {"scenario": "mech_test", "seed": "3"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "briefing", "arguments": {"session_id": "nope"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "briefing", "arguments": {}}},
    ]
    reader = io.BytesIO(b"".join(json.dumps(row, ensure_ascii=False).encode("utf-8") + b"\n" for row in lines) + b"not json\n")
    writer = io.BytesIO()
    serve(Engine(), reader, writer)
    replies = [json.loads(row) for row in writer.getvalue().decode("utf-8").splitlines()]
    assert [row.get("id") for row in replies] == [1, 2, 3, 4, None]
    assert replies[0]["result"]["protocolVersion"] == "2024-11-05"
    new_game = replies[1]["result"]
    assert not new_game["isError"] and "session_id=" in new_game["content"][0]["text"] and "机制测试" in new_game["content"][0]["text"]
    missing = replies[2]["result"]
    assert missing["isError"] and missing["content"][0]["text"].startswith("错误 [SESSION_NOT_FOUND]")
    assert replies[3]["result"]["isError"] and "缺少必填参数 session_id" in replies[3]["result"]["content"][0]["text"]
    assert replies[4]["error"]["code"] == -32700


def test_check_arguments_types() -> None:
    assert check_arguments("export_log", {"session_id": "s", "from_turn": 2.0, "to_turn": None}) == (
        {"session_id": "s", "from_turn": 2, "to_turn": None},
        None,
    )
    assert check_arguments("end_turn", {"session_id": "s", "discard_drafts": "yes"})[1] == "参数 discard_drafts 应为布尔值"
    assert check_arguments("court_debate", {"session_id": "s", "participants": "a"})[1] == "参数 participants 应为数组"


def test_real_process_answers_initialize_fast(mech_env) -> None:
    env = {**os.environ, "SIM_SCENARIOS_DIR": str(FIXTURE_SCENARIOS), "LLM_SIM_HOME": str(mech_env), "PYTHONIOENCODING": "utf-8"}
    started = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, "-m", "cli.main", "mcp"], cwd=BACKEND, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}}}
        proc.stdin.write((json.dumps(request) + "\n").encode())
        proc.stdin.flush()
        reply = json.loads(proc.stdout.readline())
        elapsed = time.perf_counter() - started
        assert reply["result"]["serverInfo"]["name"] == "llm-sim"
        # 宽松上限（CI / 杀毒扫描）；改前在本机要 6–80 秒
        assert elapsed < 20, elapsed
    finally:
        proc.stdin.close()
        proc.wait(timeout=60)


def test_integration_configs_carry_a_60s_startup_timeout(mech_env) -> None:
    from fastapi.testclient import TestClient

    from api.app import create_app

    info = TestClient(create_app()).get("/api/integration").json()
    assert "startup_timeout_sec = 60" in info["codex"]["toml"]
    assert any(flag.endswith(".startup_timeout_sec=60") for flag in info["codex"]["exec_flags"])
    agent_play = (BACKEND.parents[1] / "tools" / "agent_play.py").read_text(encoding="utf-8")
    assert "startup_timeout_sec=60" in agent_play
    for script in ("setup_mcp.py", "setup_mcp.ps1"):
        text = (BACKEND.parents[1] / "integrations" / script).read_text(encoding="utf-8")
        assert "'60'" in text or '"60"' in text, script


# ---------------------------------------------------------------- 2. 敌方人物不能隔空拘拿


def _mech_state(mech_scenario):
    from simcore.scenario.loader import build_initial_state

    return build_initial_state(mech_scenario)


def _status_code(state, character: str, status: str = "imprisoned"):
    from simcore.domain.permissions import check_player_op

    result = check_player_op({"op": "set_character_status", "character": character, "status": status, "cause": "按问"}, state)
    return (result.code, result.message) if result is not None else (None, "")


def test_set_character_faction_is_a_scenario_op(mech_scenario) -> None:
    from simcore.domain.apply import apply
    from simcore.domain.permissions import check_player_op
    from simcore.domain.rules import Rejected, validate

    state = _mech_state(mech_scenario)
    op = {"op": "set_character_faction", "character": "plotter", "faction": "rebels"}
    denied = check_player_op(op, state)
    assert denied is not None and denied.code == "OP_NOT_PERMITTED"
    bad = validate({"op": "set_character_faction", "character": "plotter", "faction": "ghost"}, state)
    assert isinstance(bad, Rejected) and bad.code == "UNKNOWN_ENTITY"
    updated, facts = apply(op, state)
    assert updated.characters["plotter"].faction == "rebels"
    assert facts[0].kind == "character_faction" and facts[0].text == "安节度改属叛军"


def test_hostile_faction_or_hostile_command_blocks_any_player_disposal(mech_scenario) -> None:
    from simcore.domain.apply import apply

    state = _mech_state(mech_scenario)
    assert _status_code(state, "chancellor") == (None, "")  # 本朝臣属、在京：可拘
    defected, _ = apply({"op": "set_character_faction", "character": "chancellor", "faction": "rebels"}, state)
    for status in ("imprisoned", "exiled", "retired"):
        code, message = _status_code(defected, "chancellor", status)
        assert code == "HOSTILE_TARGET" and "敌对势力叛军" in message
    # 名义上仍是本朝人物，却统领敌军：同样不能隔空处置
    state.characters["eunuch"].region = "capital"
    state.armies["rebel_army"].commander = "eunuch"
    code, message = _status_code(state, "eunuch", "exiled")
    assert code == "HOSTILE_TARGET" and "统领敌军叛军主力" in message


def test_arrest_needs_own_land_and_no_hostile_escort(mech_scenario) -> None:
    state = _mech_state(mech_scenario)
    # 安节度在叛军控制的北地：可致仕、不可拘拿
    code, message = _status_code(state, "plotter")
    assert code == "HOSTILE_TARGET" and "不在本朝控制之地" in message
    assert _status_code(state, "plotter", "retired")[0] is None
    # 京畿有敌军（不在途）：视为护卫，不能拘拿
    state.regions["capital"].controller = "court"
    state.armies["rebel_army"].region = "capital"
    code, message = _status_code(state, "chancellor")
    assert code == "HOSTILE_TARGET" and "护卫" in message and "叛军主力" in message
    state.armies["rebel_army"].in_transit = {"to_region": "plain", "eta_turn": 2}
    assert _status_code(state, "chancellor")[0] is None


def test_delayed_arrest_is_rechecked_at_settlement(mech_scenario) -> None:
    from simcore.domain.models import PendingClause
    from simcore.domain.turn import resolve_turn

    state = _mech_state(mech_scenario)
    state.characters["chancellor"].faction = "rebels"
    state.pending_clauses.append(
        PendingClause(id="p1", ops=[{"op": "set_character_status", "character": "chancellor", "status": "imprisoned"}], due_turn=1)
    )
    updated, report = resolve_turn(state, random.Random(1), mech_scenario)
    assert updated.characters["chancellor"].status == "alive"
    assert any("延期条款未能执行" in line and "敌对势力" in line for line in report.warnings)


def test_op_reference_explains_hostile_target(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 1)
    note = service.op_reference(session_id, "set_character_status", None, None)["note"]
    assert "HOSTILE_TARGET" in note and "护卫" in note


# ---------------------------------------------------------------- 3. 在途军队不写成驻目的地


def test_in_transit_armies_are_not_shown_at_their_destination(mech_env) -> None:
    from simcore.domain.command import command_table, command_table_lines

    service = GameService()
    session_id, _ = service.create_session("mech_test", 1)
    session = service.session(session_id)
    results = session.append_ops([
        {"op": "detach", "army": "guard", "strength": 5000, "to_region": "pass", "new_army_name": "禁军前锋"},
        {"op": "move_army", "army": "pass_army", "to_region": "plain"},
    ])
    assert all(row["ok"] for row in results), results
    state = session.head_state()
    table = command_table(state, "ruler")
    vanguard = next(row for row in table["vacancies"]["armies"] if row["name"] == "禁军前锋")
    assert vanguard["region"] == "capital" and vanguard["whereabouts"] == "在途：京畿→雄关，约1季后到"
    lines = "\n".join(command_table_lines(table))
    assert "禁军前锋5000（在途：京畿→雄关，约1季后到）" in lines and "（驻雄关）" not in lines
    assert "关军10000（在途：雄关→平原，约1季后到）" in lines
    assert any("禁军前锋（在途：京畿→雄关" in alert for alert in table["alerts"])
    briefing = service.briefing(session_id)
    assert "禁军前锋5000（在途：京畿→雄关，约1季后到）" in briefing and "禁军前锋5000（驻雄关）" not in briefing
    # 抵达后才写「驻」
    asyncio.run(service.end_turn(session_id, narrate=False))
    table = command_table(service.session(session_id).head_state(), "ruler")
    vanguard = next(row for row in table["vacancies"]["armies"] if row["name"] == "禁军前锋")
    assert vanguard["whereabouts"] == "驻雄关" and vanguard["in_transit"] is None


# ---------------------------------------------------------------- 4. 地区指标量程


def test_region_stats_are_clamped_to_scenario_ranges(mech_scenario) -> None:
    import copy

    from simcore.domain.apply import apply
    from simcore.scenario.loader import build_initial_state, validate_scenario

    state = _mech_state(mech_scenario)
    assert state.rules["stat_ranges"]["defense"] == [0.0, 100.0]
    updated, facts = apply({"op": "adjust_region", "region": "pass", "stat": "defense", "delta": 30}, state)
    assert updated.regions["pass"].stats["defense"] == 100
    assert facts[0].text.startswith("雄关防务 +15（已至上限 100，原拟 +30）") and facts[0].after == 100
    again, facts = apply({"op": "adjust_region", "region": "pass", "stat": "defense", "delta": 5}, updated)
    assert again.regions["pass"].stats["defense"] == 100 and "+0（已至上限 100" in facts[0].text
    low, facts = apply({"op": "adjust_region", "region": "plain", "stat": "defense", "delta": -50}, state)
    assert low.regions["plain"].stats["defense"] == 0 and "已至下限 0" in facts[0].text
    # 开局值超范围的截入范围；range 写错的剧本校验报错
    scenario = copy.deepcopy(mech_scenario)
    scenario["regions"][0]["stats"]["prosperity"] = 130
    assert build_initial_state(scenario).regions[scenario["regions"][0]["id"]].stats["prosperity"] == 100
    scenario["scenario"]["stats"][0]["range"] = [100, 0]
    assert any("range" in error for error in validate_scenario(scenario))


def test_briefing_legend_lists_stat_ranges(mech_env) -> None:
    service = GameService()
    session_id, _ = service.create_session("mech_test", 1)
    assert "地区指标量程：繁荣 0–100、防务 0–100、税基 0–100" in service.briefing(session_id)


# ---------------------------------------------------------------- 5. 战报因素


def test_battle_report_itemises_factors_losses_and_rout(mech_scenario) -> None:
    from simcore.domain.battle import resolve_battles
    from simcore.domain.visibility import battle_breakdown
    from simcore.mcp.server import _turn_text

    state = _mech_state(mech_scenario)
    # 北镇（叛军所据）：禁军 20000（郭将军在军）攻 叛军主力 3000（无主帅），叛军无路可退
    state.armies["guard"].region = "north"
    state.characters["general"].region = "north"
    state.armies["rebel_army"].strength = 3000
    battles, _ = resolve_battles(state, random.Random(3), mech_scenario.get("scenario", {}).get("wars"))
    battle = battles[0]
    assert battle["result"] == "attacker_won" and battle["routed"]["rebel_army"] == 2100
    detail = battle_breakdown(battle)
    assert "攻：禁军20000（主帅郭将军在军）" in detail
    assert "守：叛军主力3000（无主帅），城防+30%" in detail
    assert "战力=兵×素质×(1+城防%)×天时±15%" in detail
    assert "伤亡（胜方各折1成、败方各折3成）：禁军−2000；叛军主力−900，无路可退，余2100溃散" in detail
    text = _turn_text({"report": {"turn": 1, "battles": battles}, "calendar_label": "x", "state": {"resources": {}, "armies": {}}})
    assert "## 战报" in text and "天时±15%" in text and "余2100溃散" in text
    # 旧存档的战报没有 factors：照旧只列损失
    assert battle_breakdown({k: v for k, v in battle.items() if k != "factors"}) == ""


# ---------------------------------------------------------------- 6. 兼领预览只说实际变化


def test_remote_appointment_preview_does_not_claim_the_present_army_lost_its_commander(mech_scenario) -> None:
    import copy

    from simcore.domain.apply import apply
    from simcore.domain.memorials import summarize_ops
    from simcore.scenario.loader import build_initial_state

    scenario = copy.deepcopy(mech_scenario)
    scenario["scenario"]["offices"].append({"id": "pass_jds", "name": "关镇节度", "army": "pass_army"})
    state = build_initial_state(scenario)
    # 郭将军在京畿统禁军；遥领关镇节度（关军驻雄关）：禁军照旧在军，不能说成「不在军中」
    op = {"op": "appoint", "character": "general", "office": "pass_jds"}
    preview = summarize_ops([op], state)[0]
    assert "遥领，不赴任（今在京畿），由副将暂摄" in preview and "禁军" not in preview
    updated, facts = apply(op, state)
    assert "禁军" not in facts[0].text and updated.armies["guard"].commander == "general"
    # 真正遥领在外的军队照旧列出：再令其遥领北镇之军时，关军确不在身边
    scenario["scenario"]["offices"].append({"id": "north_jds", "name": "北镇节度", "army": "rebel_army"})
    state = build_initial_state(scenario)
    state.armies["rebel_army"].faction = "court"
    state, _ = apply(op, state)
    text = summarize_ops([{"op": "appoint", "character": "general", "office": "north_jds"}], state)[0]
    assert "仍兼领关军（不在军中，副将暂摄）" in text and "禁军" not in text
