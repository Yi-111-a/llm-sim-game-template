from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

from simcore.llm.base import ChatResult, ToolCall, ToolCallingUnsupported, Usage


class MockProvider:
    """Deterministic local provider for the bundled scenario and offline tests."""

    def __init__(self, scenario: dict[str, Any] | None = None, tool_calling: bool = True) -> None:
        self.scenario = scenario or {}
        self.tool_calling = tool_calling

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResult:
        prompt = "\n".join(str(message.get("content", "")) for message in messages)
        if tools and not self.tool_calling:
            raise ToolCallingUnsupported("Mock provider simulates unsupported function tools")
        if response_format:
            operations = [
                op
                for call in self._edict_calls(prompt)
                for op in call.arguments.get("ops", [])
            ]
            return ChatResult(
                content=f"```json\n{json.dumps({'ops': operations}, ensure_ascii=False)}\n```",
                usage=Usage(
                    input_tokens=max(1, len(prompt) // 4),
                    output_tokens=20,
                    total_tokens=max(1, len(prompt) // 4) + 20,
                ),
            )
        if "【廷议发言】" in prompt:
            return ChatResult(
                content=self._debate_line(prompt),
                usage=Usage(input_tokens=max(1, len(prompt) // 4), output_tokens=30, total_tokens=max(1, len(prompt) // 4) + 30),
            )
        if "edicts" in prompt.lower() or "诏令" in prompt or "政策" in prompt:
            calls = self._edict_calls(prompt)
            return ChatResult(
                content="已将诏令拆分为可审计的政策条款。",
                tool_calls=calls,
                usage=Usage(input_tokens=max(1, len(prompt) // 4), output_tokens=24, total_tokens=max(1, len(prompt) // 4) + 24),
            )
        return ChatResult(
            content="臣已记录所议。此答复仅据当前已知的国情作出，不改变国中事实。",
            usage=Usage(input_tokens=max(1, len(prompt) // 4), output_tokens=20, total_tokens=max(1, len(prompt) // 4) + 20),
        )

    async def stream_text(self, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        result = await self.chat(messages)
        for offset in range(0, len(result.content), 8):
            yield result.content[offset:offset + 8]

    @staticmethod
    def _debate_line(prompt: str) -> str:
        """模板台词：只引用提示词中事实摘要的第一条国势，不编造数字。"""
        stance_match = re.search(r"立场：\S+（(support|oppose|neutral)）", prompt)
        stance = stance_match.group(1) if stance_match else "neutral"
        opener = {"support": "臣以为此议可行", "oppose": "臣以为此议不可", "neutral": "臣以为此事宜再详议"}[stance]
        national = next((line for line in prompt.splitlines() if line.startswith("【国势】")), "")
        first = national.removeprefix("【国势】").split("；")[0] if national else ""
        return f"{opener}。今{first}，不可不察。" if first else f"{opener}。"

    def _edict_calls(self, prompt: str) -> list[ToolCall]:
        lines = [line.strip() for line in prompt.splitlines() if line.strip()]
        calls: list[ToolCall] = []
        for index, line in enumerate(lines):
            resource_match = re.search(r"(国库|库银|粮储|粮食|权威|贪腐|民心).{0,10}(?:增加|增|减少|减|拨|赈|整顿)", line)
            if resource_match:
                label = resource_match.group(1)
                resource = {
                    "国库": "treasury",
                    "库银": "treasury",
                    "粮储": "grain",
                    "粮食": "grain",
                    "权威": "authority",
                    "贪腐": "corruption",
                    "民心": "morale",
                }.get(label, label)
                sign = -1 if re.search(r"减少|减|拨|赈", line) else 1
                amount = 5.0
                number = re.search(r"(\d+(?:\.\d+)?)", line)
                if number:
                    amount = float(number.group(1))
                if sign < 0 and label in {"粮储", "粮食"} and ("赈" in line or "救" in line):
                    amount = abs(amount)
                calls.append(ToolCall(id=f"mock-{index}", name="propose_ops", arguments={"ops": [{
                    "op": "adjust_resource", "resource": resource, "delta": sign * amount, "reason": line
                }]}))
                continue
            appoint = re.search(r"任命\s*([\u4e00-\u9fff·]+)\s*(?:为|作)\s*([\u4e00-\u9fff·]+)", line)
            if appoint:
                chars = self.scenario.get("characters", [])
                offices = self.scenario.get("scenario", {}).get("offices", [])
                char = next((row for row in chars if row.get("name") == appoint.group(1)), None)
                office = next((row for row in offices if row.get("name") == appoint.group(2)), None)
                if char and office:
                    calls.append(ToolCall(id=f"mock-{index}", name="propose_ops", arguments={"ops": [{
                        "op": "appoint", "character": char["id"], "office": office["id"]
                    }]}))
        if not calls:
            calls.append(ToolCall(
                id="mock-rejected",
                name="propose_ops",
                arguments={"ops": [{"op": "adjust_resource", "resource": "unknown_resource", "delta": 1, "reason": "无法识别的条款"}]},
            ))
        return calls
