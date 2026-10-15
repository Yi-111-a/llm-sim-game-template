from __future__ import annotations

import asyncio
from typing import Any

from simcore.domain.models import WorldState
from simcore.llm.base import ChatResult, LLMProvider


class AgentAborted(RuntimeError):
    pass


async def guarded_chat(
    provider: LLMProvider,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    deadline: float,
    call_timeout: float = 30,
    abort_event: asyncio.Event | None = None,
    response_format: dict[str, Any] | None = None,
) -> ChatResult:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError("Agent total timeout exceeded")
    timeout = min(call_timeout, remaining)
    call = asyncio.create_task(provider.chat(messages, tools=tools, response_format=response_format))
    abort = asyncio.create_task(abort_event.wait()) if abort_event else None
    try:
        tasks = {call, abort} if abort else {call}
        done, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if abort and abort in done:
            raise AgentAborted("Agent request aborted")
        if call not in done:
            raise TimeoutError("Agent provider call timed out")
        return call.result()
    finally:
        for task in (call, abort):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (call, abort) if task), return_exceptions=True)


async def run_agent(
    provider: LLMProvider,
    system_prompt: str,
    user_prompt: str,
    tools: list[dict[str, Any]] | None = None,
    max_steps: int = 12,
    call_timeout: float = 30,
    total_timeout: float = 120,
    abort_event: asyncio.Event | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
    emitted: list[dict[str, Any]] = []
    deadline = asyncio.get_running_loop().time() + total_timeout
    for _ in range(min(max_steps, 12)):
        result = await guarded_chat(provider, messages, tools, deadline, call_timeout, abort_event)
        emitted.append({"type": "assistant", "content": result.content, "tool_calls": [call.name for call in result.tool_calls]})
        if not result.tool_calls:
            return result.content, emitted
        for call in result.tool_calls:
            if call.name in {"finish_turn", "submit_ops"}:
                return result.content, emitted
            messages.append({"role": "assistant", "content": result.content, "tool_call_id": call.id})
            messages.append({"role": "tool", "name": call.name, "content": str(call.arguments)})
    return "已达到本轮工具调用上限。", emitted


def world_summary(state: WorldState) -> dict[str, Any]:
    return {
        "turn": state.turn,
        "calendar": state.calendar.model_dump(mode="json"),
        "resources": {key: value.amount for key, value in state.resources.items()},
        "offices": {key: value.holder for key, value in state.offices.items()},
    }
