"""GET /api/integration：外部模型（Codex / Claude Code）经 MCP 接入的现成命令。"""

import json
import sys
import tomllib

import pytest
from fastapi.testclient import TestClient

from api.app import create_app


@pytest.fixture
def client(mech_env, monkeypatch) -> TestClient:
    monkeypatch.delenv("SIM_MCP_LAUNCH", raising=False)
    monkeypatch.delenv("SIM_MCP_NAME", raising=False)
    monkeypatch.delenv("SIM_GAME_ROOT", raising=False)
    return TestClient(create_app())


def test_integration_from_source_points_at_this_python_and_home(client, mech_env) -> None:
    response = client.get("/api/integration")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    info = response.json()
    assert info["name"] == "llm-sim" and info["mode"] == "source"
    server = info["server"]
    assert server["command"] == sys.executable
    assert server["args"] == ["-m", "cli.main", "mcp"]
    assert server["cwd"] and server["cwd"].endswith("backend")
    assert server["env"]["LLM_SIM_HOME"] == str(mech_env.resolve()) == info["home"]
    assert server["env"]["SIM_SCENARIOS_DIR"] == info["scenarios_dir"]

    codex = info["codex"]
    assert codex["add"].startswith("codex mcp add llm-sim --env ")
    assert codex["add"].endswith(" -m cli.main mcp") and " -- " in codex["add"]
    table = tomllib.loads(codex["toml"])["mcp_servers"]["llm-sim"]
    assert table["command"] == sys.executable and table["args"] == server["args"] and table["cwd"] == server["cwd"]
    assert table["default_tools_approval_mode"] == "approve"
    assert table["env"] == server["env"]
    # -c 覆盖值本身也是 TOML
    for flag in codex["exec_flags"]:
        key, value = flag.split("=", 1)
        assert key.startswith("mcp_servers.llm-sim.")
        tomllib.loads(f"v = {value}")

    claude = info["claude"]
    assert claude["add"].startswith("claude mcp add llm-sim -e ")
    assert claude["add_user"].startswith("claude mcp add llm-sim --scope user -e ")
    entry = json.loads(claude["mcp_json"])["mcpServers"]["llm-sim"]
    assert entry == {"type": "stdio", "command": sys.executable, "args": server["args"], "env": server["env"]}
    assert info["setup"] is None


def test_integration_uses_shell_supplied_launch_spec(client, monkeypatch, tmp_path) -> None:
    exe = "C:\\Users\\某人\\AppData\\Local\\天宝\\server\\tianbao-server.exe"
    monkeypatch.setenv("SIM_MCP_NAME", "tianbao")
    monkeypatch.setenv("SIM_MCP_LAUNCH", json.dumps({"command": exe, "args": ["mcp"], "env": {}, "mode": "installed"}))
    root = tmp_path / "game"
    (root / "integrations").mkdir(parents=True)
    (root / "integrations" / "setup_mcp.ps1").write_text("# stub", encoding="utf-8")
    monkeypatch.setenv("SIM_GAME_ROOT", str(root))
    info = client.get("/api/integration").json()
    assert info["mode"] == "installed"
    assert info["codex"]["add"] == f'codex mcp add tianbao -- "{exe}" mcp'
    assert info["claude"]["add"] == f'claude mcp add tianbao -- "{exe}" mcp'
    assert info["claude"]["add_user"] == f'claude mcp add tianbao --scope user -- "{exe}" mcp'
    table = tomllib.loads(info["codex"]["toml"])["mcp_servers"]["tianbao"]
    assert table == {
        "command": exe,
        "args": ["mcp"],
        "startup_timeout_sec": 30,
        "tool_timeout_sec": 120,
        "default_tools_approval_mode": "approve",
    }
    assert json.loads(info["claude"]["mcp_json"]) == {"mcpServers": {"tianbao": {"type": "stdio", "command": exe, "args": ["mcp"]}}}
    setup = info["setup"]
    assert setup["script"].endswith("setup_mcp.ps1")
    assert setup["codex"].endswith(f' -ServerExe "{exe}" -Codex') and setup["claude"].endswith(" -Claude")
    # 只读：不产生存档
    assert client.get("/api/sessions").json() == []
