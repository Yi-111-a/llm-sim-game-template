---
name: frontend-panels
description: 实现只呈现服务端公开视图的游戏面板：/view 驱动的奏疏、待决事件、诏书草案与回执、对账、危机计、朝会日程、新手引导与终局，带 data-testid。
---

# 前端面板

## 何时使用

新增或调整地图、人物、奏疏、诏书、政令卡、军务、对账、纪事、召对、廷议、存档时间线或设置界面时使用。本仓库的 `frontend/` 是通用最小界面；精修的完整界面参见《天宝》的 `app/`（React 18 + Vite + Framer Motion，Tauri 桌面壳）。

## 输入

API 契约（`/view`、`/edicts/draft|confirm`、`/choices`、`/turn`、`/chat`、`/policy_cards`、`/armies`、`/present`、`/export`）、场景元数据、manifest id。

## 步骤

1. **以 `/view` 为主数据**：`state`（公开视图）、`memorials`、`pending_choices`、`objectives`、`crisis`、`agenda`、`tutorial`、`final_notice`、`outcome` / `review`、`last_report`（含对账）、`command_table`、`provider_mode`。每次操作后重新拉取；前端不维护独立规则、不改本地世界状态。
2. **隐藏数值**没有 `amount`，只有 `estimate` 区间与 `display`；任何地方都不能把它显示成确数。
3. **诏书**：批答奏疏（`adopted`）、政令卡（`cards`）、文本（`sections`）一起 `draft`，逐条显示回执（`summary`、状态、`reason` / `delay_reason`），再 `confirm`。离线时提示「御笔只记入史册」。
4. **推进**：409 `DRAFT_UNCONFIRMED` 时提示先确认，或提供「放弃草案并推进」（`{discard_drafts: true}`）；终局后禁用下诏与推进，显示结局与复盘。
5. **召对**：SSE 消费 `status` / `message` / `deferred` / `error` / `done`；409 `AGENDA_EXHAUSTED` 在流开始前返回，显示剩余次数。廷议只列 `/present` 的在京者，其余可递书面议状。
6. 在途军队显示 `whereabouts`（「在途：A→B」），不要写成「驻目的地」。
7. 图片只按场景 id + manifest id 请求；所有文字由 DOM 渲染（图片不含文字）。
8. 每个可交互元素带 `data-testid="<画面>.<元素>"`，剧本的新手引导 `target` 指向它们；模板前端已有 `desk.resources`、`memorial.list`、`memorial.item.<id>`、`memorial.option.<id>.<选项>`、`edict.preview`、`edict.confirm`、`desk.advance`、`choice.list`、`ending.panel`、`tutorial.panel`。
9. 窄屏可读，请求期间禁用按钮；模型设置走本机 settings API，不把密钥放进构建。

## 输出契约

可访问的面板，只呈现服务端确认的状态，Mock 模式下能从开局玩到结局。

## 涉及文件

`frontend/src/`、必要时 `backend/api/app.py` 与 `ARCHITECTURE.md`。

## 验证

`npm ci && npx tsc --noEmit && npm run build`；启动 `sim serve` 与 `npm run dev`，开一局 `_template`：批答 → 预览 → 未确认推进被拒 → 确认 → 推进 → 处置待决事件 → 召对 → 打到终局；控制台 0 错误、无乱码。按钮清单可用 Playwright 按 data-testid 逐个点击。
