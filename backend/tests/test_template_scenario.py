"""scenarios/_template：校验与内容 lint 通过，机制示范可用，并能经 MCP 以「每项取首选」打到终局。"""

from __future__ import annotations

import asyncio
import importlib.util
import re
from pathlib import Path

from mcp import Client

from simcore.mcp.server import build_server
from simcore.scenario.loader import load_scenario, validate_scenario
from simcore.service import GameService

ENGINE = Path(__file__).resolve().parents[2]
SCENARIOS = ENGINE / "scenarios"
TEMPLATE_ENDINGS = {"border_secured", "uneasy_peace", "fail_capital", "fail_bankrupt"}


def text_of(result) -> str:
    return "\n".join(block.text for block in result.content if getattr(block, "text", None))


def _env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SIM_SCENARIOS_DIR", str(SCENARIOS))
    monkeypatch.setenv("LLM_SIM_HOME", str(tmp_path))
    monkeypatch.delenv("SIM_DEBUG", raising=False)
    monkeypatch.delenv("SIM_MCP_MODE", raising=False)


def test_template_validates_and_passes_content_lint() -> None:
    assert validate_scenario(SCENARIOS / "_template") == []
    spec = importlib.util.spec_from_file_location("content_lint", ENGINE / "scripts" / "content_lint.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    linter = module.Linter(module.load_pack(SCENARIOS / "_template"))
    linter.run()
    assert linter.errors == [] and linter.warnings == []


def test_template_demonstrates_round9_features(monkeypatch, tmp_path) -> None:
    _env(monkeypatch, tmp_path)
    scenario = load_scenario(SCENARIOS / "_template")
    assert {row["id"] for row in scenario["scenario"]["difficulties"]} == {"easy", "normal", "hard"}
    service = GameService()
    session_id, _ = service.create_session("_template", 1)
    view = service.view(session_id)
    assert [row["step"] for row in view["tutorial"]["steps"]] == [1, 2, 3]
    assert view["agenda"]["text"] == "召对 0/2、廷议 0/1"
    assert view["crisis"] and {row["id"] for row in view["memorials"]} == {"m_frontier_pay", "m_scouts", "m_censor_vacant"}
    pay = next(row for row in view["memorials"] if row["id"] == "m_frontier_pay")
    assert pay["arrival"] and "北境" in pay["arrival"]
    assert view["state"]["resources"]["raider_will"].get("estimate")  # hidden value: range only
    cards = {row["id"]: row for row in service.policy_cards(session_id)}
    assert set(cards) == {"card_march", "card_hold", "card_appoint", "card_fortify"} and cards["card_appoint"]["available"]
    present = {row["character"] for row in service.present(session_id)["present"]}
    assert "minister" in present and "general" not in present  # 北境总兵不在京，不能当廷议


def test_template_plays_to_an_outcome_via_mcp_with_first_options(monkeypatch, tmp_path) -> None:
    _env(monkeypatch, tmp_path)

    async def run() -> None:
        service = GameService()
        async with Client(build_server(service, mode="client")) as client:
            result = await client.call_tool("new_game", {"scenario": "_template", "seed": 3})
            assert not result.is_error
            session_id = re.search(r"session_id=([0-9a-f]{32})", text_of(result)).group(1)
            for _ in range(15):
                view = service.view(session_id)
                if view["outcome"]:
                    break
                adopted = [{"memorial_id": row["id"], "option_id": row["options"][0]["id"]} for row in view["memorials"]]
                if adopted:
                    draft = await client.call_tool("draft_edict", {"session_id": session_id, "adopted": adopted})
                    assert not draft.is_error, text_of(draft)
                    edict_id = re.search(r"edict_id=(edict-[0-9a-f]+)", text_of(draft)).group(1)
                    confirm = await client.call_tool("confirm_edict", {"session_id": session_id, "edict_id": edict_id})
                    assert not confirm.is_error, text_of(confirm)
                for choice in service.view(session_id)["pending_choices"]:
                    for option in choice["options"]:  # first option whose conditions hold
                        picked = await client.call_tool("choose", {"session_id": session_id, "choice_id": choice["id"], "option_id": option["id"]})
                        if not picked.is_error:
                            break
                ended = await client.call_tool("end_turn", {"session_id": session_id})
                assert not ended.is_error, text_of(ended)
            final = service.view(session_id)
            assert final["outcome"] is not None and final["outcome"]["id"] in TEMPLATE_ENDINGS
            assert final["review"] and final["review"]["turns_played"] >= 8
            after = await client.call_tool("end_turn", {"session_id": session_id})
            assert after.is_error and "GAME_OVER" in text_of(after)
            log = await client.call_tool("export_log", {"session_id": session_id})
            assert not log.is_error and '"outcome"' in text_of(log)

    asyncio.run(run())
