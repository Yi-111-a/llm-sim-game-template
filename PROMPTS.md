# 提示词约定

提示词位于 `backend/simcore/prompts/*.md`，使用 Jinja2 和 YAML frontmatter 描述角色、输入与输出契约。场景可在 `scenarios/<id>/prompts/<同名文件>.md` 覆盖任意内置模板；覆盖只影响该场景。

## 角色边界

- `edict_interpreter`：将诏令文本转换为类型化操作提议；不自行修改状态，信息不足时要求澄清。
- `narrator`：只根据本回合已应用的 `TurnReport` 写纪事，不补造人物行动、数字或因果。
- `character_chat`：以当前人物状态、身份与记忆对话；不可创造新的可执行状态变更。
- `court_debate`：围绕议题呈现不同立场，不替代规则校验。
- `edict_polish`：只润色表达，保留原意，不产生操作。
- `chronicler`：整理已记录事实，不添加来源之外的历史事实。

## 编写规则

1. 给每个输入命名并提供简短字段说明。
2. 明确模型可做与不可做的事；事实来源应限定到状态摘要或操作结果。
3. 用工具 schema 或 JSON 结构承载程序要消费的数据，不解析自由文本猜测规则。
4. 场景覆盖文件沿用内置文件名。运行时先查场景 `prompts/`，无覆盖时使用内置文件。
5. 先跑 `sim validate <scenario>`，再用 Mock provider 检查返回内容和操作结果。

提示词应随场景版本管理，不要在提示词中复制程序确定的资源数值或规则。
