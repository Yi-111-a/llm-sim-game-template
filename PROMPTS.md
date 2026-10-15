# 提示词约定

提示词位于 `backend/simcore/prompts/*.md`，使用 Jinja2 和 YAML frontmatter 描述角色、输入与输出契约。场景可在 `scenarios/<id>/prompts/<同名文件>.md` 覆盖任意内置模板；覆盖只影响该场景。

## 角色边界

- `edict_interpreter`：将诏令文本转换为类型化操作提议；不自行修改状态，信息不足时要求澄清。
- `narrator`：只根据本回合已应用的 `TurnReport` 写纪事，不补造人物行动、数字或因果。
- `character_chat`：以当前人物状态、身份与记忆对话；不可创造新的可执行状态变更。
- `court_debate`：围绕议题呈现不同立场，不替代规则校验。
- `edict_polish`：只润色表达，保留原意，不产生操作。
- `chronicler`：整理已记录事实，不添加来源之外的历史事实。

## StateDigest（事实摘要）

`simcore/domain/digest.py::render_digest(state, seed, character_id=None, report=None, scenario=None)` 渲染固定格式的中文事实摘要，是召对、廷议、诏令解释提示词与 MCP 工具的唯一数值来源：

```text
【时】天宝十载春（第0回合）
【本人】李相；宰相；所在 京畿；在世          ← 仅人物视角
【国势】国库 1000万贯；…；异志 约28.9–51.3点（风闻）
【官职】宰相：李相；元帅：出缺；…
【地区】京畿（朝廷） 繁荣60 防务50 …       ← 人物视角只含所在及相邻地区
【军队】禁军 20000，驻京畿，属朝廷，统帅郭将军
【工程】/【常设令】/【待决】/【本季事实】/【终局】  ← 有内容时才出现
```

- 只使用公开视图：隐藏资源一律给区间，隐藏心态不出现。
- `minister_chat.md` 接收 `digest` 与 `memories`（该人物最近 12 条记忆）；旧变量 `resources` 仍可用（已是公开文字）。
- `court_debate.md` 每位发言人单独调用一次，只带该发言人的人设、引擎给定的立场（support/oppose/neutral 与理由）、该人物的 StateDigest 与此前发言。台词中的数字若不在摘要或议题里，引擎改用模板台词。Mock provider 识别「【廷议发言】」标记，生成只引用摘要第一条国势的模板句。
- 立场规则（`simcore/agent/debate.py`）：场景 `debate_topics: [{id, keywords, stances: {bloc 或人物 id: stance}}]` → 议题牵涉本人（姓名、官职、所统军队）或其亲疏之人（同派系、relation ≥ 20 / ≤ −20），按议题褒贬词判定 → 忠诚（≥8 赞成，≤3 反对，其余持中）。

## 编写规则

1. 给每个输入命名并提供简短字段说明。
2. 明确模型可做与不可做的事；事实来源应限定到状态摘要或操作结果。
3. 用工具 schema 或 JSON 结构承载程序要消费的数据，不解析自由文本猜测规则。
4. 场景覆盖文件沿用内置文件名。运行时先查场景 `prompts/`，无覆盖时使用内置文件。
5. 先跑 `sim validate <scenario>`，再用 Mock provider 检查返回内容和操作结果。

提示词应随场景版本管理，不要在提示词中复制程序确定的资源数值或规则。
