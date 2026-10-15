---
name: frontend-panels
description: 实现与确定性 API、场景数据和资产 manifest 对接的模拟游戏前端面板。
---

# 前端面板

## 何时使用

新增地图、角色、诏令、账目、纪事、聊天、存档或模型配置界面时使用。

## 输入

API 契约、Pydantic 状态字段、场景元数据和 manifest ID。

## 步骤

1. 先检查 API 响应与状态类型；前端不维护一套独立规则或修改本地世界状态。
2. 从场景 API 读取类别和主题；图片必须用场景 id 与 manifest id 请求。
3. 诏令流程先 draft 展示回执，再调用 confirm；推进回合后以 API 返回状态为准。
4. 对话通过 SSE 消费 status/message/done 事件；显示可恢复的错误与加载状态。
5. 存档分叉使用服务端 API，模型设置使用本机 settings API，不把密钥放进构建文件。
6. 为窄屏保留可读布局，按钮应在请求期间禁用。

## 输出契约

可访问的面板，只呈现服务端确认的状态，并能在 Mock 模式下完成完整回合。

## 涉及文件

`frontend/src/`、必要时 `backend/api/app.py` 与共享接口文档。

## 验证

执行 `npx tsc --noEmit`、`npm run build`；通过 curl 检查 Vite 页面可达。
