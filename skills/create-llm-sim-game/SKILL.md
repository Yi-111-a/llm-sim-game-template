---
name: create-llm-sim-game
description: 从想法到可玩的本地优先 LLM 模拟游戏，涵盖场景、资产、提示词、验证、测试与游玩。
---

# 创建模拟游戏

## 何时使用

当需要从一个游戏点子创建完整新场景，或从空白仓库建立可玩的模拟游戏时使用。

## 输入

- 游戏主题、玩家角色、目标与回合尺度。
- 运行规则、资源、地区、角色、事件和可选的公开史料。
- 目标模型与图像服务（可先全部使用 Mock）。

## 步骤

1. 写下玩家每回合的决策、可观测结果和失败条件；规则必须能由代码判定，模型不能直接改状态。
2. 复制 `scenarios/_template` 到 `scenarios/<id>`，编辑 `scenario.yaml`：资源、官职、诏令类别、公式、版本与历法。
3. 编写 `regions.yaml`、`characters.yaml`、`factions.yaml`、`armies.yaml`、`events.yaml`。人物与事件文案须自行撰写；确保所有邻接地、人物驻地和事件目标有效。
4. 按 `ASSETS.md` 为角色肖像、地区卡、事件插画及 UI 面板添加 `assets.yaml` 条目，登记用途、相对路径、尺寸和生成提示词。先用 SVG 占位验证界面。
5. 检查 `backend/simcore/prompts/` 的角色提示词；需要场景语气时，用相同文件名放入 `<scenario>/prompts/` 覆盖。
6. 在 `backend` 环境中运行 `sim validate ../scenarios/<id>`。逐条修复错误后再次验证。
7. 可选地运行 `sim gen-assets <id> --only missing`；核对生成路径与 manifest 状态，不将密钥或未授权素材加入场景。
8. 为规则、事件和模型边界编写测试，先用 Mock provider 运行 `pytest -q`。
9. 启动 `sim serve` 和前端，创建存档，草拟并确认诏令，推进回合、查看纪事，再尝试角色对话。

## 输出契约

- 一个校验通过、可离线游玩的场景包。
- 可重现的规则测试与资产 manifest；未生成图片能由占位图代替。
- README 或场景说明记录玩家目标、回合规则与模型要求。

## 涉及文件

`scenarios/<id>/{scenario.yaml,characters.yaml,regions.yaml,factions.yaml,armies.yaml,events.yaml,assets.yaml,style.md,prompts/}`、`backend/tests/`、必要的根目录文档。

## 验证

至少通过 `sim validate`、目标场景的规则测试和手动完整回合流程；外部提供方测试不替代 Mock 离线测试。
