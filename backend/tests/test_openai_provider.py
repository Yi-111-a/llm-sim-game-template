"""OpenAICompatProvider 端到端：对接仿 OpenAI 服务器。"""

import asyncio
import json

import pytest
from fake_openai import FakeOpenAI
from fastapi.testclient import TestClient

from api.app import create_app
from simcore.agent.loop import AgentAborted, guarded_chat
from simcore.agent.roles import interpret_edict
from simcore.llm.base import ToolCallingUnsupported
from simcore.llm.openai_compat import OpenAICompatProvider
from simcore.scenario.loader import build_initial_state


def write_settings(home, base_url: str) -> None:
    (home / "settings.json").write_text(
        json.dumps({"provider": "custom", "base_url": base_url, "model": "fake-model", "api_key": "sk-test-0000", "roles": {}}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_edict_interpretation_via_tool_calls(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    state.rules["player_ops"] = {"allow": ["adjust_resource"]}  # 剧本可放开（settings.player_ops）；缺省不许玩家直接改数值
    with FakeOpenAI() as fake:
        fake.script.append({
            "tool_calls": [{"name": "propose_ops", "arguments": {"ops": [{"op": "adjust_resource", "resource": "treasury", "delta": -40, "reason": "赈济"}]}}],
        })
        provider = OpenAICompatProvider(fake.base_url, "sk-test-0000", "fake-model")
        receipt = asyncio.run(interpret_edict({"interior": "拨国库四十赈济"}, mech_scenario, state, provider, seed=1))
        body = fake.requests[0]
        assert body["model"] == "fake-model" and body["tool_choice"] == "required"
        assert {tool["function"]["name"] for tool in body["tools"]} == {"propose_ops", "submit_ops", "finish_turn"}
        assert fake.headers[0]["authorization"] == "Bearer sk-test-0000"
        assert "异志 约" in body["messages"][1]["content"]  # 提示词中的隐藏资源只有区间
    clause = receipt["clauses"][0]
    assert clause["status"] == "applied" and clause["op"]["delta"] == -40 and clause["source"] == "text"


def test_tool_calling_unsupported_falls_back_to_json_mode(mech_scenario) -> None:
    state = build_initial_state(mech_scenario)
    state.rules["player_ops"] = {"allow": ["adjust_resource"]}  # 剧本可放开（settings.player_ops）；缺省不许玩家直接改数值
    with FakeOpenAI(tools_supported=False) as fake:
        fake.script.append({"content": "```json\n" + json.dumps({"ops": [{"op": "adjust_resource", "resource": "grain", "delta": 25}]}) + "\n```"})
        provider = OpenAICompatProvider(fake.base_url, "", "fake-model")
        with pytest.raises(ToolCallingUnsupported):
            asyncio.run(provider.chat([{"role": "user", "content": "x"}], tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}]))
        receipt = asyncio.run(interpret_edict({"interior": "增粮"}, mech_scenario, state, provider, seed=1))
        assert fake.requests[-1]["response_format"] == {"type": "json_object"}
        assert "tools" not in fake.requests[-1]
    assert receipt["clauses"][0]["status"] == "applied"
    assert receipt["clauses"][0]["op"]["resource"] == "grain"


def test_streaming_text_and_streamed_tool_calls() -> None:
    with FakeOpenAI() as fake:
        provider = OpenAICompatProvider(fake.base_url, "", "fake-model")
        fake.script.append({"content": "陛下圣明，臣谨遵旨意。", "chunk": 3})

        async def collect() -> list[str]:
            return [piece async for piece in provider.stream_text([{"role": "user", "content": "问安"}])]

        pieces = asyncio.run(collect())
        assert len(pieces) > 1 and "".join(pieces) == "陛下圣明，臣谨遵旨意。"
        assert fake.requests[-1]["stream"] is True
        fake.script.append({"tool_calls": [{"name": "propose_ops", "arguments": {"ops": [{"op": "set_flag", "key": "长安", "value": True}]}}]})
        result = asyncio.run(provider.chat([{"role": "user", "content": "x"}], stream=True))
        assert result.tool_calls[0].name == "propose_ops"
        assert result.tool_calls[0].arguments["ops"][0]["key"] == "长安"
        assert result.usage.total_tokens == 18


def test_provider_timeout_and_abort() -> None:
    with FakeOpenAI() as fake:
        provider = OpenAICompatProvider(fake.base_url, "", "fake-model")
        fake.script.append({"content": "迟到的回答", "delay": 2})

        async def timed_out() -> None:
            loop = asyncio.get_running_loop()
            await guarded_chat(provider, [{"role": "user", "content": "x"}], None, loop.time() + 10, call_timeout=0.3)

        with pytest.raises(TimeoutError):
            asyncio.run(timed_out())
        fake.script.append({"content": "迟到的回答", "delay": 2})

        async def aborted() -> None:
            loop = asyncio.get_running_loop()
            event = asyncio.Event()
            loop.call_later(0.2, event.set)
            await guarded_chat(provider, [{"role": "user", "content": "x"}], None, loop.time() + 10, abort_event=event)

        with pytest.raises(AgentAborted):
            asyncio.run(aborted())


def test_api_chat_streams_sse_from_openai_compatible_provider(mech_env) -> None:
    with FakeOpenAI() as fake:
        write_settings(mech_env, fake.base_url)
        fake.script.append({"content": "老臣以为，边事宜缓图之。", "chunk": 2})
        client = TestClient(create_app())
        session_id = client.post("/api/sessions", json={"scenario": "mech_test", "seed": 2}).json()["id"]
        response = client.post(f"/api/sessions/{session_id}/chat/eunuch", json={"text": "边事如何？"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "charset=utf-8" in response.headers["content-type"]
        events = [block for block in response.text.split("\n\n") if block.startswith("event: message")]
        assert len(events) > 3
        text = "".join(json.loads(block.split("data: ", 1)[1])["text"] for block in events)
        assert text == "老臣以为，边事宜缓图之。"
        prompt = fake.requests[-1]["messages"][0]["content"]
        assert "高内侍" in prompt and "StateDigest" in prompt
        usage = client.get("/api/usage").json()
        assert usage["calls"][-1]["kind"] == "character_chat"


def test_api_draft_uses_provider_and_reports_provider_timeout_as_error(mech_env, monkeypatch) -> None:
    with FakeOpenAI() as fake:
        write_settings(mech_env, fake.base_url)
        fake.script.append({"tool_calls": [{"name": "propose_ops", "arguments": {"ops": [{"op": "appoint", "character": "general", "office": "marshal"}]}}]})
        client = TestClient(create_app())
        session_id = client.post("/api/sessions", json={"scenario": "mech_test", "seed": 2}).json()["id"]
        draft = client.post(f"/api/sessions/{session_id}/edicts/draft", json={"sections": {"interior": "君威稍振"}})
        assert draft.status_code == 200
        assert draft.json()["clauses"][0]["status"] == "applied"
        import simcore.agent.loop as loop_module

        original = loop_module.guarded_chat

        async def short_timeout(*args, **kwargs):
            kwargs["call_timeout"] = 0.2
            return await original(*args, **kwargs)

        monkeypatch.setattr("simcore.agent.roles.guarded_chat", short_timeout)
        fake.script.append({"content": "迟到", "delay": 2})
        slow = client.post(f"/api/sessions/{session_id}/edicts/draft", json={"sections": {"interior": "再议"}})
        assert slow.status_code == 504
        assert slow.json()["detail"]["code"] == "TIMEOUT"
