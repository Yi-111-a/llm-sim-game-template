import os
from pathlib import Path

import pytest

# 每次 resolve_turn 结束都运行不变量检查（见 simcore/domain/invariants.py）。
os.environ["SIM_CHECK_INVARIANTS"] = "1"

FIXTURE_SCENARIOS = Path(__file__).parent / "fixtures" / "scenarios"


@pytest.fixture
def mech_scenario() -> dict:
    from simcore.scenario.loader import load_scenario

    return load_scenario(FIXTURE_SCENARIOS / "mech_test")


@pytest.fixture
def mech_env(monkeypatch, tmp_path) -> Path:
    """指向测试场景目录与临时存档目录的环境。"""
    monkeypatch.setenv("SIM_SCENARIOS_DIR", str(FIXTURE_SCENARIOS))
    monkeypatch.setenv("LLM_SIM_HOME", str(tmp_path))
    monkeypatch.delenv("SIM_DEBUG", raising=False)
    monkeypatch.delenv("SIM_MCP_MODE", raising=False)
    return tmp_path
