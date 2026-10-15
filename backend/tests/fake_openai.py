"""仿 OpenAI Chat Completions 服务器（线程内 uvicorn），供 OpenAICompatProvider 端到端测试。

支持：非流式、SSE 流式、tool_calls（含流式分片）、JSON 模式（response_format）、
「不支持工具」的 400 错误、可配置延迟（用于超时/中止测试）。

用法：
    with FakeOpenAI() as fake:
        fake.script.append({"tool_calls": [{"name": "propose_ops", "arguments": {...}}]})
        provider = OpenAICompatProvider(fake.base_url, "test-key", "fake-model")
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


class FakeOpenAI:
    def __init__(self, tools_supported: bool = True) -> None:
        self.tools_supported = tools_supported
        self.script: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.default_content = "臣谨奏：一切如常。"
        self.app = self._build_app()
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> FakeOpenAI:
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error", lifespan="off")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("fake OpenAI server failed to start")
            time.sleep(0.02)
        return self

    def __exit__(self, *_: Any) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _next(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.script:
            return self.script.pop(0)
        if body.get("response_format", {}).get("type") == "json_object":
            return {"content": json.dumps({"ops": []}, ensure_ascii=False)}
        return {"content": self.default_content}

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def completions(request: Request) -> Any:
            body = await request.json()
            self.requests.append(body)
            self.headers.append(dict(request.headers))
            if body.get("tools") and not self.tools_supported:
                error = {"message": "tools / tool_choice is not supported by this model", "type": "invalid_request_error"}
                return JSONResponse({"error": error}, status_code=400)
            item = self._next(body)
            if item.get("delay"):
                await asyncio.sleep(float(item["delay"]))
            if item.get("status"):
                return JSONResponse({"error": {"message": item.get("error", "error")}}, status_code=int(item["status"]))
            calls = [
                {
                    "id": f"call_{index}",
                    "type": "function",
                    "function": {"name": call["name"], "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False)},
                }
                for index, call in enumerate(item.get("tool_calls", []))
            ]
            content = item.get("content", "" if calls else self.default_content)
            usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
            if body.get("stream"):
                return StreamingResponse(_stream(content, calls, usage, item.get("chunk", 4)), media_type="text/event-stream")
            message: dict[str, Any] = {"role": "assistant", "content": content or None}
            if calls:
                message["tool_calls"] = calls
            return JSONResponse({
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "model": body.get("model", "fake-model"),
                "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}],
                "usage": usage,
            })

        return app


async def _stream(content: str, calls: list[dict[str, Any]], usage: dict[str, int], chunk: int) -> AsyncIterator[str]:
    def frame(delta: dict[str, Any], extra: dict[str, Any] | None = None) -> str:
        payload = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": "fake-model", "choices": [{"index": 0, "delta": delta}]}
        payload.update(extra or {})
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield frame({"role": "assistant"})
    for offset in range(0, len(content), chunk):
        yield frame({"content": content[offset:offset + chunk]})
        await asyncio.sleep(0)
    for index, call in enumerate(calls):
        arguments = call["function"]["arguments"]
        yield frame({"tool_calls": [{"index": index, "id": call["id"], "function": {"name": call["function"]["name"], "arguments": ""}}]})
        for offset in range(0, len(arguments), 5):
            yield frame({"tool_calls": [{"index": index, "function": {"arguments": arguments[offset:offset + 5]}}]})
    yield f"data: {json.dumps({'choices': [], 'usage': usage})}\n\n"
    yield "data: [DONE]\n\n"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
