"""本机已登录的编程助手 CLI（Claude Code / Codex）充当模型：不需要 API key，用的是本机账号。

provider 名为 ``claude_cli`` / ``codex_cli``（见 presets.yaml）。每次 chat 起一个无头子进程：
- 在空的临时目录里运行，不读项目文件、CLAUDE.md / AGENTS.md；
- 关掉 CLI 自带的工具与 MCP（否则它会去连本游戏自己的 MCP 服务）；
- 提示词经 stdin 传入，回复按 UTF-8 读取（Windows 控制台代码页不影响）。

CLI 没有原生的函数调用：带 tools 的请求抛 ToolCallingUnsupported，调用方（诏令解析）退回「只返回 JSON」路径。
Claude 支持 stream-json，召对答话可以逐段流出；Codex 一次给出整段。
"""
from __future__ import annotations

import asyncio
import contextlib
import glob
import json
import os
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from simcore.llm.base import ChatResult, ToolCallingUnsupported, Usage

CLI_KINDS = {"claude_cli": "claude", "codex_cli": "codex"}
# 子进程冷启动加一次模型回复常在 5–40 秒；调用方的单次超时不低于此值（见 agent.loop.guarded_chat）
MIN_CALL_TIMEOUT = 120.0


class CliAgentUnavailable(RuntimeError):
    """找不到 CLI，或 CLI 未登录 / 返回错误。消息是给玩家看的中文说明。"""


def _windows_glob_newest(pattern: str) -> str | None:
    found = sorted(glob.glob(pattern), key=lambda path: os.path.getmtime(path), reverse=True)
    return found[0] if found else None


def find_claude() -> list[str] | None:
    """Claude Code CLI：SIM_CLAUDE_CLI → PATH → ~/.local/bin → Claude 桌面版自带（取最新）。"""
    explicit = os.environ.get("SIM_CLAUDE_CLI")
    if explicit:
        return [explicit]
    on_path = shutil.which("claude")
    if on_path:
        return [on_path]
    local = Path.home() / ".local" / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
    if local.exists():
        return [str(local)]
    # Claude 桌面版自带的 CLI。桌面版是 MSIX 打包应用：它自己看到的 %APPDATA% 被重定向到
    # %LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming；从外面（普通终端、天宝安装版）只能在后者找到。
    patterns = []
    if os.environ.get("APPDATA"):
        patterns.append(os.path.join(os.environ["APPDATA"], "Claude", "claude-code", "*", "*", "claude.exe"))
    if os.environ.get("LOCALAPPDATA"):
        patterns.append(os.path.join(
            os.environ["LOCALAPPDATA"], "Packages", "Claude_*", "LocalCache", "Roaming", "Claude", "claude-code", "*", "*", "claude.exe"
        ))
    for pattern in patterns:
        bundled = _windows_glob_newest(pattern)
        if bundled:
            return [bundled]
    return None


def find_codex() -> list[str] | None:
    """Codex CLI：SIM_CODEX_CLI → npm 全局安装的 codex.js（用 node 直接跑，绕开 .cmd 包装）→ PATH。"""
    explicit = os.environ.get("SIM_CODEX_CLI")
    if explicit:
        return [explicit]
    node = shutil.which("node")
    appdata = os.environ.get("APPDATA")
    if node and appdata:
        script = Path(appdata) / "npm" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        if script.exists():
            return [node, str(script)]
    on_path = shutil.which("codex")
    if on_path and not on_path.lower().endswith((".cmd", ".bat")):
        return [on_path]
    return None


def render_prompt(messages: list[dict[str, Any]], response_format: dict[str, Any] | None) -> tuple[str, str]:
    """OpenAI 风格的 messages → (system, prompt)。多轮对话按「上文」顺序平铺；只要 JSON 时末尾加说明。"""
    system_parts = [str(message.get("content") or "") for message in messages if message.get("role") == "system"]
    turns = [message for message in messages if message.get("role") != "system"]
    lines: list[str] = []
    for message in turns[:-1]:
        speaker = "玩家" if message.get("role") == "user" else "你（上一轮）"
        lines.append(f"【{speaker}】\n{message.get('content') or ''}")
    last = str(turns[-1].get("content") or "") if turns else ""
    prompt = ("以下是此前的往来，供你接续：\n\n" + "\n\n".join(lines) + "\n\n——本轮——\n" + last) if lines else last
    if response_format and response_format.get("type") in {"json_object", "json_schema"}:
        prompt += "\n\n只输出一个 JSON 对象，不要代码块标记，不要任何解释。"
    system = "\n\n".join(part for part in system_parts if part.strip())
    guard = "你是一款历史模拟游戏的文字后端，只按要求写正文。不要使用任何工具，不要读写文件，不要提及你是程序或模型。"
    return (f"{system}\n\n{guard}" if system else guard), prompt


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        return body.strip()
    return stripped


class CliAgentProvider:
    """``kind`` 为 claude / codex；``model`` 为 "default" 时跟随 CLI 自身的当前模型。"""

    min_call_timeout = MIN_CALL_TIMEOUT

    def __init__(self, kind: str, model: str = "default", timeout: float = 180) -> None:
        if kind not in {"claude", "codex"}:
            raise ValueError(f"unknown CLI kind: {kind}")
        self.kind = kind
        self.model = model or "default"
        self.timeout = timeout

    # ------------------------------------------------------------------ public
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        response_format: dict[str, Any] | None = None,
    ) -> ChatResult:
        if tools:
            raise ToolCallingUnsupported(f"{self.kind} CLI 没有原生函数调用，请改用只返回 JSON 的路径")
        system, prompt = render_prompt(messages, response_format)
        text = await (self._claude(system, prompt) if self.kind == "claude" else self._codex(system, prompt))
        text = strip_thinking(text)
        if response_format:
            text = _strip_fence(text)
        return ChatResult(
            content=text,
            usage=Usage(input_tokens=max(1, len(system + prompt) // 4), output_tokens=max(1, len(text) // 4)),
            model=self.label,
        )

    async def stream_text(self, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        system, prompt = render_prompt(messages, None)
        if self.kind != "claude":
            yield await self._codex(system, prompt)
            return
        gate = ThinkingFilter()
        async for piece in self._claude_stream(system, prompt):
            visible = gate.feed(piece)
            if visible:
                yield visible
        tail = gate.flush()
        if tail:
            yield tail

    @property
    def label(self) -> str:
        name = "Claude（本机）" if self.kind == "claude" else "Codex（本机）"
        return name if self.model == "default" else f"{name} · {self.model}"

    async def check(self) -> str:
        """连通性检查：返回一句话的状态；失败抛 CliAgentUnavailable。"""
        reply = await self.chat([{"role": "user", "content": "只回复两个字：在此。"}])
        return f"{self.label} 已就绪：{reply.content.strip()[:20]}"

    # ------------------------------------------------------------------ claude
    def _claude_args(self, system: str, streaming: bool) -> list[str]:
        exe = find_claude()
        if not exe:
            raise CliAgentUnavailable("找不到 Claude Code CLI：请安装 Claude Code，或设置环境变量 SIM_CLAUDE_CLI 指向 claude 可执行文件。")
        args = [
            *exe, "-p",
            "--output-format", "stream-json" if streaming else "json",
            "--tools", "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--system-prompt", system,
        ]
        if streaming:
            args += ["--verbose", "--include-partial-messages"]
        if self.model != "default":
            args += ["--model", self.model]
        return args

    async def _claude(self, system: str, prompt: str) -> str:
        out = await self._run(self._claude_args(system, False), prompt)
        try:
            payload = json.loads(out)
        except json.JSONDecodeError as exc:
            raise CliAgentUnavailable(f"Claude CLI 返回无法解析：{out[:200]}") from exc
        result = str(payload.get("result") or "")
        if payload.get("is_error"):
            raise CliAgentUnavailable(_claude_error(result))
        return result

    async def _claude_stream(self, system: str, prompt: str) -> AsyncIterator[str]:
        with tempfile.TemporaryDirectory(prefix="tianbao-cli-") as cwd:
            proc = await asyncio.create_subprocess_exec(
                *self._claude_args(system, True),
                cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=_child_env(),
            )
            assert proc.stdin and proc.stdout
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            emitted = False
            final: dict[str, Any] = {}
            try:
                async with asyncio.timeout(self.timeout):
                    async for raw in proc.stdout:
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if event.get("type") == "stream_event":
                            delta = (event.get("event") or {}).get("delta") or {}
                            if delta.get("type") == "text_delta" and delta.get("text"):
                                emitted = True
                                yield delta["text"]
                        elif event.get("type") == "result":
                            final = event
                    await proc.wait()
            finally:
                if proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
            if final.get("is_error"):
                raise CliAgentUnavailable(_claude_error(str(final.get("result") or "")))
            if not emitted and final.get("result"):
                yield str(final["result"])

    # ------------------------------------------------------------------ codex
    async def _codex(self, system: str, prompt: str) -> str:
        exe = find_codex()
        if not exe:
            raise CliAgentUnavailable("找不到 Codex CLI：请先 npm i -g @openai/codex 并 codex login，或设置 SIM_CODEX_CLI。")
        with tempfile.TemporaryDirectory(prefix="tianbao-cli-") as cwd:
            last = Path(cwd) / "last.txt"
            args = [
                *exe, "exec",
                "--skip-git-repo-check", "--ephemeral", "--ignore-rules",
                "--sandbox", "read-only", "--color", "never",
                "-c", 'approval_policy="never"',
                "-c", "mcp_servers={}",
                "-o", str(last),
            ]
            if self.model != "default":
                args += ["-m", self.model]
            await self._run([*args, "-"], f"{system}\n\n{prompt}", cwd=cwd)
            if not last.exists():
                raise CliAgentUnavailable("Codex CLI 没有返回内容：请确认已运行 codex login。")
            return last.read_text(encoding="utf-8").strip()

    # ------------------------------------------------------------------ process
    async def _run(self, args: list[str], stdin_text: str, cwd: str | None = None) -> str:
        own_dir = None if cwd else tempfile.TemporaryDirectory(prefix="tianbao-cli-")
        workdir = cwd or (own_dir.name if own_dir else None)
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args, cwd=workdir,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env=_child_env(),
                )
            except FileNotFoundError as exc:
                raise CliAgentUnavailable(f"无法启动 {self.kind} CLI：{exc}") from exc
            try:
                out, err = await asyncio.wait_for(proc.communicate(stdin_text.encode("utf-8")), timeout=self.timeout)
            except TimeoutError as exc:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                raise CliAgentUnavailable(f"{self.kind} CLI 超过 {int(self.timeout)} 秒未回复") from exc
            text = out.decode("utf-8", errors="replace")
            if proc.returncode != 0 and self.kind == "codex":
                tail = err.decode("utf-8", errors="replace").strip().splitlines()[-3:]
                raise CliAgentUnavailable("Codex CLI 出错：" + " / ".join(tail) if tail else "Codex CLI 出错")
            return text
        finally:
            if own_dir:
                own_dir.cleanup()


def _claude_error(result: str) -> str:
    if "login" in result.lower() or "logged in" in result.lower():
        return "Claude CLI 未登录：在终端运行 claude auth login（或 Claude 桌面版自带的 claude.exe auth login）后重试。"
    return f"Claude CLI 出错：{result[:200]}"


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    # 游戏自身的环境变量不传给 CLI，免得它的工具链误连本游戏
    for key in ("SIM_MCP_MODE", "SIM_MCP_NAME"):
        env.pop(key, None)
    return env


_THINK_OPEN = ("<thinking>", "<think>")
_THINK_CLOSE = {"<thinking>": "</thinking>", "<think>": "</think>"}


def strip_thinking(text: str) -> str:
    """去掉模型写进正文里的 <thinking>…</thinking>（或 <think>…</think>）段：这些是它的草稿，不能给玩家看。"""
    gate = ThinkingFilter()
    return (gate.feed(text) + gate.flush()).strip()


class ThinkingFilter:
    """流式过滤 <thinking> 段：标签可能被切在两块之间，故末尾留一小段待下一块拼上再判断。"""

    def __init__(self) -> None:
        self.buffer = ""
        self.closing: str | None = None

    def feed(self, piece: str) -> str:
        self.buffer += piece
        out: list[str] = []
        while self.buffer:
            if self.closing:
                end = self.buffer.find(self.closing)
                if end < 0:
                    keep = len(self.closing) - 1
                    self.buffer = self.buffer[-keep:] if keep else ""
                    break
                self.buffer = self.buffer[end + len(self.closing):]
                self.closing = None
                continue
            starts = [(self.buffer.find(tag), tag) for tag in _THINK_OPEN if self.buffer.find(tag) >= 0]
            if starts:
                index, tag = min(starts)
                out.append(self.buffer[:index])
                self.buffer = self.buffer[index + len(tag):]
                self.closing = _THINK_CLOSE[tag]
                continue
            hold = max((len(tag) - 1 for tag in _THINK_OPEN), default=0)
            cut = len(self.buffer)
            for size in range(min(hold, len(self.buffer)), 0, -1):
                if any(tag.startswith(self.buffer[-size:]) for tag in _THINK_OPEN):
                    cut = len(self.buffer) - size
                    break
            out.append(self.buffer[:cut])
            self.buffer = self.buffer[cut:]
            break
        return "".join(out)

    def flush(self) -> str:
        rest = "" if self.closing else self.buffer
        self.buffer = ""
        return rest
