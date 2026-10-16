"""§3 MCP 服务：进程内客户端调用全部工具 + 一次真实 stdio 往返。"""

import asyncio
import os
import re
import sys
from pathlib import Path

from conftest import FIXTURE_SCENARIOS
from mcp import Client, StdioServerParameters

from simcore.mcp.server import DESCRIPTIONS, build_server
from simcore.service import GameService

EXPECTED_TOOLS = {
    "list_scenarios",
    "policy_cards",
    "export_log",
    "new_game",
    "list_saves",
    "load_game",
    "briefing",
    "op_reference",
    "audience",
    "court_debate",
    "save_advice",
    "draft_edict",
    "confirm_edict",
    "choose",
    "end_turn",
    "history",
    "rewind",
}


def text_of(result) -> str:
    return "\n".join(block.text for block in result.content if getattr(block, "text", None))


async def call(client: Client, name: str, **arguments) -> tuple[bool, str]:
    result = await client.call_tool(name, arguments)
    return bool(result.is_error), text_of(result)


def session_from(text: str) -> str:
    match = re.search(r"session_id=([0-9a-f]{32})", text)
    assert match, text[:200]
    return match.group(1)


def test_tool_list_and_bilingual_descriptions(mech_env) -> None:
    async def run() -> None:
        async with Client(build_server(GameService())) as client:
            tools = (await client.list_tools()).tools
            assert {tool.name for tool in tools} == EXPECTED_TOOLS
            for tool in tools:
                assert re.search(r"[一-鿿]", tool.description) and re.search(r"[A-Za-z]{4,}", tool.description), tool.name

    asyncio.run(run())
    assert set(DESCRIPTIONS) == EXPECTED_TOOLS


def test_client_mode_full_play_loop_without_leaking_hidden_values(mech_env) -> None:
    async def run() -> None:
        service = GameService()
        async with Client(build_server(service, mode="client")) as client:
            error, text = await call(client, "list_scenarios")
            assert not error and "mech_test" in text and "机制测试" in text
            error, text = await call(client, "new_game", scenario="mech_test", seed=5)
            assert not error
            session_id = session_from(text)
            assert "天宝十载春" in text and "扮演与数字规则" in text
            assert "[m_tax] 请减赋" in text and "`approve`「准减赋」" in text and "国库 −50" in text
            true_ambition = service.session(session_id).state.resources["ambition"].amount
            assert "异志 约" in text and f"异志 {true_ambition:g}" not in text
            briefing = (await call(client, "briefing", session_id=session_id))[1]
            assert briefing.strip().endswith("不能直接变成操作。")
            error, reference = await call(client, "op_reference", session_id=session_id)
            assert not error and '"choose_event_option"' in reference and "pass_army" in reference and '"permitted_ops"' in reference
            error, card = await call(client, "audience", session_id=session_id, character="plotter")
            assert not error and "人设卡：安节度" in card and "StateDigest" in card and "口吻要求" in card
            assert '"ambition": 7' not in card
            error, debate = await call(client, "court_debate", session_id=session_id, topic="边镇增兵议", participants=["chancellor", "general"])
            assert not error and "赞成（support）" in debate and "反对（oppose）" in debate
            error, saved = await call(client, "save_advice", session_id=session_id, character="general", text="宜守雄关")
            assert not error and "已存入谏言库" in saved
            error, draft = await call(
                client,
                "draft_edict",
                session_id=session_id,
                sections={"interior": "减赋以安民"},
                adopted=[{"memorial_id": "m_tax", "option_id": "approve"}],
                ops=[{"op": "move_army", "army": "guard", "to_region": "north"}, {"op": "move_army", "army": "pass_army", "to_region": "pass"}],
            )
            assert not error
            assert "[applied 可行（确认后本季即行）] (memorial) 国库 −50" in draft
            assert "[applied 可行（确认后本季即行）] (client) 禁军（全军 20000） 移驻 北镇（经雄关、平原，约3季）" in draft  # 非相邻目标：整段寻路
            assert "[rejected 被拒] [INVALID_TARGET] (client)" in draft
            assert "narrative_only" in draft
            edict_id = re.search(r"edict_id=(edict-[0-9a-f]+)", draft).group(1)
            error, confirmed = await call(client, "confirm_edict", session_id=session_id, edict_id=edict_id)
            assert not error and "m_tax→approve" in confirmed
            error, report = await call(client, "end_turn", session_id=session_id)
            assert not error and "天宝十载夏" in report and "choice-petition-1" in report and "危机计" in report
            assert "异志：数值不明" in report
            error, chosen = await call(client, "choose", session_id=session_id, choice_id="choice-petition-1", option_id="grant")
            assert not error and "已处置" in chosen
            error, history = await call(client, "history", session_id=session_id)
            assert not error and "←当前" in history
            first_entry = history.splitlines()[1].split(" | ")[0]
            error, rewound = await call(client, "rewind", session_id=session_id, entry_id=first_entry)
            assert not error and "另开分支" in rewound
            error, saves = await call(client, "list_saves")
            assert session_id in saves
            error, loaded = await call(client, "load_game", session_id=session_id)
            assert not error and session_id in loaded
            full_state = service.session(session_id).state.model_dump_json()
            assert '"amount"' in full_state  # 服务端完整状态存在，但从未经工具外泄

    asyncio.run(run())


def test_tool_errors_are_readable_chinese_with_codes(mech_env) -> None:
    async def run() -> None:
        async with Client(build_server(GameService())) as client:
            error, text = await call(client, "briefing", session_id="nope")
            assert error and "错误 [SESSION_NOT_FOUND]" in text and "Traceback" not in text
            error, text = await call(client, "new_game", scenario="ghost", seed=1)
            assert error and "SCENARIO_NOT_FOUND" in text
            session_id = session_from((await call(client, "new_game", scenario="mech_test", seed=1))[1])
            error, text = await call(client, "choose", session_id=session_id, choice_id="ghost", option_id="x")
            assert error and "错误 [UNKNOWN_ENTITY]" in text
            error, text = await call(client, "confirm_edict", session_id=session_id, edict_id="edict-none")
            assert error and "EDICT_NOT_FOUND" in text
            error, text = await call(client, "draft_edict", session_id=session_id, ops=[{"op": "bogus"}])
            assert not error and "未知操作：bogus" in text

    asyncio.run(run())


def test_audience_refuses_dead_or_imprisoned(mech_env) -> None:
    async def run() -> None:
        service = GameService()
        async with Client(build_server(service)) as client:
            session_id = session_from((await call(client, "new_game", scenario="mech_test", seed=1))[1])
            service.session(session_id).append_op({"op": "set_character_status", "character": "plotter", "status": "imprisoned"})
            error, text = await call(client, "audience", session_id=session_id, character="plotter")
            assert error and "CHARACTER_UNAVAILABLE" in text and "系狱" in text
            error, text = await call(client, "audience", session_id=session_id, character="ghost")
            assert error and "UNKNOWN_CHARACTER" in text

    asyncio.run(run())


def test_server_mode_uses_configured_provider(mech_env) -> None:
    async def run() -> None:
        async with Client(build_server(GameService(), mode="server")) as client:
            session_id = session_from((await call(client, "new_game", scenario="mech_test", seed=1))[1])
            error, reply = await call(client, "audience", session_id=session_id, character="chancellor", question="国用如何？")
            assert not error and reply.startswith("李相：")
            error, debate = await call(client, "court_debate", session_id=session_id, topic="边镇增兵议", participants=["chancellor", "general"])
            assert not error and "李相（support）：" in debate and "郭将军（oppose）：" in debate
            error, draft = await call(client, "draft_edict", session_id=session_id, sections={"interior": "国库增加 10"})
            assert not error and "[narrative_only 仅记录] (text) 国库增加 10；原因：未接模型" in draft
            error, report = await call(client, "end_turn", session_id=session_id)
            assert not error and "## 纪事" in report

    asyncio.run(run())


def test_real_stdio_round_trip(mech_env) -> None:
    backend = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "SIM_SCENARIOS_DIR": str(FIXTURE_SCENARIOS),
        "LLM_SIM_HOME": str(mech_env),
        "PYTHONIOENCODING": "utf-8",
    }

    async def run() -> None:
        params = StdioServerParameters(command=sys.executable, args=["-m", "cli.main", "mcp"], env=env, cwd=str(backend))
        async with Client(params, read_timeout_seconds=60) as client:
            tools = (await client.list_tools()).tools
            assert {tool.name for tool in tools} == EXPECTED_TOOLS
            error, text = await call(client, "new_game", scenario="mech_test", seed=9)
            assert not error and "机制测试" in text and "天宝十载春" in text
            session_id = session_from(text)
            error, report = await call(client, "end_turn", session_id=session_id)
            assert not error and "天宝十载夏" in report
        assert (mech_env / "sessions" / f"{session_id}.jsonl").is_file()

    asyncio.run(run())
