from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from simcore.agent.loop import AgentAborted
from simcore.agent.roles import interpret_edict, minister_chat, narrator
from simcore.config import (
    load_settings,
    provider_presets,
    public_settings,
    resolve_role_settings,
    save_settings,
)
from simcore.images.manifest import asset_path, placeholder_for, read_manifest
from simcore.llm.base import ChatResult, LLMProvider, ToolCallingUnsupported
from simcore.llm.factory import create_provider
from simcore.prompts.loader import render_prompt
from simcore.scenario.loader import build_initial_state, load_scenario, validate_scenario
from simcore.store.session import SessionStore


def project_root() -> Path:
    return Path(os.environ.get("SIM_GAME_ROOT", Path(__file__).resolve().parents[2])).resolve()


def scenarios_root() -> Path:
    return Path(os.environ.get("SIM_SCENARIOS_DIR", project_root() / "scenarios")).resolve()


def session_root() -> Path:
    return Path(os.environ.get("LLM_SIM_HOME", Path.home() / ".llm-sim")) / "sessions"


class CreateSession(BaseModel):
    scenario: str
    seed: int = 0


class ForkSession(BaseModel):
    entry_id: str | None = None


class DraftEdict(BaseModel):
    sections: dict[str, str] = Field(default_factory=dict)


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")


class TrackedProvider:
    def __init__(self, provider: LLMProvider, runtime: Runtime, kind: str) -> None:
        self.provider = provider
        self.runtime = runtime
        self.kind = kind

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResult:
        result = await self.provider.chat(messages, tools, stream, response_format)
        settings = load_settings()
        role_settings = resolve_role_settings(settings, self.kind)
        input_rate = float(settings.get("input_cost_per_million", 0))
        output_rate = float(settings.get("output_cost_per_million", 0))
        estimated = (result.usage.input_tokens * input_rate + result.usage.output_tokens * output_rate) / 1_000_000
        result.usage.estimated_cost = estimated
        self.runtime.usage.append({
            "id": uuid.uuid4().hex[:12],
            "kind": self.kind,
            "provider": role_settings["provider"],
            "model": result.model,
            "turn": self.runtime.current_turn,
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "total_tokens": result.usage.total_tokens,
            "estimated_cost": estimated,
            "cost_rates_configured": bool(input_rate or output_rate),
            "timestamp": datetime.now(UTC).isoformat(),
        })
        return result


class Runtime:
    def __init__(self) -> None:
        self.sessions: dict[str, SessionStore] = {}
        self.scenarios: dict[str, dict[str, Any]] = {}
        self.drafts: dict[tuple[str, str], dict[str, Any]] = {}
        self.usage: list[dict[str, Any]] = []
        self.current_turn = 0
        self.abort_events: dict[str, asyncio.Event] = {}

    def scenario(self, scenario_id: str) -> dict[str, Any]:
        if scenario_id not in self.scenarios:
            path = (scenarios_root() / scenario_id).resolve()
            if scenarios_root() not in path.parents:
                raise HTTPException(status_code=400, detail="无效场景路径")
            try:
                data = load_scenario(path)
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail="场景不存在") from exc
            self.scenarios[scenario_id] = data
        return self.scenarios[scenario_id]

    def session(self, session_id: str) -> SessionStore:
        if session_id in self.sessions:
            return self.sessions[session_id]
        path = session_root() / f"{session_id}.jsonl"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="存档不存在")
        header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        scenario = self.scenario(header["scenario"])
        session = SessionStore(path, scenario)
        self.sessions[session_id] = session
        return session

    def provider(self, scenario: dict[str, Any], kind: str) -> TrackedProvider:
        settings = load_settings()
        role = "character_chat" if kind == "minister_chat" else kind
        return TrackedProvider(create_provider(settings, role, scenario), self, role)


def create_app() -> FastAPI:
    runtime = Runtime()
    app = FastAPI(title="LLM 模拟游戏", version="0.1.0")
    app.state.runtime = runtime

    @app.get("/api/scenarios")
    async def list_scenarios() -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for folder in sorted(scenarios_root().iterdir()):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            try:
                data = runtime.scenario(folder.name)
                errors = validate_scenario(data)
                result.append({
                    "id": data["scenario"]["id"],
                    "title": data["scenario"]["title"],
                    "era": data["scenario"]["era"],
                    "version": data["scenario"].get("version", "0.1.0"),
                    "categories": data["scenario"].get("edict_categories", []),
                    "calendar": data["scenario"].get("calendar", {}),
                    "asset_ids": [
                        entry.get("id", "")
                        for entry in data.get("assets", {}).get("entries", [])
                    ],
                    "theme_css": (
                        (Path(data["root"]) / "theme.css").read_text(encoding="utf-8")
                        if (Path(data["root"]) / "theme.css").is_file()
                        else ""
                    ),
                    "valid": not errors,
                })
            except (OSError, KeyError, ValueError):
                continue
        return result

    @app.post("/api/sessions")
    async def create_session(request: CreateSession) -> dict[str, Any]:
        scenario = runtime.scenario(request.scenario)
        errors = validate_scenario(scenario)
        if errors:
            raise HTTPException(status_code=422, detail=errors)
        initial_state = build_initial_state(scenario)
        session_id = uuid.uuid4().hex
        session = SessionStore(session_root() / f"{session_id}.jsonl", scenario, initial_state, request.seed)
        runtime.sessions[session_id] = session
        return {"id": session_id, "scenario": request.scenario, "state": session.state.model_dump(mode="json")}

    @app.get("/api/sessions")
    async def list_sessions() -> list[dict[str, Any]]:
        result = []
        for path in session_root().glob("*.jsonl"):
            try:
                header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
                result.append({"id": path.stem, "scenario": header["scenario"], "turn": runtime.session(path.stem).state.turn})
            except (OSError, KeyError, ValueError, IndexError):
                continue
        return result

    @app.post("/api/sessions/{session_id}/fork")
    async def fork_session(session_id: str, request: ForkSession) -> dict[str, Any]:
        source = runtime.session(session_id)
        fork_id = uuid.uuid4().hex
        forked = source.fork(session_root() / f"{fork_id}.jsonl", request.entry_id)
        runtime.sessions[fork_id] = forked
        return {"id": fork_id, "state": forked.state.model_dump(mode="json")}

    @app.get("/api/sessions/{session_id}/state")
    async def get_state(session_id: str) -> dict[str, Any]:
        return runtime.session(session_id).state.model_dump(mode="json")

    @app.post("/api/sessions/{session_id}/edicts/draft")
    async def draft_edict(session_id: str, request: DraftEdict) -> dict[str, Any]:
        session = runtime.session(session_id)
        scenario = runtime.scenario(session.state.scenario_id)
        runtime.current_turn = session.state.turn
        abort_event = asyncio.Event()
        runtime.abort_events[session_id] = abort_event
        try:
            receipt = await interpret_edict(
                request.sections,
                scenario,
                session.state,
                runtime.provider(scenario, "edict_interpreter"),
                session.seed,
                abort_event,
            )
        except AgentAborted as exc:
            raise HTTPException(status_code=409, detail="诏令处理已中止") from exc
        finally:
            runtime.abort_events.pop(session_id, None)
        runtime.drafts[(session_id, receipt["id"])] = receipt
        return receipt

    @app.post("/api/sessions/{session_id}/abort")
    async def abort_agent(session_id: str) -> dict[str, bool]:
        runtime.session(session_id)
        abort_event = runtime.abort_events.get(session_id)
        if abort_event is None:
            abort_event = asyncio.Event()
            runtime.abort_events[session_id] = abort_event
        abort_event.set()
        return {"aborted": True}

    @app.post("/api/sessions/{session_id}/edicts/{edict_id}/confirm")
    async def confirm_edict(session_id: str, edict_id: str) -> dict[str, Any]:
        session = runtime.session(session_id)
        receipt = runtime.drafts.get((session_id, edict_id))
        if receipt is None:
            raise HTTPException(status_code=404, detail="诏令草案不存在或已过期")
        if receipt.get("confirmed"):
            raise HTTPException(status_code=409, detail="诏令已经确认")
        for clause in receipt["clauses"]:
            if clause["status"] == "applied" and clause.get("op"):
                try:
                    session.append_op(clause["op"])
                except ValueError as exc:
                    clause["status"] = "rejected"
                    clause["reason"] = str(exc)
            elif clause["status"] == "delayed" and clause.get("op"):
                session.append_pending([clause["op"]], session.state.turn + 1)
        receipt["confirmed"] = True
        receipt["state"] = session.state.model_dump(mode="json")
        return receipt

    @app.post("/api/sessions/{session_id}/edicts/polish")
    async def polish_edict(session_id: str, request: DraftEdict) -> dict[str, Any]:
        session = runtime.session(session_id)
        scenario = runtime.scenario(session.state.scenario_id)
        provider = runtime.provider(scenario, "edict_polish")
        text = "\n".join(f"{category}：{value}" for category, value in request.sections.items())
        result = await provider.chat([
            {
                "role": "system",
                "content": render_prompt(
                    "edict_polish", {"text": text}, scenario.get("root")
                ),
            },
            {"role": "user", "content": text},
        ])
        return {"text": result.content}

    @app.post("/api/sessions/{session_id}/turn")
    async def advance_turn(session_id: str) -> dict[str, Any]:
        session = runtime.session(session_id)
        scenario = runtime.scenario(session.state.scenario_id)
        runtime.current_turn = session.state.turn
        state, report, _ = session.append_turn()
        memorial = await narrator(report, runtime.provider(scenario, "narrator"), scenario)
        session.append_message("narrator", memorial, {"turn": state.turn})
        return {
            "state": session.state.model_dump(mode="json"),
            "report": report.model_dump(mode="json"),
            "memorial": memorial,
        }

    @app.get("/api/sessions/{session_id}/history")
    async def get_history(session_id: str) -> list[dict[str, Any]]:
        return runtime.session(session_id).history()

    @app.post("/api/sessions/{session_id}/chat/{character_id}")
    async def chat(session_id: str, character_id: str, request: dict[str, str]) -> StreamingResponse:
        session = runtime.session(session_id)
        scenario = runtime.scenario(session.state.scenario_id)
        runtime.current_turn = session.state.turn
        afterlife = bool(scenario.get("scenario", {}).get("settings", {}).get("afterlife_chat", False))
        result = await minister_chat(
            character_id,
            session.state,
            runtime.provider(scenario, "minister_chat"),
            afterlife_chat=afterlife,
            user_text=request.get("text", ""),
            scenario=scenario,
        )
        session.append_message("player", request.get("text", ""), {"character": character_id})
        session.append_message("character", result["content"], {"character": character_id, "status": result["status"]})

        async def events():
            yield f"event: status\ndata: {json.dumps({'status': result['status']}, ensure_ascii=False)}\n\n"
            content = result["content"]
            for offset in range(0, len(content), 20):
                yield f"event: message\ndata: {json.dumps({'text': content[offset:offset + 20]}, ensure_ascii=False)}\n\n"
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/api/assets/{scenario_id}/{asset_id:path}")
    async def get_asset(scenario_id: str, asset_id: str) -> Response:
        data = runtime.scenario(scenario_id)
        root = Path(data["root"])
        manifest = read_manifest(root)
        entry = next((item for item in manifest.get("entries", []) if item.get("id") == asset_id), None)
        if entry is None:
            payload = placeholder_for(root, asset_id)
            return Response(payload, media_type="image/svg+xml")
        file_path = asset_path(root, entry["path"])
        if file_path.is_file():
            payload = file_path.read_bytes()
            media_type = "image/svg+xml" if payload.lstrip().startswith(b"<svg") else (
                "image/png" if entry.get("format") == "png" else "image/webp"
            )
            return Response(payload, media_type=media_type)
        return Response(
            placeholder_for(root, asset_id),
            media_type="image/svg+xml",
        )

    @app.get("/api/usage")
    async def get_usage() -> dict[str, Any]:
        total_tokens = sum(int(row["total_tokens"]) for row in runtime.usage)
        return {
            "calls": runtime.usage,
            "total_tokens": total_tokens,
            "estimated_cost": sum(float(row["estimated_cost"]) for row in runtime.usage),
            "cost_rates_configured": any(row["cost_rates_configured"] for row in runtime.usage),
        }

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return {**public_settings(load_settings()), "presets": provider_presets()}

    @app.put("/api/settings")
    async def put_settings(request: SettingsUpdate) -> dict[str, Any]:
        current = load_settings()
        current.update(request.model_dump(exclude_unset=True))
        save_settings(current)
        return {**public_settings(current), "presets": provider_presets()}

    @app.post("/api/settings/test")
    async def test_settings() -> dict[str, Any]:
        provider = runtime.provider({}, "edict_interpreter")
        probe = [{
            "type": "function",
            "function": {
                "name": "finish_turn",
                "description": "Return immediately.",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        try:
            result = await provider.chat(
                [
                    {"role": "system", "content": "调用工具完成这次简短的诏令能力探测。"},
                    {"role": "user", "content": "请立即调用 finish_turn。"},
                ],
                tools=probe,
            )
        except ToolCallingUnsupported:
            return {"ok": True, "tool_calling": False, "json_fallback": True}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"模型连接失败：{exc}") from exc
        return {
            "ok": True,
            "tool_calling": bool(result.tool_calls),
            "json_fallback": not bool(result.tool_calls),
            "model": result.model,
        }

    return app


app = create_app()
