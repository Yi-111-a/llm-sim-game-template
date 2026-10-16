---
name: character-roleplay
description: 实现与编写遵循身份、在场、记忆、生死与朝会日程的人物召对与廷议；人设卡与 StateDigest 是模型的唯一事实来源。
---

# 人物扮演

## 何时使用

新增或调整召对（audience / chat）、廷议（court_debate）、人物记忆、人设卡（`characters.yaml` 的 persona 与 card）或离线模板时使用。

## 输入

人物的 persona、speech_style、knowledge、card（greeting、examples）、当前官职与所在、记忆、该人物视角的 StateDigest、召对方式与朝会日程。

## 步骤

1. **能否召对**：已故、系狱者拒绝（`afterlife_chat` 例外）；玩家本人不在名单；每季召对次数受 `settings.agenda` 限制（`AGENDA_EXHAUSTED`）。
2. **召对方式**：与君主同地 → 面见；远在他处 → 书面奏对；在途 → 遣使问对。远地召对在 `remote_reply: next_season` 时当场只回「使者已发」，回奏下季送达（client 模式由调用方据下季简报的「回奏」一节扮演）。
3. **只给该人物可知的事实**：人设卡（现任官职，旧衔标注）+ 该人物视角的 StateDigest（所在、统兵所在、knowledge 中的地区、行程目的地、相邻；其余地区压成一行）+ 最近 12 条记忆。隐藏数值只给区间，隐藏心态不出现。
4. **数字白名单**：台词里的数字必须出现在摘要或问题里，否则引擎改用模板台词。提示词写明「只引用摘要中的数」。
5. **廷议**：只有身在京城且不在途者能当廷议（`NOT_PRESENT`）；不在京者 `written` 递书面议状，下季入谏言库。立场由引擎确定（奏疏谏言 → debate_topics → 议题实体与亲疏 → 忠诚 → 种子），模型只写台词，不改立场；台词要点明主张哪个选项。廷议结论只能存为谏言（`save_advice`），不能直接变成操作。
6. 对话不产生状态变更；诏书确认时引擎自动给当事人写记忆（任免、处置）。

## 写人设（剧本侧）

- persona 只写不变的性格、立场、口头禅；不要写现任官职、所在地、未来命运、隐藏数值。
- greeting 尽量不含官衔；examples 给两三句典型说法。
- 出镇在外的人物，不要在奏疏里写成当廷进言；第一人称「臣在京」须有 `requires.region` 守卫。

## 输出契约

有身份与在场边界的台词或明确拒绝；所有文本都不具备状态操作权限。

## 涉及文件

`backend/simcore/agent/debate.py`、`agent/offline.py`、`domain/court.py`、`domain/digest.py`、`api/app.py`（SSE `/chat`）、`simcore/mcp/server.py`、`prompts/minister_chat.md`、`prompts/court_debate.md`、`scenarios/<id>/characters.yaml`。

## 验证

覆盖在世 / 已故 / 系狱、面见 / 书面 / 遣使、日程用尽、不在京廷议、书面议状下季送达、台词编造数字被替换；Mock 下 SSE 含 status、message、done（远地另有 deferred）。
