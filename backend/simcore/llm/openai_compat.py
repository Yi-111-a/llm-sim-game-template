from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

import httpx

from simcore.llm.base import ChatResult, ToolCall, ToolCallingUnsupported, Usage


class OpenAICompatProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 90,
        extra_headers: dict[str, str] | None = None,
        params_override: dict[str, Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.extra_headers = extra_headers or {}
        self.params_override = params_override or {}

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResult:
        body = self.request_body(messages, tools, stream, response_format)
        headers = dict(self.extra_headers)
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if stream:
                return await self._stream_chat(client, body, headers)
            try:
                response = await client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if tools and exc.response.status_code in {400, 404, 422} and _mentions_tools(exc.response.text):
                    raise ToolCallingUnsupported(exc.response.text) from exc
                raise
        payload = response.json()
        choice = payload["choices"][0]["message"]
        calls = [
            ToolCall(
                id=call.get("id", ""),
                name=call["function"]["name"],
                arguments=_parse_arguments(call["function"].get("arguments", "{}")),
            )
            for call in choice.get("tool_calls", [])
        ]
        raw_usage = payload.get("usage", {})
        input_tokens = int(raw_usage.get("prompt_tokens", 0))
        output_tokens = int(raw_usage.get("completion_tokens", 0))
        return ChatResult(
            content=choice.get("content") or "",
            tool_calls=calls,
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=int(raw_usage.get("total_tokens", input_tokens + output_tokens)),
            ),
            model=payload.get("model", self.model),
        )

    async def stream_text(self, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        """以 SSE 流式请求，逐段产出文本增量。"""
        body = self.request_body(messages, None, True, None)
        headers = dict(self.extra_headers)
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", f"{self.base_url}/chat/completions", json=body, headers=headers) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    chunk = json.loads(raw)
                    for choice in chunk.get("choices", []):
                        text = (choice.get("delta") or {}).get("content")
                        if text:
                            yield text

    def request_body(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        stream: bool,
        response_format: dict[str, Any] | None,
    ) -> dict[str, Any]:
        body = {
            key: value for key, value in self.params_override.items()
            if key not in {"messages", "model", "tools", "stream", "tool_choice", "response_format"}
        }
        body.update({"model": self.model, "messages": messages, "stream": stream})
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "required"
        if response_format:
            body["response_format"] = response_format
        return body

    async def _stream_chat(
        self, client: httpx.AsyncClient, body: dict[str, Any], headers: dict[str, str]
    ) -> ChatResult:
        content: list[str] = []
        calls: dict[int, dict[str, str]] = defaultdict(lambda: {"id": "", "name": "", "arguments": ""})
        usage: dict[str, Any] = {}
        model = self.model
        async with client.stream("POST", f"{self.base_url}/chat/completions", json=body, headers=headers) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                chunk = json.loads(raw)
                model = chunk.get("model", model)
                usage = chunk.get("usage", usage)
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta", {})
                    if delta.get("content"):
                        content.append(delta["content"])
                    for call in delta.get("tool_calls", []):
                        target = calls[int(call["index"])]
                        target["id"] += call.get("id", "")
                        function = call.get("function", {})
                        target["name"] += function.get("name", "")
                        target["arguments"] += function.get("arguments", "")
        tool_calls = [
            ToolCall(
                id=value["id"],
                name=value["name"],
                arguments=_parse_arguments(value["arguments"] or "{}"),
            )
            for _, value in sorted(calls.items())
        ]
        input_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))
        return ChatResult(
            content="".join(content),
            tool_calls=tool_calls,
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=int(usage.get("total_tokens", input_tokens + output_tokens)),
            ),
            model=model,
        )


def _parse_arguments(value: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _mentions_tools(message: str) -> bool:
    lowered = message.lower()
    return any(term in lowered for term in ("tool", "function", "tool_choice", "工具"))
