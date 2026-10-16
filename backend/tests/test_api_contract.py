"""§2 REST API 契约：每个端点、统一错误形状、UTF-8 头、CORS、隐藏数值不外泄。"""

import json

import pytest
from fastapi.testclient import TestClient

from api.app import create_app


@pytest.fixture
def client(mech_env) -> TestClient:
    return TestClient(create_app())


def new_session(client: TestClient, seed: int = 7) -> str:
    response = client.post("/api/sessions", json={"scenario": "mech_test", "seed": seed})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert isinstance(detail["message"], str) and detail["message"]
    assert "Traceback" not in response.text


def assert_utf8_json(response) -> None:
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    raw = response.content.decode("utf-8")
    assert "\\u" not in raw  # ensure_ascii=False


def test_health_and_scenarios(client) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert_utf8_json(health)
    assert health.json()["ok"] is True and "mech_test" in health.json()["scenarios"]
    assert client.get("/api/health").json()["version"] == health.json()["version"]
    scenarios = client.get("/api/scenarios")
    assert_utf8_json(scenarios)
    assert scenarios.json()[0]["title"] == "机制测试" and scenarios.json()[0]["valid"] is True


def test_cors_allows_local_and_tauri_origins_only(client) -> None:
    for origin in ("http://127.0.0.1:5173", "http://localhost:1420", "tauri://localhost", "http://tauri.localhost"):
        response = client.get("/health", headers={"Origin": origin})
        assert response.headers.get("access-control-allow-origin") == origin
    preflight = client.options("/api/sessions", headers={"Origin": "tauri://localhost", "Access-Control-Request-Method": "POST"})
    assert preflight.status_code == 200
    evil = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in evil.headers


def test_view_contract_and_hidden_values_never_leak(client) -> None:
    session_id = new_session(client)
    view = client.get(f"/api/sessions/{session_id}/view")
    assert_utf8_json(view)
    data = view.json()
    for key in ("state", "calendar_label", "objectives", "crisis", "memorials", "pending_choices", "outcome", "last_report", "can_advance"):
        assert key in data
    assert data["calendar_label"] == "天宝十载春"
    assert data["can_advance"] is True and data["outcome"] is None
    assert data["objectives"][0] == {"id": "calm", "title": "弭兵", "status": "on_track", "detail": "危机计低于 60"}
    assert data["crisis"]["band"] == "承平" and data["crisis"]["value_or_estimate"] == 30
    assert [term["label"] for term in data["crisis"]["terms"]] == ["异志", "积弊"]
    assert data["memorials"][0]["id"] == "m_tax"
    ambition = data["state"]["resources"]["ambition"]
    assert ambition["hidden"] is True and "amount" not in ambition
    state = client.get(f"/api/sessions/{session_id}/state").json()
    assert "amount" not in state["resources"]["ambition"]
    assert_error(client.get(f"/api/sessions/{session_id}/state?debug=1"), 403, "DEBUG_DISABLED")


def test_debug_state_requires_sim_debug(client, monkeypatch) -> None:
    session_id = new_session(client)
    monkeypatch.setenv("SIM_DEBUG", "1")
    full = client.get(f"/api/sessions/{session_id}/state?debug=1")
    assert full.status_code == 200 and full.json()["resources"]["ambition"]["amount"] == 40


def test_draft_confirm_with_memorial_client_ops_and_text(client) -> None:
    session_id = new_session(client)
    draft = client.post(
        f"/api/sessions/{session_id}/edicts/draft",
        json={
            "sections": {"interior": "国库增加 10"},
            "adopted": [{"memorial_id": "m_tax", "option_id": "approve"}],
            "ops": [{"op": "appoint", "character": "general", "office": "marshal"}, {"op": "appoint", "character": "nobody", "office": "censor"}],
        },
    )
    assert_utf8_json(draft)
    receipt = draft.json()
    assert [clause["source"] for clause in receipt["clauses"]] == ["memorial", "memorial", "client", "client", "text"]
    assert [clause["status"] for clause in receipt["clauses"]] == ["applied", "applied", "applied", "rejected", "narrative_only"]
    assert receipt["clauses"][3]["code"] == "UNKNOWN_ENTITY"
    assert receipt["clauses"][4]["code"] == "OFFLINE_TEXT"  # 未接模型：御笔只记入史册（§7.4）
    confirmed = client.post(f"/api/sessions/{session_id}/edicts/{receipt['id']}/confirm")
    assert confirmed.status_code == 200
    state = confirmed.json()["state"]
    assert state["flags"]["memorial:m_tax"] == "approve"
    assert state["resources"]["treasury"]["amount"] == 950
    assert state["offices"]["marshal"]["holder"] == "general"
    assert client.get(f"/api/sessions/{session_id}/view").json()["memorials"] == []
    assert_error(client.post(f"/api/sessions/{session_id}/edicts/{receipt['id']}/confirm"), 409, "EDICT_CONFIRMED")
    assert_error(client.post(f"/api/sessions/{session_id}/edicts/nope/confirm"), 404, "EDICT_NOT_FOUND")


def test_stale_draft_cannot_be_confirmed_after_turn(client) -> None:
    session_id = new_session(client)
    receipt = client.post(f"/api/sessions/{session_id}/edicts/draft", json={"ops": [{"op": "adjust_resource", "resource": "grain", "delta": 5}]}).json()
    assert client.post(f"/api/sessions/{session_id}/turn").status_code == 200
    assert_error(client.post(f"/api/sessions/{session_id}/edicts/{receipt['id']}/confirm"), 409, "EDICT_STALE")


def test_turn_choice_flow_and_report_shape(client) -> None:
    session_id = new_session(client)
    turned = client.post(f"/api/sessions/{session_id}/turn")
    assert_utf8_json(turned)
    body = turned.json()
    assert set(body) >= {"state", "report", "memorial", "outcome"}
    report = body["report"]
    assert report["events_fired"] == ["petition"]
    assert report["choices_opened"][0]["id"] == "choice-petition-1"
    assert {term["id"] for term in report["crisis"]["terms"]} == {"amb", "cor"}
    assert all(line["amount"] is None for line in report["ledger_lines"] if line["resource"] == "ambition")
    view = client.get(f"/api/sessions/{session_id}/view").json()
    assert view["pending_choices"][0]["id"] == "choice-petition-1"
    assert view["last_report"]["turn"] == 1
    assert {item["id"] for item in view["memorials"]} == {"m_tax", "m_border"}
    assert_error(client.post(f"/api/sessions/{session_id}/choices/choice-petition-1", json={"option_id": "special"}), 422, "CONDITION_FAILED")
    assert_error(client.post(f"/api/sessions/{session_id}/choices/ghost", json={"option_id": "grant"}), 404, "UNKNOWN_ENTITY")
    assert_error(client.post(f"/api/sessions/{session_id}/choices/choice-petition-1", json={}), 422, "INVALID_REQUEST")
    chosen = client.post(f"/api/sessions/{session_id}/choices/choice-petition-1", json={"option_id": "grant"})
    assert chosen.status_code == 200
    assert chosen.json()["pending_choices"] == []
    assert chosen.json()["state"]["resources"]["treasury"]["amount"] < 1100


def test_game_over_returns_409_and_blocks_writes(client) -> None:
    session_id = new_session(client)
    assert client.post(f"/api/sessions/{session_id}/turn").json()["outcome"] is None
    receipt = client.post(
        f"/api/sessions/{session_id}/edicts/draft",
        json={"ops": [{"op": "set_region_controller", "region": "capital", "faction": "rebels"}]},
    ).json()
    assert receipt["clauses"][0]["code"] == "OP_NOT_PERMITTED"  # 玩家不能直接改地区归属
    client.app.state.runtime.service.session(session_id).append_op({"op": "set_region_controller", "region": "capital", "faction": "rebels"})
    outcome = client.post(f"/api/sessions/{session_id}/turn").json()["outcome"]
    assert outcome is not None and outcome["id"] == "fail_capital" and outcome["kind"] == "fail"
    assert_error(client.post(f"/api/sessions/{session_id}/turn"), 409, "GAME_OVER")
    assert_error(client.post(f"/api/sessions/{session_id}/edicts/draft", json={"ops": []}), 409, "GAME_OVER")
    view = client.get(f"/api/sessions/{session_id}/view").json()
    assert view["can_advance"] is False and view["outcome"]["title"] == "京城不守"
    history = client.get(f"/api/sessions/{session_id}/history").json()
    first_turn = next(row for row in history if row["type"] == "turn")
    rewound = client.post(f"/api/sessions/{session_id}/rewind", json={"entry_id": first_turn["id"]})
    assert rewound.status_code == 200
    assert rewound.json()["can_advance"] is True and rewound.json()["turn"] == 1


def test_debate_advice_history_rewind_fork_delete(client) -> None:
    session_id = new_session(client)
    debate = client.post(f"/api/sessions/{session_id}/debate", json={"topic": "边镇增兵议", "participants": ["chancellor", "general"]})
    assert_utf8_json(debate)
    speeches = debate.json()["speeches"]
    assert [(row["character"], row["stance"]) for row in speeches] == [("chancellor", "support"), ("general", "oppose")]
    assert all(row["text"] for row in speeches) and "赞成" in debate.json()["summary"]
    assert_error(client.post(f"/api/sessions/{session_id}/debate", json={"topic": "x", "participants": ["ghost"]}), 404, "UNKNOWN_CHARACTER")
    assert_error(client.post(f"/api/sessions/{session_id}/debate", json={"topic": "x", "participants": []}), 422, "INVALID_REQUEST")
    added = client.post(f"/api/sessions/{session_id}/advice", json={"character": "general", "text": "宜守雄关", "source": "debate"})
    assert added.status_code == 200 and added.json()["turn"] == 0
    assert client.get(f"/api/sessions/{session_id}/advice").json()[0]["text"] == "宜守雄关"
    assert_error(client.post(f"/api/sessions/{session_id}/advice", json={"character": "ghost", "text": "x"}), 404, "UNKNOWN_CHARACTER")
    state_before = client.get(f"/api/sessions/{session_id}/view").json()["state"]
    client.post(f"/api/sessions/{session_id}/turn")
    history = client.get(f"/api/sessions/{session_id}/history").json()
    assert {"id", "type", "turn", "label", "parentId"} <= set(history[0])
    assert any(row["type"] == "advice" for row in history)
    advice_entry = next(row for row in history if row["type"] == "advice")
    rewound = client.post(f"/api/sessions/{session_id}/rewind", json={"entry_id": advice_entry["id"]})
    assert rewound.json()["turn"] == 0 and rewound.json()["state"]["resources"] == state_before["resources"]
    timeline = client.get(f"/api/sessions/{session_id}/history").json()
    assert next(row for row in timeline if row["head"])["id"] == advice_entry["id"]
    assert_error(client.post(f"/api/sessions/{session_id}/rewind", json={"entry_id": "nope"}), 404, "ENTRY_NOT_FOUND")
    fork = client.post(f"/api/sessions/{session_id}/fork", json={"entry_id": None})
    assert fork.status_code == 200
    fork_id = fork.json()["id"]
    assert {row["id"] for row in client.get("/api/sessions").json()} >= {session_id, fork_id}
    deleted = client.delete(f"/api/sessions/{fork_id}")
    assert deleted.json() == {"deleted": True, "id": fork_id}
    assert_error(client.get(f"/api/sessions/{fork_id}/view"), 404, "SESSION_NOT_FOUND")


def test_chat_refusal_digest_and_error_shapes(client) -> None:
    session_id = new_session(client)
    ok = client.post(f"/api/sessions/{session_id}/chat/chancellor", json={"text": "国用如何"})
    assert ok.status_code == 200 and '"status": "ok"' in ok.text
    digest = client.get(f"/api/sessions/{session_id}/digest?character=chancellor").json()["digest"]
    assert "【本人】李相" in digest
    assert_error(client.get("/api/sessions/nope/view"), 404, "SESSION_NOT_FOUND")
    assert_error(client.post("/api/sessions", json={"scenario": "ghost"}), 404, "SCENARIO_NOT_FOUND")
    assert_error(client.post("/api/sessions", json={"seed": 1}), 422, "INVALID_REQUEST")
    assert_error(client.get("/api/no-such-endpoint"), 404, "NOT_FOUND")
    assert_error(client.post(f"/api/sessions/{session_id}/edicts/draft", json={"unknown": 1}), 422, "INVALID_REQUEST")


def test_session_file_and_responses_are_utf8_without_mojibake(client, mech_env) -> None:
    session_id = new_session(client)
    client.post(f"/api/sessions/{session_id}/advice", json={"character": "eunuch", "text": "民力宜惜，长安米贵"})
    client.post(f"/api/sessions/{session_id}/turn")
    raw = (mech_env / "sessions" / f"{session_id}.jsonl").read_bytes()
    text = raw.decode("utf-8")
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert "长安米贵" in text and "�" not in text and "锟斤拷" not in text
    for line in text.splitlines():
        json.loads(line)


def test_assets_usage_settings_endpoints_still_work(client) -> None:
    asset = client.get("/api/assets/mech_test/character.ruler.portrait")
    assert asset.status_code == 200 and asset.headers["content-type"].startswith("image/svg+xml")
    assert client.get("/api/usage").json()["total_tokens"] >= 0
    assert "presets" in client.get("/api/settings").json()


def test_abort_and_polish_endpoints(client) -> None:
    session_id = new_session(client)
    assert client.post(f"/api/sessions/{session_id}/abort").json() == {"aborted": True}
    polished = client.post(f"/api/sessions/{session_id}/edicts/polish", json={"sections": {"interior": "减赋"}})
    assert polished.status_code == 200 and isinstance(polished.json()["text"], str)
    assert_error(client.post("/api/sessions/nope/abort"), 404, "SESSION_NOT_FOUND")
