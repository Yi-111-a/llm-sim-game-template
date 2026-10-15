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

从 `scenarios/_template` 复制目录并修改 `scenario.yaml`、人物、地区、事件与 `assets.yaml`。新场景需要设置 `manifest_version`、`version`、`requires_engine` 和历法。图片可先保留 `missing` 状态，运行时使用 SVG 占位。

```bash
cd backend
sim validate ../scenarios/my-scenario
sim gen-assets my-scenario --only missing
pytest -q
```

更多操作流程见 `skills/create-llm-sim-game/SKILL.md`。

## 5. 检查

```bash
scripts/check.sh
```

该脚本运行 Ruff、mypy、pytest、TypeScript 和 Vite 构建；也可按 [CI 工作流](.github/workflows/ci.yml)逐项检查。
