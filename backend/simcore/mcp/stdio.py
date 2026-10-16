"""轻量 stdio MCP 服务：先秒回 initialize / tools/list，再在后台预热引擎。

官方 MCP SDK 本身就要导入约 600 个模块（starlette、uvicorn、httpx、opentelemetry……），加上服务层，
在杀毒软件逐个扫描 .py 的 Windows 机器上冷启动可达十几到几十秒，超出 Codex 默认的 MCP 启动超时
（gpt-09：Codex 根本没挂上 tianbao 工具）。这里只用标准库实现 MCP 的 stdio 子集
（initialize、ping、tools/list、tools/call，以 JSON-RPC 换行分隔），工具元数据来自 simcore.mcp.meta；
initialize 应答之后在后台线程导入服务层并建好 GameService，首个 tools/call 时若未预热完就等它。

工具实现与 SDK 版（simcore.mcp.server.build_server）共用 make_tools，返回文本完全相同。
环境变量 SIM_MCP_SDK=1 时改用 SDK 版 stdio 传输（排查兼容问题用）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any, BinaryIO

from simcore.mcp.meta import INSTRUCTIONS, REQUIRED, TOOL_PARAMS, tool_list
from simcore.version import ENGINE_VERSION

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

_KIND_CHECKS: dict[str, tuple[type, ...]] = {
    "str": (str,),
    "int": (int,),
    "bool": (bool,),
    "list[str]": (list,),
    "dict[str,str]": (dict,),
    "list[dict[str,str]]": (list,),
    "list[dict]": (list,),
}
_KIND_LABELS = {"str": "字符串", "int": "整数", "bool": "布尔值", "dict[str,str]": "对象"}


def check_arguments(tool: str, arguments: Any) -> tuple[dict[str, Any], str | None]:
    """按 TOOL_PARAMS 校验并整理参数；返回 (参数, 错误说明)。未知参数忽略（与 SDK 一致）。"""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return {}, "arguments 应为对象"
    result: dict[str, Any] = {}
    for name, kind, default in TOOL_PARAMS[tool]:
        if name not in arguments:
            if default is REQUIRED:
                return {}, f"缺少必填参数 {name}"
            continue
        value = arguments[name]
        base = kind.removesuffix("|None")
        if value is None:
            if kind.endswith("|None"):
                result[name] = None
                continue
            return {}, f"参数 {name} 不能为 null"
        if base == "int" and isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value)
        if base == "int" and isinstance(value, float) and value.is_integer():
            value = int(value)
        valid = isinstance(value, _KIND_CHECKS[base]) and not (base == "int" and isinstance(value, bool))
        if not valid:
            label = _KIND_LABELS.get(base, "数组")
            return {}, f"参数 {name} 应为{label}"
        result[name] = value
    return result, None


class Engine:
    """延迟加载的工具集：导入服务层、建 GameService 都推迟到 initialize 之后（后台线程）或首个工具调用。"""

    def __init__(self, mode: str | None = None) -> None:
        self.mode = mode
        self._tools: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._loop: Any = None

    def warm_up(self) -> None:
        def run() -> None:
            try:
                self.tools()
            except Exception:  # noqa: BLE001 - 预热失败留给首个工具调用报错
                pass

        threading.Thread(target=run, name="tianbao-warmup", daemon=True).start()

    def tools(self) -> dict[str, Any]:
        with self._lock:
            if self._tools is None:
                from simcore.mcp.server import make_tools

                self._tools = make_tools(mode=self.mode)
            return self._tools

    def call(self, name: str, arguments: Any) -> tuple[str, bool]:
        if name not in TOOL_PARAMS:
            return f"错误 [UNKNOWN_TOOL]：没有名为 {name} 的工具；可用：{'、'.join(TOOL_PARAMS)}", True
        kwargs, problem = check_arguments(name, arguments)
        if problem:
            return f"错误 [INVALID_REQUEST]：{name} 的{problem}", True
        try:
            tools = self.tools()
            import asyncio

            if self._loop is None:
                self._loop = asyncio.new_event_loop()
            return str(self._loop.run_until_complete(tools[name](**kwargs))), False
        except Exception as exc:  # noqa: BLE001 - 工具错误一律转为可读说明
            from simcore.mcp.server import tool_error_text

            return tool_error_text(exc), True


def _result(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def handle(engine: Engine, message: Any) -> dict[str, Any] | None:
    """处理一条 JSON-RPC 消息；通知与客户端的应答返回 None。"""
    if not isinstance(message, dict) or "method" not in message:
        return None if isinstance(message, dict) else _error(None, -32600, "Invalid Request")
    method = message.get("method")
    if "id" not in message:
        return None  # 通知：notifications/initialized、notifications/cancelled 等
    message_id = message["id"]
    params = message.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _result(
            message_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "tianbao", "version": ENGINE_VERSION},
                "instructions": INSTRUCTIONS,
            },
        )
    if method == "ping" or method == "logging/setLevel":
        return _result(message_id, {})
    if method == "tools/list":
        return _result(message_id, {"tools": tool_list()})
    if method == "tools/call":
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _error(message_id, -32602, "tools/call 需要 name")
        text, is_error = engine.call(params["name"], params.get("arguments"))
        return _result(message_id, {"content": [{"type": "text", "text": text}], "isError": is_error})
    if method in {"resources/list", "resources/templates/list", "prompts/list"}:
        key = {"resources/list": "resources", "resources/templates/list": "resourceTemplates", "prompts/list": "prompts"}[method]
        return _result(message_id, {key: []})
    return _error(message_id, -32601, f"Method not found: {method}")


def serve(engine: Engine, reader: BinaryIO, writer: BinaryIO) -> None:
    """逐行读 JSON-RPC 消息并应答，直到 stdin 关闭。"""

    def send(payload: Any) -> None:
        writer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        writer.flush()

    warmed = False
    for raw in reader:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            send(_error(None, -32700, "Parse error"))
            continue
        batch = message if isinstance(message, list) else [message]
        replies = [reply for reply in (handle(engine, item) for item in batch) if reply is not None]
        if isinstance(message, list):
            if replies:
                send(replies)
        else:
            for reply in replies:
                send(reply)
        if not warmed and any(isinstance(item, dict) and item.get("method") == "initialize" for item in batch):
            warmed = True
            engine.warm_up()


def run(mode: str | None = None) -> None:
    if os.environ.get("SIM_MCP_SDK", "").strip() in {"1", "true", "yes"}:
        from simcore.mcp.server import build_server

        build_server(mode=mode).run("stdio")
        return
    protocol_out = sys.stdout.buffer
    sys.stdout = sys.stderr  # 引擎里任何零星 print 都不能混进协议流
    serve(Engine(mode), sys.stdin.buffer, protocol_out)
