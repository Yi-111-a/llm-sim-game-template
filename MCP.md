# MCP 接入

引擎自带一个 stdio MCP 服务（`simcore/mcp/server.py`，基于官方 `mcp` Python SDK 2.x 的 `MCPServer`，即 v1 的 FastMCP），外部模型可以直接游玩任何场景。

## 启动命令

```bash
# 已安装（pip install -e backend）后，任意目录均可：
<venv>/Scripts/python -m cli.main mcp          # Windows
<venv>/bin/python -m cli.main mcp              # macOS / Linux
sim mcp                                        # 等价的控制台脚本
```

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `SIM_SCENARIOS_DIR` | 场景目录（每个子目录一个场景） | `engine/scenarios` |
| `LLM_SIM_HOME` | 存档与设置目录，与前端 / 桌面应用共用 | `~/.llm-sim` |
| `SIM_MCP_MODE` | `client`：调用方模型扮演人物、直接给出 ops；`server`：服务端配置的 provider 解释诏令文本、写召对与廷议台词 | `client` |

也可用 `sim mcp --mode server` 覆盖模式。服务只写 stdout 的 JSON-RPC，日志走 stderr。

冷启动：`sim mcp` / `python -m cli.main mcp` 走 `simcore/mcp/stdio.py` 的轻量 stdio 实现（只用标准库），
先应答 `initialize` / `tools/list`（工具表与 JSON Schema 在 `simcore/mcp/meta.py`，测试保证与 SDK 生成的一致），
再在后台线程加载引擎；首个工具调用若引擎未加载完就等它。本机实测握手由约 6–80 秒（官方 SDK + 服务层全量导入，
视杀毒扫描而定）降到约 0.7 秒。仍建议 Codex 配置 `startup_timeout_sec = 60`。排查兼容问题可设 `SIM_MCP_SDK=1` 改用官方 SDK 的 stdio 传输。

## Claude Code

```bash
claude mcp add llm-sim   -e SIM_SCENARIOS_DIR="/path/to/llm-sim-game-template/scenarios"   -e LLM_SIM_HOME="$HOME/.llm-sim"   -- "/path/to/backend/.venv/bin/python" -m cli.main mcp
```

Windows 下 python 写 `C:/.../backend/.venv/Scripts/python.exe`。加 `--scope user` 则所有项目可用。在 Claude Code 中运行 `/mcp` 确认 `llm-sim` 已连接，然后直接说「用 llm-sim 开一局 _template，种子 1，玩到结局」。工具名形如 `mcp__llm-sim__briefing`。

无人值守（`claude -p`）时用 `--mcp-config <json> --strict-mcp-config --allowedTools "mcp__llm-sim__*"`，并设环境变量 `MCP_TIMEOUT=60000` 放宽启动超时；见 `scripts/agent_play.py`。

## Codex CLI

命令行添加：

```bash
codex mcp add llm-sim   --env SIM_SCENARIOS_DIR="/path/to/llm-sim-game-template/scenarios"   --env LLM_SIM_HOME="$HOME/.llm-sim"   -- "/path/to/backend/.venv/bin/python" -m cli.main mcp
```

`codex mcp add` 不写超时与审批模式，请在 `~/.codex/config.toml` 中补上（或整段手写）：

```toml
[mcp_servers.llm-sim]
command = "/path/to/backend/.venv/bin/python"
args = ["-m", "cli.main", "mcp"]
startup_timeout_sec = 60                  # Python 冷启动 + 杀毒扫描可能超过默认值；超时后服务会悄悄挂不上
tool_timeout_sec = 120
default_tools_approval_mode = "approve"   # codex exec 非交互运行时必需，否则每次工具调用都在等审批

[mcp_servers.llm-sim.env]
SIM_SCENARIOS_DIR = "/path/to/llm-sim-game-template/scenarios"
LLM_SIM_HOME = "/home/<you>/.llm-sim"
SIM_MCP_MODE = "client"
```

Windows 经验（《天宝》试玩中踩过的坑）：npm 的 `codex.CMD` 经 cmd.exe 转发会弄乱 UTF-8 参数——无人值守时直接 `node <npm 全局目录>/@openai/codex/bin/codex.js exec …` 并把提示词经 stdin 传入；中文 Windows 上 Codex 的 JSON 事件流是 cp936 编码，按行解码。`scripts/agent_play.py` 已处理这些。

运行中的后端另有 `GET /api/integration`，给出本机现成的接入命令（服务名缺省 `llm-sim`，可用 `SIM_MCP_NAME` 改）。

## 工具

| 工具 | 入参 | 说明 |
|---|---|---|
| `list_scenarios` | — | 场景列表 |
| `new_game` | `scenario, seed, difficulty?` | 返回 `session_id` 与开局简报 |
| `list_saves` / `load_game` | — / `session_id` | 存档列表 / 读档并给简报 |
| `briefing` | `session_id` | 历法、目标、公开资源（隐藏资源只给区间）、危机计及分项、奏疏（含选项与 ops 摘要）、待决事件、上季事实，末尾附扮演与数字规则 |
| `op_reference` | `session_id` | 全部 StateOp 的 JSON Schema 与当前实体 id 表 |
| `audience` | `session_id, character, question?` | client：人设卡 + 该人物可知的 StateDigest + 记忆 + 口吻要求；server：带 question 时返回回答。死者、系狱者拒绝 |
| `court_debate` | `session_id, topic, participants, memorial_id?, option_id?, written?` | 引擎给定的立场 + 人设（client），或各人台词（server）；participants 须在京（`NOT_PRESENT`），written 递书面议状、下季入谏言库 |
| `policy_cards` | `session_id` | 政令卡：类别、可用状态、代价提示、各参数按局势过滤的可选值 |
| `save_advice` | `session_id, character, text` | 存入谏言库，不改变状态 |
| `draft_edict` | `session_id, sections?, adopted?, ops?, cards?` | 回执逐条 applied / rejected（原因与错误码）/ delayed（驿程 / 有司稽迟 / 将领自主）/ narrative_only，返回 `edict_id`；cards 为 `[{card_id, params}]` |
| `confirm_edict` | `session_id, edict_id` | 生效并记下已采纳奏疏 |
| `choose` | `session_id, choice_id, option_id` | 处置待决事件 |
| `end_turn` | `session_id, discard_drafts?` | 事实、账目合计、战报、事件、新待决事件、危机分项、纪事、终局 |
| `history` / `rewind` | `session_id` / `session_id, entry_id?` | 时间线（含分支）/ 回到某节点另开分支 |
| `export_log` | `session_id, from_turn?, to_turn?` | 结构化复盘日志（JSON，公开视图）：逐季的采纳选项、客户端 ops、拟诏 / 确认回执、延期条款、抉择、结算事实、对账（含经常收支明细）、危机变化拆分、终局；REST 为 `GET /api/sessions/{id}/export` |

所有工具只读公开视图；错误一律以「错误 [CODE]：中文说明」返回（`is_error=true`），不含堆栈。

## 自检

```bash
cd engine/backend
python -m pytest -q tests/test_mcp.py   # 含一次真实 stdio 往返（python -m cli.main mcp）
```

## 让模型整局试玩

```bash
python scripts/agent_play.py codex  --scenario _template --run gpt-01 --seed 7
python scripts/agent_play.py claude --scenario _template --run claude-01 --seed 7
```

每次运行独立的存档目录、事件流与模型自己写的试玩报告，见 [mcp-playtesting 技能](skills/mcp-playtesting/SKILL.md)。
