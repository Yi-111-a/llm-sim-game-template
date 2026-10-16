---
name: mcp-playtesting
description: 让 Claude Code 或 Codex 经 MCP 从开局玩到结局并写试玩报告；用于发现文字与状态不符、说明不清、数字矛盾与机制漏洞，并把问题归类为引擎或剧本修复。
---

# MCP 试玩

## 何时使用

剧本或引擎有了较大改动、准备发版、或需要「真人视角」的反馈时使用。种子模拟（`scripts/simulate.py`）只会按选项位置选择；模型试玩会读简报、召对、廷议、下诏、读回执，最能暴露「读起来不对」的问题。

## 输入

场景 id、种子与难度、要用的 agent（Codex / Claude Code）与模型、装好引擎的 venv。

## 两种方式

**A. 无人值守（推荐，便于复现与对比）**

```bash
python scripts/agent_play.py codex  --scenario <id> --run gpt-01    --seed 7  [--model <model>] [--difficulty hard]
python scripts/agent_play.py claude --scenario <id> --run claude-01 --seed 7
```

每次运行在 `playtests/<agent>/<run>/` 下有独立的 `home/`（`LLM_SIM_HOME`，存档互不干扰）、`events.jsonl`（全部工具调用与结果）、`stderr.txt`、`report.md`（模型写的报告）与 `summary.json`。提示词要求：逐季 briefing → 召对 / 廷议 → draft_edict → confirm_edict → choose → end_turn，玩到结局，最后 `export_log` 复盘并写中文报告（结局、每季一行大事记、问题清单、改进建议，问题写明季、工具、参数与返回）。

**B. 交互**：`claude mcp add` / `codex mcp add` 注册服务（见 MCP.md），在会话里说「用 llm-sim 开一局 <id>，种子 7，玩到结局，然后把试玩报告写到 report.md」。

## Windows 与接入的坑（都已在 agent_play.py 中处理）

- Codex 非交互必须 `default_tools_approval_mode = "approve"`，否则每次 MCP 调用都在等审批；`codex mcp add` 不写这一项。
- Codex 的 `startup_timeout_sec = 60`、Claude Code 的 `MCP_TIMEOUT=60000`：Python 服务冷启动加杀毒扫描可能超时，服务会**悄悄**没挂上（《天宝》gpt-09 就这样开局时工具目录为空，模型自己绕道用了别的客户端）。报告里先确认工具调用确实走的是 MCP。
- npm 的 `codex.CMD` 经 cmd.exe 会弄乱 UTF-8 参数：用 `node codex.js` 直调，提示词经 stdin 传入。
- 中文 Windows 上 Codex 的 JSON 流是 cp936：按行先试 UTF-8 再退回 cp936。
- 服务端设 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`；`SIM_MCP_MODE=client`（调用方模型扮演人物、直接给 ops）。
- Claude Code 用 `--strict-mcp-config` 与 `--allowedTools "mcp__<name>__*" Write` 保持非交互。

## 读报告与修复

1. 先看 `summary.json`（退出码、是否有报告）与 `events.jsonl` 中的工具报错次数；0 报错是基本门槛。
2. 把报告的每一条问题归类并编号（如 `#38`）：**引擎**（规则、回执措辞、视图不一致、缺工具）还是**剧本**（文字未加守卫、选项名实不符、平衡）。不要只修症状：同类问题往往在别的奏疏里也有——把规则补进 `content_lint`。
3. 典型发现（《天宝》九轮）：已故者仍在进言；在任者被说成出缺；奏疏晚于事件；「另择」却不任命；同一任命先说遥领又说已启程；危机计突跳无从解释；未确认草案被静默丢弃；情报「减半」与区间对不上；最后一季没有提示；客户端能直接改数值（作弊口子）。
4. 修复后用**同一种子**再跑一局对比；换种子、换难度、换 agent 各跑一局。连续数局 0 工具报错、问题只剩措辞时再发版。
5. 报告与日志存入 `playtests/`（模板仓库默认忽略该目录；游戏仓库可提交作为门禁证据）。

## 输出契约

每局：完整打到结局的事件流与模型报告；一张问题表（编号、归属、状态）；对应的修复与回归测试或 lint 规则。

## 涉及文件

`scripts/agent_play.py`、`MCP.md`、`backend/simcore/mcp/`、`playtests/`、剧本 YAML。

## 验证

`summary.json` 退出码 0 且 `report: true`；`export_log` 显示 outcome 非空；问题表中「引擎」类问题都有测试、「剧本」类问题都有守卫或 lint 规则覆盖。
