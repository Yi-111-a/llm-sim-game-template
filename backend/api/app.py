from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from simcore.agent.roles import minister_chat_messages
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
from simcore.service import (
    ENGINE_VERSION,
    GameError,
    GameService,
    scenarios_root,
    session_root,
)

__all__ = ["create_app", "app", "scenarios_root", "session_root"]

CORS_ORIGIN_REGEX = r"^(https?://(127\.0\.0\.1|localhost)(:\d+)?|tauri://localhost|https?://tauri\.localhost(:\d+)?)$"
STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "INVALID_REQUEST",
    500: "INTERNAL",
}


class UTF8JSONResponse(JSONResponse):
    """所有 JSON 响应：ensure_ascii=False，Content-Type 带 charset=utf-8。"""

    media_type = "application/json; charset=utf-8"

    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSession(RequestModel):
    scenario: str
    seed: int = 0
    difficulty: str | None = None


class ForkSession(RequestModel):
    entry_id: str | None = None


class AdoptedOption(RequestModel):
    memorial_id: str
    option_id: str


class DraftEdict(RequestModel):
    sections: dict[str, str] = Field(default_factory=dict)
    adopted: list[AdoptedOption] = Field(default_factory=list)
    ops: list[dict[str, Any]] = Field(default_factory=list)


class PolishEdict(RequestModel):
    sections: dict[str, str] = Field(default_factory=dict)


class ChooseOption(RequestModel):
    option_id: str


class DebateRequest(RequestModel):
    topic: str = ""
    participants: list[str]
    memorial_id: str | None = None


class AdviceRequest(RequestModel):
    character: str
    text: str
    source: str = "player"


class RewindRequest(RequestModel):
    entry_id: str | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    text: str = ""


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")


class TrackedProvider:
    """记录每次模型调用的 token 用量与估算成本。"""

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
        self._record(result.model, result.usage.input_tokens, result.usage.output_tokens, result)
        return result

    async def stream_text(self, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        streamer = getattr(self.provider, "stream_text", None)
        produced: list[str] = []
        if streamer is None:
            result = await self.provider.chat(messages)
            produced.append(result.content)
            yield result.content
        else:
            async for piece in streamer(messages):
                produced.append(piece)
                yield piece
        prompt_chars = sum(len(str(message.get("content", ""))) for message in messages)
        self._record(getattr(self.provider, "model", "mock"), max(1, prompt_chars // 4), max(1, len("".join(produced)) // 4), None)

    def _record(self, model: str, input_tokens: int, output_tokens: int, result: ChatResult | None) -> None:
        settings = load_settings()
        role_settings = resolve_role_settings(settings, self.kind)
        input_rate = float(settings.get("input_cost_per_million", 0))
        output_rate = float(settings.get("output_cost_per_million", 0))
        estimated = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
        if result is not None:
            result.usage.estimated_cost = estimated
        self.runtime.usage.append({
            "id": uuid.uuid4().hex[:12],
            "kind": self.kind,
            "provider": role_settings["provider"],
            "model": model,
            "turn": self.runtime.current_turn,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "estimated_cost": estimated,
            "cost_rates_configured": bool(input_rate or output_rate),
            "timestamp": datetime.now(UTC).isoformat(),
        })


class Runtime:
    def __init__(self) -> None:
        self.usage: list[dict[str, Any]] = []
        self.current_turn = 0
        self.service = GameService(self.provider)

    def provider(self, scenario: dict[str, Any], kind: str) -> TrackedProvider:
        settings = load_settings()
        role = "character_chat" if kind == "minister_chat" else kind
        return TrackedProvider(create_provider(settings, role, scenario), self, role)

    # 兼容旧测试与脚本：runtime.session(id) 返回 SessionStore
    def session(self, session_id: str) -> Any:
        return self.service.session(session_id)


def _error(status: int, code: str, message: str) -> UTF8JSONResponse:
    return UTF8JSONResponse({"detail": {"code": code, "message": message}}, status_code=status)


def create_app() -> FastAPI:
    runtime = Runtime()
    service = runtime.service
    app = FastAPI(title="LLM 模拟游戏", version=ENGINE_VERSION, default_response_class=UTF8JSONResponse)
    app.state.runtime = runtime
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=CORS_ORIGIN_REGEX,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(GameError)
    async def game_error_handler(_: Request, exc: GameError) -> UTF8JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_: Request, exc: StarletteHTTPException) -> UTF8JSONResponse:
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            return UTF8JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        message = "接口不存在" if exc.status_code == 404 else str(exc.detail)
        return _error(exc.status_code, STATUS_CODES.get(exc.status_code, "HTTP_ERROR"), message)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> UTF8JSONResponse:
        parts = [f"{'.'.join(str(item) for item in error.get('loc', []))}: {error.get('msg', '')}" for error in exc.errors()]
        return _error(422, "INVALID_REQUEST", "请求参数无效：" + "；".join(parts[:5]))

    @app.exception_handler(Exception)
    async def internal_error_handler(_: Request, exc: Exception) -> UTF8JSONResponse:
        return _error(500, "INTERNAL", f"服务内部错误：{type(exc).__name__}")

    @app.get("/health")
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": ENGINE_VERSION, "scenarios": service.scenario_ids()}

    @app.get("/api/scenarios")
    async def list_scenarios() -> list[dict[str, Any]]:
        return service.list_scenarios()

    @app.post("/api/sessions")
    async def create_session(request: CreateSession) -> dict[str, Any]:
        session_id, session = service.create_session(request.scenario, request.seed, request.difficulty)
        return {
            "id": session_id,
            "scenario": request.scenario,
            "difficulty": service.difficulty_info(session.head_state()),
            "state": service.public_state(session),
        }

    @app.get("/api/sessions")
    async def list_sessions() -> list[dict[str, Any]]:
        return service.list_sessions()

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        return service.delete_session(session_id)

    @app.post("/api/sessions/{session_id}/fork")
    async def fork_session(session_id: str, request: ForkSession) -> dict[str, Any]:
        fork_id, forked = service.fork_session(session_id, request.entry_id)
        return {"id": fork_id, "state": service.public_state(forked)}

    @app.get("/api/sessions/{session_id}/state")
    async def get_state(session_id: str, debug: int = 0) -> dict[str, Any]:
        if debug:
            return service.debug_state(session_id)
        return service.public_state(service.session(session_id))

    @app.get("/api/sessions/{session_id}/view")
    async def get_view(session_id: str) -> dict[str, Any]:
        return service.view(session_id)

    @app.get("/api/sessions/{session_id}/op_reference")
    async def get_op_reference(
        session_id: str, op: str | None = None, entity_kind: str | None = None, ids: str | None = None
    ) -> dict[str, Any]:
        id_list = [item for item in (ids or "").split(",") if item]
        return service.op_reference(session_id, op, entity_kind, id_list or None)

    @app.get("/api/sessions/{session_id}/digest")
    async def get_digest(session_id: str, character: str | None = None) -> dict[str, Any]:
        return {"digest": service.digest(session_id, character)}

    @app.post("/api/sessions/{session_id}/edicts/draft")
    async def draft_edict(session_id: str, request: DraftEdict) -> dict[str, Any]:
        runtime.current_turn = service.session(session_id).head_turn()
        return await service.draft_edict(
            session_id,
            request.sections,
            [row.model_dump() for row in request.adopted],
            request.ops,
        )

    @app.post("/api/sessions/{session_id}/abort")
    async def abort_agent(session_id: str) -> dict[str, bool]:
        return service.abort(session_id)

    @app.post("/api/sessions/{session_id}/edicts/{edict_id}/confirm")
    async def confirm_edict(session_id: str, edict_id: str) -> dict[str, Any]:
        return service.confirm_edict(session_id, edict_id)

    @app.post("/api/sessions/{session_id}/edicts/polish")
    async def polish_edict(session_id: str, request: PolishEdict) -> dict[str, Any]:
        session = service.session(session_id)
        provider = runtime.provider(session.scenario, "edict_polish")
        text = "\n".join(f"{category}：{value}" for category, value in request.sections.items())
        result = await provider.chat([
            {"role": "system", "content": render_prompt("edict_polish", {"text": text}, session.scenario.get("root"))},
            {"role": "user", "content": text},
        ])
        return {"text": result.content}

    @app.post("/api/sessions/{session_id}/choices/{choice_id}")
    async def choose(session_id: str, choice_id: str, request: ChooseOption) -> dict[str, Any]:
        return service.choose(session_id, choice_id, request.option_id)

    @app.post("/api/sessions/{session_id}/turn")
    async def advance_turn(session_id: str) -> dict[str, Any]:
        runtime.current_turn = service.session(session_id).head_turn()
        return await service.end_turn(session_id)

    @app.post("/api/sessions/{session_id}/debate")
    async def debate(session_id: str, request: DebateRequest) -> dict[str, Any]:
        return await service.debate(session_id, request.topic, request.participants, request.memorial_id)

    @app.get("/api/sessions/{session_id}/advice")
    async def list_advice(session_id: str) -> list[dict[str, Any]]:
        return service.list_advice(session_id)

    @app.post("/api/sessions/{session_id}/advice")
    async def add_advice(session_id: str, request: AdviceRequest) -> dict[str, Any]:
        return service.add_advice(session_id, request.character, request.text, request.source)

    @app.get("/api/sessions/{session_id}/history")
    async def get_history(session_id: str) -> list[dict[str, Any]]:
        return service.history(session_id)

    @app.post("/api/sessions/{session_id}/rewind")
    async def rewind(session_id: str, request: RewindRequest) -> dict[str, Any]:
        return service.rewind(session_id, request.entry_id)

    @app.post("/api/sessions/{session_id}/chat/{character_id}")
    async def chat(session_id: str, character_id: str, request: ChatRequest) -> StreamingResponse:
        session, refusal = service.chat_prepare(session_id, character_id)
        state = session.state
        runtime.current_turn = state.turn
        user_text = request.text

        def sse(event: str, payload: dict[str, Any]) -> str:
            return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        async def events() -> AsyncIterator[str]:
            if refusal is not None:
                session.append_message("player", user_text, {"character": character_id})
                session.append_message("character", refusal["content"], {"character": character_id, "status": refusal["status"]})
                service.touch(session_id)
                yield sse("status", {"status": refusal["status"]})
                yield sse("message", {"text": refusal["content"]})
                yield "event: done\ndata: {}\n\n"
                return
            yield sse("status", {"status": "ok"})
            provider = runtime.provider(session.scenario, "minister_chat")
            messages = minister_chat_messages(character_id, state, user_text, session.scenario, session.seed, session.last_report())
            pieces: list[str] = []
            try:
                offline = service.offline_chat_text(session, character_id, user_text, provider)
                if offline is not None:
                    # 离线（mock）：模板答话按小段流出，与在线流式的前端处理一致。
                    for offset in range(0, len(offline), 6):
                        piece = offline[offset:offset + 6]
                        pieces.append(piece)
                        yield sse("message", {"text": piece})
                else:
                    async for piece in provider.stream_text(messages):
                        pieces.append(piece)
                        yield sse("message", {"text": piece})
            except Exception as exc:  # noqa: BLE001 - 流中错误以 SSE error 事件告知前端
                yield sse("error", {"code": "PROVIDER_ERROR", "message": f"模型调用失败：{type(exc).__name__}"})
            content = "".join(pieces)
            session.append_message("player", user_text, {"character": character_id})
            session.append_message("character", content, {"character": character_id, "status": "ok"})
            service.touch(session_id)
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream; charset=utf-8")

    @app.get("/api/assets/{scenario_id}/{asset_id:path}")
    async def get_asset(scenario_id: str, asset_id: str) -> Response:
        data = service.scenario(scenario_id)
        root = Path(data["root"])
        manifest = read_manifest(root)
        entry = next((item for item in manifest.get("entries", []) if item.get("id") == asset_id), None)
        if entry is None:
            return Response(placeholder_for(root, asset_id), media_type="image/svg+xml")
        file_path = asset_path(root, entry["path"])
        if file_path.is_file():
            payload = file_path.read_bytes()
            media_type = "image/svg+xml" if payload.lstrip().startswith(b"<svg") else (
                "image/png" if entry.get("format") == "png" else "image/webp"
            )
            return Response(payload, media_type=media_type)
        return Response(placeholder_for(root, asset_id), media_type="image/svg+xml")

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
            raise GameError("PROVIDER_ERROR", f"模型连接失败：{exc}") from exc
        return {
            "ok": True,
            "tool_calling": bool(result.tool_calls),
            "json_fallback": not bool(result.tool_calls),
            "model": result.model,
        }

    return app


app = create_app()
