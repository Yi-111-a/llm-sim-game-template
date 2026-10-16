---
name: balance-and-replay-testing
description: 用种子模拟（scripts/simulate.py）平衡结局分布、难度梯度与经济曲线，并用状态哈希验证存档可重放。
---

# 平衡与回放测试

## 何时使用

改变资源初值、公式、事件概率、奏疏选项效果、结局条件或难度档；或排查重放差异时使用。

## 输入

场景包、设计目标（每种玩法应得到什么结局）、随机种子范围、需要的命名策略。

## 步骤

1. **基线分布**：

   ```bash
   cd backend
   python ../scripts/simulate.py <id> --seeds 200                     # none / first / last / random
   python ../scripts/simulate.py <id> --seeds 200 --difficulty hard
   ```

   看结局分布、平均终局回合、危机计终局值 / 峰值、目标达成率。
2. **设计目标**（可按剧本调整）：不作为 → 大概率中下结局；不看局势的 first / last → 拿不到最好结局；用心的「明君」策略 → 标准难度稳定好结局、高难度仍有相当失败率；每个结局（含失败）都可达。
3. **命名策略**：写一个小脚本复用 `simulate.play_engine`，策略是 `policy(options, rng) -> option_id`（按选项 id 偏好排序即可）；为难以触达的结局写「探针」策略。剧本复杂时参照《天宝》`tools/sim_policies.py`（明君、史实、各结局探针、政令卡策略）。
4. **逐季轨迹**：`--seed N --trace` 看每季的资源、批答、事件、战斗；用来定位「为什么这条路线总是失败 / 总是太容易」。
5. **覆盖**：`--coverage` 检查每季可见奏疏的最少条数（目标 ≥ 2–3）。
6. **精确规则**：最终确认用 `--backend service`（走真实的草案、确认、延期、驿程、将领自主）。
7. **回放**：记录一条含操作、延期、回合与分叉的会话，`sim replay <session>` 逐回合比对 `state_hash`；不一致时先找非确定时间、未派生的随机数或未入日志的变更。

## 输出契约

一张「策略 × 难度 → 结局分布」表（写进剧本说明或 PR），固定种子的回归测试，可重放的会话证据。

## 涉及文件

`scripts/simulate.py`、`scenarios/<id>/scenario.yaml`、`events.yaml`、`memorials.yaml`、`backend/tests/`、`simcore/store/session.py`。

## 经验（《天宝》）

- 「全选最差选项仍得再造唐室」（#52）、「明君路线危机计全程不超过 38」（#19）、「T0 夺镇后第一次结算即兵变，来不及应对」（#50）都是先由模拟或自玩发现的。
- engine 后端不掷延期骰；延期概率高的剧本，用 service 后端复核。
- 改平衡后同时看三档难度，避免只调好了标准档。

## 验证

目标分布达标并记录；`pytest` 中的剧本测试通过；同一 JSONL 重放两次得到相同哈希。
