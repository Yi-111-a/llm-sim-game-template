"""Let an external agent CLI (Codex or Claude Code) play a full game through the MCP server and write a report.

    python scripts/agent_play.py codex  --scenario _template --run gpt-01 [--model <model>] [--seed 7] [--difficulty hard]
    python scripts/agent_play.py claude --scenario _template --run claude-01 [--seed 7]

Every run gets its own folder (default playtests/<agent>/<run>/) with:
    home/          LLM_SIM_HOME for this run (saves, drafts, receipts)
    events.jsonl   the agent's JSON event stream (tool calls and results)
    stderr.txt     the agent CLI's stderr
    report.md      the playtest report the agent writes at the end
    summary.json   exit code, seconds, whether a report exists

Lessons from playtesting 《天宝》 on Windows, all handled here:
- Codex: `codex exec` needs `default_tools_approval_mode="approve"` or every MCP call waits for an
  approval nobody gives; `codex mcp add` does not write that key, so pass it with -c.
- Codex: the npm `codex.CMD` shim goes through cmd.exe, whose code page mangles UTF-8 arguments ->
  call `node codex.js` directly and send the (Chinese) prompt on stdin.
- Codex on a Chinese-locale Windows writes its JSON stream in cp936, not UTF-8 -> decode line by line.
- Cold start: a Python MCP server can take tens of seconds while antivirus scans the venv; one GPT run
  silently started without the server. Set `startup_timeout_sec=60` (Codex) / `MCP_TIMEOUT=60000` (Claude Code).
- Claude Code: `--strict-mcp-config` + `--allowedTools mcp__<name>__* Write` keeps the run non-interactive.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

PROMPT = """你要完整地玩一局策略游戏《{title}》。游戏通过名为 {name} 的 MCP 服务提供工具。

规则：
1. 先调用 list_scenarios，再用 new_game 开局（scenario={scenario}，seed={seed}{difficulty_arg}）。记下 session_id。
2. 每一季：调用 briefing 阅读局势；需要时召对大臣（audience：你根据返回的人设卡和事实摘要扮演他回话，数字只能引用工具给出的值）
   或开廷议（court_debate，只有在京者能当廷议）；然后用 draft_edict 下诏（adopted 采纳奏疏选项，cards 用政令卡，ops 直接提出操作，
   op 格式见 op_reference），读回执后 confirm_edict；有待决事件就用 choose 处置；最后 end_turn。
3. 一直玩到游戏给出结局（outcome）为止，不要中途停下，也不要问我。目标：{goals}。
4. 工具报错时，读懂错误说明，修正参数再试；同一个错误连续出现 3 次就换一种做法，并记进报告。
5. 结束后调用 export_log 复盘，再把一份中文试玩报告写到文件 {report}：结局；每季一行的大事记；遇到的所有问题
   （工具报错、看不懂的说明、前后矛盾的数字或文字、卡住的地方、不合理的机制、文字与实际状态不符之处）；改进建议。
   问题请写具体：哪一季、哪个工具、什么参数、返回了什么。
"""


def scenario_info(scenarios_dir: Path, scenario: str) -> tuple[str, str]:
    data = yaml.safe_load((scenarios_dir / scenario / "scenario.yaml").read_text(encoding="utf-8")) or {}
    goals = "；".join(str(row.get("title")) for row in data.get("objectives") or [] if isinstance(row, dict)) or "取得最好的结局"
    return str(data.get("title") or scenario), goals


def mcp_env(home: Path, scenarios_dir: Path) -> dict[str, str]:
    return {
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "SIM_SCENARIOS_DIR": str(scenarios_dir),
        "LLM_SIM_HOME": str(home),
        "SIM_MCP_MODE": "client",
    }


def codex_command(args: argparse.Namespace, run_dir: Path, env: dict[str, str]) -> list[str]:
    server = f"mcp_servers.{args.name}"
    codex_js = Path(os.environ.get("CODEX_JS") or Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js")
    if sys.platform == "win32" and codex_js.is_file():
        base = [shutil.which("node") or "node", str(codex_js)]   # bypass codex.CMD (cmd.exe mangles UTF-8)
    else:
        base = [shutil.which("codex") or "codex"]
    command = [
        *base, "exec", "--skip-git-repo-check", "--json", "-C", str(run_dir), "-s", "workspace-write",
        "-c", f"{server}.command={json.dumps(args.python)}",
        "-c", f'{server}.args=["-m","cli.main","mcp"]',
        "-c", f"{server}.cwd={json.dumps(str(ROOT / 'backend'))}",
        "-c", f"{server}.env={{{', '.join(f'{key}={json.dumps(value)}' for key, value in env.items())}}}",
        "-c", f"{server}.startup_timeout_sec=60",
        "-c", f"{server}.tool_timeout_sec=120",
        "-c", f'{server}.default_tools_approval_mode="approve"',
    ]
    if sys.platform == "win32":
        command += ["-c", 'windows.sandbox="unelevated"']
    if args.model:
        command += ["-m", args.model]
    return command   # the prompt goes on stdin


def claude_command(args: argparse.Namespace, run_dir: Path, env: dict[str, str], prompt: str) -> list[str]:
    config = {"mcpServers": {args.name: {"command": args.python, "args": ["-m", "cli.main", "mcp"], "cwd": str(ROOT / "backend"), "env": env}}}
    config_path = run_dir / "mcp.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    command = [
        os.environ.get("CLAUDE_CLI") or shutil.which("claude") or "claude",
        "-p", prompt, "--output-format", "stream-json", "--verbose",
        "--mcp-config", str(config_path), "--strict-mcp-config",
        "--allowedTools", f"mcp__{args.name}__*", "Write",
        "--max-turns", str(args.max_turns),
    ]
    if args.model:
        command += ["--model", args.model]
    return command


def decode(data: bytes) -> str:
    """UTF-8 per line, falling back to cp936 (Codex on Chinese-locale Windows)."""
    lines = []
    for raw in data.splitlines():
        try:
            lines.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            lines.append(raw.decode("cp936", errors="replace"))
    return "\n".join(lines) + ("\n" if lines else "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Let Codex / Claude Code play a full game over MCP")
    parser.add_argument("agent", choices=["codex", "claude"])
    parser.add_argument("--run", required=True, help="运行名，如 gpt-01")
    parser.add_argument("--scenario", default="_template")
    parser.add_argument("--scenarios-dir", default=str(ROOT / "scenarios"))
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--difficulty", default="")
    parser.add_argument("--model", default="", help="模型名（缺省用该 CLI 的默认模型）")
    parser.add_argument("--name", default="llm-sim", help="MCP 服务名（工具名前缀 mcp__<name>__）")
    parser.add_argument("--python", default=sys.executable, help="装有引擎的 Python（venv）")
    parser.add_argument("--out", default="", help="输出目录（缺省 playtests/<agent>/<run>）")
    parser.add_argument("--max-turns", type=int, default=400, help="Claude Code 的最大轮数")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    scenarios_dir = Path(args.scenarios_dir).resolve()
    run_dir = Path(args.out).resolve() if args.out else ROOT / "playtests" / args.agent / args.run
    (run_dir / "home").mkdir(parents=True, exist_ok=True)
    report = run_dir / "report.md"
    title, goals = scenario_info(scenarios_dir, args.scenario)
    prompt = PROMPT.format(
        title=title, name=args.name, scenario=args.scenario, seed=args.seed, goals=goals, report=report,
        difficulty_arg=f"，difficulty={args.difficulty}" if args.difficulty else "",
    )
    env = mcp_env(run_dir / "home", scenarios_dir)
    command = codex_command(args, run_dir, env) if args.agent == "codex" else claude_command(args, run_dir, env, prompt)
    started = time.time()
    proc = subprocess.run(
        command,
        cwd=run_dir,
        input=prompt.encode("utf-8") if args.agent == "codex" else None,
        capture_output=True,
        env={**os.environ, "MCP_TIMEOUT": os.environ.get("MCP_TIMEOUT", "60000")},
    )
    (run_dir / "events.jsonl").write_text(decode(proc.stdout), encoding="utf-8")
    (run_dir / "stderr.txt").write_text(decode(proc.stderr), encoding="utf-8")
    summary = {"agent": args.agent, "run": args.run, "scenario": args.scenario, "seed": args.seed, "exit": proc.returncode,
               "seconds": round(time.time() - started), "report": report.exists()}
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
