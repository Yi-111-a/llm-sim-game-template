# 上手指南

## 1. 安装

准备 Python 3.11+、Node.js 22+、npm。

```bash
git clone https://github.com/Yi-111-a/llm-sim-game-template.git
cd llm-sim-game-template
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cd ../frontend
npm ci
```

Windows PowerShell 激活虚拟环境时使用 `.venv\Scripts\Activate.ps1`。

## 2. 验证并启动

终端一：

```bash
cd backend
source .venv/bin/activate
sim validate ../scenarios/_template
sim validate ../scenarios/chongzhen_1627
sim serve
```

终端二：

```bash
cd frontend
npm run dev
```

打开 Vite 输出的本机地址，选场景并开始新存档。默认 Mock 可完整游玩。编辑器中的每个类别可单独填写诏令；预览后逐条检查，确认后才应用，再推进回合查看纪事。点击朝臣可在对话面板提问。

## 3. 接入自己的模型

在设置面板选择预设或 custom，填写兼容 API 地址、模型与密钥。密钥只由本机后端读取，不提交到仓库。先用「测试工具能力」查看工具调用支持情况；不支持时诏令处理会尝试 JSON 回退。离线测试仍建议使用 Mock。

## 4. 创建新场景

从 `scenarios/_template` 复制目录。模板只有 5 人、4 地、8 季，但每种机制（奏疏、事件抉择、结局与失败、难度、危机计、政令卡、朝会日程、驿程、将领自主、新手引导……）都有一个可运行的示范，注释说明了每个字段。逐文件的写法见 [docs/SCENARIO_GUIDE.md](docs/SCENARIO_GUIDE.md)。

```bash
cp -r scenarios/_template scenarios/my_game      # 再改 scenario.yaml 的 id、title
cd backend
sim validate ../scenarios/my_game                 # 结构与引用
python ../scripts/content_lint.py my_game         # 文字与状态是否相符
python ../scripts/simulate.py my_game --seeds 200 # 种子模拟：各策略的结局分布
sim gen-assets my_game --only missing             # 可选：生成图片（缺图时用 SVG 占位）
```

## 5. 让 Claude Code / Codex 来玩

```bash
claude mcp add llm-sim -e SIM_SCENARIOS_DIR=<仓库>/scenarios -- <venv python> -m cli.main mcp
```

Codex 的配置（`startup_timeout_sec`、`default_tools_approval_mode`）与无人值守试玩见 [MCP.md](MCP.md)。

## 6. 检查

```bash
scripts/check.sh
```

该脚本运行 Ruff、mypy、pytest、场景校验、内容检查、种子模拟、TypeScript 和 Vite 构建；也可按 [CI 工作流](.github/workflows/ci.yml)逐项检查。
