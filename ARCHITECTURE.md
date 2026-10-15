# 架构

## 模块关系

```mermaid
flowchart LR
  UI[React 前端] --> API[FastAPI]
  CLI[Typer CLI] --> Core[模拟核心]
  API --> Core
  API --> LLM[OpenAI 兼容 / Mock]
  API --> Images[图像服务 / SVG 占位图]
  Core --> Rules[操作校验与确定性规则]
  Core --> Store[JSONL 事件存档]
  Core --> Scenario[场景 YAML 与资产清单]
  LLM --> Prompts[Jinja2 提示词]
  Images --> Manifest[assets.yaml]
```

## 回合流水线

```mermaid
sequenceDiagram
  participant UI as 前端
  participant API
  participant Core as 确定性引擎
  participant Store as 会话日志
  participant LLM
  UI->>API: POST /sessions/{id}/turn
  API->>Store: 读取当前状态与场景
  API->>Core: 推进回合（种子 RNG）
  Core->>Core: 延期条款、常设命令、账目、工程、移动、公式、事件、历法
  Core-->>API: 新状态与 TurnReport
  API->>Store: 追加 turn 与状态哈希
  API->>LLM: 只基于 TurnReport 撰写纪事
  LLM-->>API: Memorial
  API-->>UI: state、report、memorial
```

## 诏令流水线

```mermaid
sequenceDiagram
  participant UI as 前端
  participant API
  participant Agent as 诏令解释器
  participant Rules as 操作校验器
  participant Store as 会话日志
  UI->>API: POST /edicts/draft
  API->>Agent: 分类文本、状态摘要、工具
  Agent->>Rules: propose_ops(typed StateOp)
  Rules-->>Agent: 可执行操作或拒绝原因
  Agent-->>API: Receipt 预览
  API-->>UI: applied / rejected / delayed
  UI->>API: POST /edicts/{id}/confirm
  API->>Store: 仅追加确认的操作或延期条款
  API-->>UI: 更新后的收据与状态
```

## 状态与模型边界

`WorldState` 是确定性规则的唯一运行状态。操作经 Pydantic 联合类型解析，再由规则层检查目标、资源、任职条件与场景限制。拒绝操作返回稳定错误码；LLM 只能通过工具提议操作，API 在确认阶段追加日志。

会话是带父节点的 JSONL 事件树。每个节点保存类型、父节点、时间与载荷；从日志可重放状态并计算 `state_hash`。分叉通过新日志头引用选定历史节点，不覆盖原存档。

模型提供方只负责文本生成与工具调用。程序负责规则、推进顺序、成本统计、请求超时和取消。详见 [PROVIDERS.md](PROVIDERS.md)。
