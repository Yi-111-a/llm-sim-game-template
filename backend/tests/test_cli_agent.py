"""本机 CLI 模型（Claude Code / Codex）：用假的 CLI 脚本验证参数、输出解析、流式、未登录提示与 JSON 退回路径。"""

import asyncio
import json
import os
import sys
import textwrap

import pytest

from simcore.agent.loop import guarded_chat
from simcore.llm import cli_agent
from simcore.llm.base import ToolCallingUnsupported
from simcore.llm.cli_agent import CliAgentProvider, CliAgentUnavailable, render_prompt
from simcore.llm.factory import create_provider

FAKE = textwrap.dedent(
    r'''
    import json, os, sys
    argv = sys.argv[1:]
    prompt = sys.stdin.buffer.read().decode("utf-8")
    log = os.environ.get("FAKE_CLI_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "prompt": prompt}, ensure_ascii=False) + "\n")
    if "exec" in argv:  # codex
        out = argv[argv.index("-o") + 1]
        with open(out, "w", encoding="utf-8") as fh:
            fh.write("老奴遵旨。" if "JSON" not in prompt else '```json\n{"ops": []}\n```')
        sys.exit(0)
    if os.environ.get("FAKE_CLI_LOGGED_OUT"):
        print(json.dumps({"type": "result", "is_error": True, "result": "Not logged in · Please run /login"}))
        sys.exit(1)
    if "stream-json" in argv:
        for piece in ["臣胡人", "只知有", "陛下。"]:
            event = {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": piece}}}
            sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        print(json.dumps({"type": "result", "is_error": False, "result": "臣胡人只知有陛下。"}, ensure_ascii=False))
        sys.exit(0)
    print(json.dumps({"type": "result", "is_error": False, "result": "臣胡人只知有陛下。"}, ensure_ascii=False))
    '''
)


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE, encoding="utf-8")
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CLI_LOG", str(log))
    monkeypatch.setattr(cli_agent, "find_claude", lambda: [sys.executable, str(script)])
    monkeypatch.setattr(cli_agent, "find_codex", lambda: [sys.executable, str(script)])
    return log


def _calls(log):
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_render_prompt_flattens_turns_and_asks_for_json() -> None:
    system, prompt = render_prompt(
        [
            {"role": "system", "content": "你扮演安禄山。"},
            {"role": "user", "content": "范阳兵如何？"},
            {"role": "assistant", "content": "臣只知有陛下。"},
            {"role": "user", "content": "再说一遍。"},
        ],
        {"type": "json_object"},
    )
    assert system.startswith("你扮演安禄山。") and "不要使用任何工具" in system
    assert "【玩家】\n范阳兵如何？" in prompt and "【你（上一轮）】\n臣只知有陛下。" in prompt
    assert prompt.rstrip().endswith("不要任何解释。") and "——本轮——\n再说一遍。" in prompt


def test_claude_chat_runs_headless_without_tools_or_mcp(fake_cli, tmp_path) -> None:
    result = asyncio.run(CliAgentProvider("claude").chat([{"role": "system", "content": "S"}, {"role": "user", "content": "问"}]))
    assert result.content == "臣胡人只知有陛下。" and result.model == "Claude（本机）"
    call = _calls(fake_cli)[0]
    argv = call["argv"]
    assert argv[:3] == ["-p", "--output-format", "json"]
    assert argv[argv.index("--tools") + 1] == "" and "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert argv[argv.index("--system-prompt") + 1].startswith("S")
    assert call["prompt"] == "问"  # the prompt goes over stdin, not argv
    assert "tianbao-cli-" in call["cwd"]  # an empty temp dir: no project files, no CLAUDE.md


def test_claude_stream_yields_text_deltas(fake_cli) -> None:
    async def collect():
        return [piece async for piece in CliAgentProvider("claude").stream_text([{"role": "user", "content": "问"}])]

    assert asyncio.run(collect()) == ["臣胡人", "只知有", "陛下。"]


def test_claude_logged_out_gives_a_player_facing_hint(fake_cli, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLI_LOGGED_OUT", "1")
    with pytest.raises(CliAgentUnavailable, match="claude auth login"):
        asyncio.run(CliAgentProvider("claude").chat([{"role": "user", "content": "问"}]))


def test_codex_reads_the_last_message_file_and_strips_json_fences(fake_cli) -> None:
    provider = CliAgentProvider("codex", model="gpt-x")
    assert asyncio.run(provider.chat([{"role": "user", "content": "问"}])).content == "老奴遵旨。"
    parsed = asyncio.run(provider.chat([{"role": "user", "content": "问"}], response_format={"type": "json_object"}))
    assert json.loads(parsed.content) == {"ops": []}
    argv = _calls(fake_cli)[0]["argv"]
    assert argv[0] == "exec" and "--ephemeral" in argv and argv[argv.index("--sandbox") + 1] == "read-only"
    assert "mcp_servers={}" in argv and argv[argv.index("-m") + 1] == "gpt-x" and argv[-1] == "-"


def test_tools_fall_back_to_the_json_path() -> None:
    with pytest.raises(ToolCallingUnsupported):
        asyncio.run(CliAgentProvider("codex").chat([{"role": "user", "content": "x"}], tools=[{"type": "function"}]))


def test_slow_cli_gets_a_longer_call_timeout_through_wrappers() -> None:
    class Slow:
        min_call_timeout = 5.0
        model = "slow"

        async def chat(self, messages, tools=None, stream=False, response_format=None):
            await asyncio.sleep(0.3)
            return type("R", (), {"content": "ok"})()

    class Wrapper:  # like api.app.TrackedProvider
        def __init__(self, inner):
            self.provider = inner

        async def chat(self, *args, **kwargs):
            return await self.provider.chat(*args, **kwargs)

    async def run():
        deadline = asyncio.get_running_loop().time() + 10
        return await guarded_chat(Wrapper(Slow()), [{"role": "user", "content": "x"}], None, deadline, call_timeout=0.1)

    assert asyncio.run(run()).content == "ok"  # 0.1 s alone would time out; the 5 s floor carries through the wrapper


def test_factory_builds_cli_providers_from_presets() -> None:
    claude = create_provider({"provider": "claude_cli", "model": "default"}, "minister_chat")
    codex = create_provider({"provider": "codex_cli", "model": "default"}, "edict_interpreter")
    assert isinstance(claude, CliAgentProvider) and claude.kind == "claude"
    assert isinstance(codex, CliAgentProvider) and codex.kind == "codex"


def test_finds_claude_desktops_cli_in_the_msix_redirected_appdata(tmp_path, monkeypatch) -> None:
    # Claude 桌面版是 MSIX 应用：它看到的 %APPDATA% 实际在 %LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming，
    # 普通终端和天宝安装版只能在那里找到它自带的 claude.exe（取最新版本）
    base = tmp_path / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude" / "claude-code"
    for version, stamp in (("2.1.295", 1_000), ("2.1.299", 2_000)):
        exe = base / version / "abc" / "claude.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        os.utime(exe, (stamp, stamp))
    monkeypatch.delenv("SIM_CLAUDE_CLI", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "no-appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(cli_agent.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli_agent.Path, "home", lambda: tmp_path / "home")
    found = cli_agent.find_claude()
    assert found and found[0].endswith(str(base / "2.1.299" / "abc" / "claude.exe"))
