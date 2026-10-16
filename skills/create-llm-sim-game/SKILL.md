---
name: create-llm-sim-game
description: 从想法到可玩的本地优先 LLM 模拟游戏：复制模板、写剧本包、校验、平衡模拟、内容检查、前端与 MCP 试玩的完整流程。
---

# 创建模拟游戏

## 何时使用

从一个游戏点子做出完整新场景，或从空白仓库建立可玩的模拟游戏时使用。这是总流程；各环节细节见同目录的其他技能与 `docs/SCENARIO_GUIDE.md`。

## 输入

- 主题、玩家扮演谁、每季要做的决定、目标与失败、回合尺度与局长（如 20 季）。
- 资源、地区、人物、势力、军队、事件链与可选的公开史料。
- 目标模型（可先全部用 Mock）。

## 步骤

1. **先写决策表**：每季玩家能做什么（批奏疏、处置事件、政令卡、召对、廷议、调兵），看到什么（简报、危机计、对账），怎样输、怎样赢。每条规则都必须能由代码判定；模型只能提议操作。
2. **复制模板**：`cp -r scenarios/_template scenarios/<id>`，改 `scenario.yaml` 的 id、title、version、历法、资源、官职、公式、`game_length`、`endings`、`crisis`、`difficulties`、`settings`（agenda、capital_region、courier、autonomy、player_ops、flag_labels）。模板里每个机制都有一个最小示范，照着替换即可。
3. **写实体**：`characters.yaml`（人设只写不变的性格与立场）、`regions.yaml`（邻接、`courier_days`）、`factions.yaml`、`armies.yaml`。
4. **写决策内容**：`memorials.yaml` 是主线（每季 2–3 条以上，谏言人、所议之案、变体、守卫），`events.yaml` 写由奏疏旗标驱动的后果与抉择，`policy_cards.yaml` 写日常可做之事，`tutorial` 写前三季的引导。见 scenario-design、event-design 技能。
5. **资产**：`assets.yaml` 登记肖像、地区卡、事件插图与 UI 槽位，先用 SVG 占位（asset-manifest、image-asset-generation 技能）。
6. **三件套检查**（每次改动后）：

   ```bash
   cd backend
   sim validate ../scenarios/<id>
   python ../scripts/content_lint.py <id>
   python ../scripts/simulate.py <id> --seeds 200
   ```

7. **平衡**：按 balance-and-replay-testing 技能调到「不作为大概率中下、处处首项 / 末项拿不到最好、用心可达成、每个结局可达」。
8. **整局试玩**：先自己经前端或 MCP 玩一局，再按 mcp-playtesting 技能让 GPT / Claude 各玩一局并写报告；把问题按「引擎 / 剧本」归类修复，重复到报告只剩措辞问题。
9. **测试**：为剧本写一个「每项取首选经 MCP 打到终局」的测试（参照 `backend/tests/test_template_scenario.py`），以及关键事件链的规则测试。

## 输出契约

- 一个 `sim validate` 通过、`content_lint` 无错误、离线（Mock）可从开局玩到结局的场景包。
- 模拟结果表（各策略、各难度的结局分布）与至少一份模型试玩报告。
- 回归测试与资产 manifest；未生成的图片由占位图代替。

## 涉及文件

`scenarios/<id>/{scenario,characters,regions,factions,armies,events,memorials,policy_cards,assets}.yaml`、`style.md`、`prompts/`、`backend/tests/`、`scripts/`。

## 经验（来自《天宝》的九轮迭代）

- 模型试玩最常发现的不是机制错误，而是**文字与状态不符**：奏疏称某人在任而他已罢免、选项说「另择」却没有任命、事件抢在奏疏前把事办了。写每一句状态断言时就加守卫。
- 玩家可见文字里永远不出现原始 id；数字只引用引擎给的值。
- 一个机制对玩家不透明（延期原因、危机突跳、情报精度）就会被报告为 bug：让引擎把原因说出来，剧本把代价写进 hint。

## 验证

`scripts/check.sh` 全绿；模拟分布达标；一局 Mock 手动通关；至少一份 GPT 或 Claude 的 MCP 整局报告且无工具报错。
