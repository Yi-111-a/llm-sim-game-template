"""MCP 工具的元数据（说明、参数表、JSON Schema）。

只依赖标准库：stdio 快速启动路径（simcore.mcp.stdio）据此回答 initialize / tools/list，
不必先导入 MCP SDK 与整个服务层（Windows 上冷启动可达数十秒，超出 Codex 的 MCP 启动超时）。
"""

from __future__ import annotations

from typing import Any

INSTRUCTIONS = """这是一个历史模拟策略游戏的引擎（This MCP server runs a deterministic history-simulation strategy game）。
你扮演君主（the player is the ruler），引擎是唯一事实来源。
推荐流程 / Typical loop:
1. list_scenarios → new_game(scenario, seed) 得到 session_id 与开局简报（或 list_saves → load_game 继续存档）。
2. 每季先读 briefing(session_id)：目标、公开国势、危机计分项、本季奏疏（含选项与 ops 摘要）、待决事件、上季事实。
3. 需要时 audience 召见人物（你按返回的人设卡与事实摘要扮演），court_debate 举行廷议（立场由引擎给定），save_advice 把有用谏言存入谏言库。
   朝会日程有上限（简报「朝会日程」，满则 AGENDA_EXHAUSTED）；不在京者召对只能遣使，回奏于下季 briefing「回奏」送达；
   不在京者不能当场廷议（NOT_PRESENT），可列入 court_debate 的 written 递书面议状，下季入谏言库。
4. 用 draft_edict 拟诏：adopted 采纳奏疏选项，cards 使用政令卡（policy_cards 查看），ops 直接给出 StateOp（格式见 op_reference）；
   作用于远地的条款「驿程遥远，令达于下季」；军令（出征、分兵、set_army_posture）附将领自主风险等级；
   看回执中每条 applied（可行：确认后本季即行）/ rejected / delayed（确认后延至本季末结算时执行），再 confirm_edict。
   草案不确认不施行：本季有未确认草案时 end_turn 会以 DRAFT_UNCONFIRMED 拒绝（确要放弃则 discard_drafts: true）。
   确认诏书或处置抉择后若出现「本季新出奏疏」，可在 end_turn 前继续拟诏处理。
5. 用 choose 处置待决事件；然后 end_turn 结算本季，读取事实、战报、危机分项与终局。
6. 终局后（outcome 非空）不能再推进；可用 history / rewind 回到更早的节点另开分支。
数字只能引用简报与工具返回中给出的值；「风闻」数值只有区间。"""

DESCRIPTIONS: dict[str, str] = {
    "list_scenarios": "列出可玩的场景（id、标题、时代、是否通过校验）。List available scenarios. 无参数。",
    "new_game": (
        "新开一局：scenario 为 list_scenarios 返回的 id，seed 为随机种子（同种子可复现），difficulty 为难度档 id"
        "（见 list_scenarios 的 difficulties；留空用场景默认）。返回 session_id 与开局简报。"
        "Start a new game from a scenario id with a seed and optional difficulty; returns the session_id and the opening briefing."
    ),
    "list_saves": "列出全部存档（session_id、场景、回合）。List saved sessions (shared with the desktop app via LLM_SIM_HOME).",
    "load_game": "读取存档并返回当前简报。Load a saved session by session_id and return its briefing.",
    "briefing": (
        "当季简报：历法、目标状态、公开资源（隐藏资源只给风闻区间）、危机计及分项、本季奏疏（含选项与 ops 摘要）、待决事件、上季事实，"
        "末尾附扮演与数字规则。每季行动前先读它。"
        "Seasonal briefing for the ruler, ending with the role-play / number rules you must follow."
    ),
    "op_reference": (
        "玩家可直接下达的操作（permitted_ops：一行字段说明 + 示例）与实体 id 表（每类至多 12 行，附总数）。"
        "其余操作（调整资源/地区/关系、旗标等）只能经奏疏选项或事件产生，客户端提交会得到 OP_NOT_PERMITTED。"
        "过滤：op=<操作名> 看完整 JSON Schema；entity_kind=characters|offices|regions|armies|resources|factions|ledger|standing_orders|pending_choices"
        " 看完整表；ids=[...] 只看这些 id。Player-permitted ops with examples and entity ids; filter with op / entity_kind / ids."
    ),
    "audience": (
        "召对某位人物：character 为人物 id。client 模式返回人设卡 + 该人物可知的事实摘要 + 记忆 + 口吻要求，由你扮演作答；"
        "server 模式需同时给 question，返回服务端模型的回答。死者或系狱者会被拒绝。每次召对计入朝会日程（满则 AGENDA_EXHAUSTED）；"
        "人不在京（书面奏对 / 遣使问对）时不当场回话：返回「使者已发，回奏于下季送达」，下季 briefing「回奏」一节给出人设卡与摘要供扮演。"
        "Audience with a character: returns a persona card and what they know (client mode) or their reply (server mode)."
    ),
    "court_debate": (
        "廷议：topic 为议题，participants 为 1–6 个在京人物 id（不在京者 NOT_PRESENT；written 列出不在京者，递书面议状，"
        "其立场与理由于下季送达谏言库，不参与当场辩论），memorial_id 可选（就某条本季奏疏廷议；给了它 topic 可留空），"
        "option_id 可选（指定「所议之案」：赞成即主此案、反对即主他案；不给时引擎按奏疏所请 / 谏言推定，推定不出则诸案并陈、不以首个选项为案）。"
        "议题对应奏疏时用奏疏中的谏言立场（谏言人主张其谏言所指的选项），否则引擎按议题点名的人物/势力/官职/地区/军队与各人的亲疏、派系利益与忠诚给出立场"
        "（support/oppose/neutral），并列出 topic_entities；client 模式返回立场 + 人设，由你写台词；server 模式返回各人台词。"
        "廷议结论只能存为谏言，不直接改变状态。Court debate: engine-decided, topic-dependent stances; you (client mode) write the speeches."
    ),
    "policy_cards": (
        "政令卡：剧本预设的常用政令（军务 / 内政 / 人事 / 外交 / 财计），每张列出参数及按当前局势过滤的可选值、代价提示、是否可用（冷却 / 条件）。"
        "在 draft_edict 的 cards 中以 [{card_id, params: {参数名: 值}}] 使用，引擎展开为玩家层条款并照常校验。"
        "Policy cards with parameter options; use them via draft_edict(cards=[{card_id, params}])."
    ),
    "save_advice": "把谏言存入谏言库（不改变世界状态）：character 为进言人 id，text 为谏言原文。Save a piece of advice to the advice log.",
    "draft_edict": (
        "拟诏书草案（不改变状态）。sections 为 {类别: 诏令文本}（client 模式仅作记录，server 模式由模型解释）；"
        "adopted 为 [{memorial_id, option_id}] 采纳奏疏选项；cards 为 [{card_id, params}] 政令卡（见 policy_cards）；"
        "ops 为 StateOp 列表（见 op_reference）。作用于远地者「驿程遥远，令达于下季」（delayed，延期原因注明驿程 / 有司稽迟 / 将领自主）；"
        "回执逐条给出 applied（可行：确认后本季即行；确认后记「已行」）/ rejected（原因与错误码）"
        "/ delayed（确认后延至本季末结算时执行（进入第 n 回合时生效）；确认后记「已延至…」），并给出 edict_id"
        "（edict_id 只取 edict- 开头的编号本身）。草案须 confirm_edict 才施行。"
        "Draft an edict; returns a per-clause receipt and an edict_id for confirm_edict."
    ),
    "confirm_edict": (
        "确认诏书草案，使 applied 条款本季即行（回执记「已行」）、delayed 条款排入下季结算（回执记「已延至本季末结算时执行（进入第 n 回合时生效）」），"
        "并记下已采纳的奏疏；因此新出现的奏疏列在「本季新出奏疏」。"
        "编号不存在时错误信息列出本季可确认的草案编号。Confirm a drafted edict by edict_id."
    ),
    "choose": "处置待决事件：choice_id 与 option_id 见 briefing 的「待决事件」；因此新出现的奏疏列在「本季新出奏疏」。Resolve a pending event choice.",
    "end_turn": (
        "结算本季并进入下一季：返回事实、账目汇总、战报、触发事件、新的待决事件、危机分项与终局（若有）。终局后再调用会报 GAME_OVER。"
        "本季有拟而未确认的诏书草案时拒绝（DRAFT_UNCONFIRMED，列出编号）；确要放弃这些草案，传 discard_drafts: true。"
        "Resolve the season and return the TurnReport (refuses with DRAFT_UNCONFIRMED while drafts are unconfirmed unless discard_drafts)."
    ),
    "history": "存档时间线（含全部分支）：每个节点的 id、类型、回合、说明、父节点，标出当前所在。List the session timeline.",
    "export_log": (
        "结构化复盘日志（只读）：按季列出采纳的奏疏选项、客户端 ops、拟诏与确认回执、延期条款、事件抉择、"
        "召对与廷议（client 模式只记人物、方式、议题，不含台词）、"
        "end_turn 的事实 / 事件 / 对账（含经常收支明细）/ 危机计与本季危机变化拆分、终局。from_turn / to_turn 按下诏所在季筛选。"
        "隐藏数值只给风闻区间。Structured per-season log export (public view only)."
    ),
    "rewind": "回到时间线上的某个节点（entry_id；留空回到开局），之后的操作另开分支，原分支保留。Rewind to an entry id.",
}

# 各工具参数：(名称, 类型, 默认值)；默认值为 REQUIRED 表示必填。类型与 server.py 中函数签名一致
# （tests/test_mcp.py 校验两者生成的 JSON Schema 完全相同）。
REQUIRED: Any = object()
TOOL_PARAMS: dict[str, list[tuple[str, str, Any]]] = {
    "list_scenarios": [],
    "new_game": [("scenario", "str", REQUIRED), ("seed", "int", 0), ("difficulty", "str", "")],
    "list_saves": [],
    "load_game": [("session_id", "str", REQUIRED)],
    "briefing": [("session_id", "str", REQUIRED)],
    "op_reference": [("session_id", "str", REQUIRED), ("op", "str", ""), ("entity_kind", "str", ""), ("ids", "list[str]|None", None)],
    "audience": [("session_id", "str", REQUIRED), ("character", "str", REQUIRED), ("question", "str", "")],
    "court_debate": [
        ("session_id", "str", REQUIRED),
        ("participants", "list[str]", REQUIRED),
        ("topic", "str", ""),
        ("memorial_id", "str", ""),
        ("option_id", "str", ""),
        ("written", "list[str]|None", None),
    ],
    "policy_cards": [("session_id", "str", REQUIRED)],
    "save_advice": [("session_id", "str", REQUIRED), ("character", "str", REQUIRED), ("text", "str", REQUIRED)],
    "draft_edict": [
        ("session_id", "str", REQUIRED),
        ("sections", "dict[str,str]|None", None),
        ("adopted", "list[dict[str,str]]|None", None),
        ("ops", "list[dict]|None", None),
        ("cards", "list[dict]|None", None),
    ],
    "confirm_edict": [("session_id", "str", REQUIRED), ("edict_id", "str", REQUIRED)],
    "choose": [("session_id", "str", REQUIRED), ("choice_id", "str", REQUIRED), ("option_id", "str", REQUIRED)],
    "end_turn": [("session_id", "str", REQUIRED), ("discard_drafts", "bool", False)],
    "history": [("session_id", "str", REQUIRED)],
    "export_log": [("session_id", "str", REQUIRED), ("from_turn", "int|None", None), ("to_turn", "int|None", None)],
    "rewind": [("session_id", "str", REQUIRED), ("entry_id", "str|None", None)],
}

_BASE_TYPES: dict[str, dict[str, Any]] = {
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "bool": {"type": "boolean"},
    "list[str]": {"items": {"type": "string"}, "type": "array"},
    "dict[str,str]": {"additionalProperties": {"type": "string"}, "type": "object"},
    "list[dict[str,str]]": {"items": {"additionalProperties": {"type": "string"}, "type": "object"}, "type": "array"},
    "list[dict]": {"items": {"additionalProperties": True, "type": "object"}, "type": "array"},
}


def input_schema(tool: str) -> dict[str, Any]:
    """工具参数的 JSON Schema（与 MCP SDK 依函数签名生成的相同）。"""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, kind, default in TOOL_PARAMS[tool]:
        title = name.replace("_", " ").title()
        if kind.endswith("|None"):
            properties[name] = {"anyOf": [_BASE_TYPES[kind[:-5]], {"type": "null"}], "default": None, "title": title}
        elif default is REQUIRED:
            properties[name] = {"title": title, **_BASE_TYPES[kind]}
            required.append(name)
        else:
            properties[name] = {"default": default, "title": title, **_BASE_TYPES[kind]}
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    schema["title"] = f"{tool}Arguments"
    return schema


def tool_list() -> list[dict[str, Any]]:
    """tools/list 的结果（顺序与 server.py 注册顺序一致）。"""
    return [{"name": name, "description": DESCRIPTIONS[name], "inputSchema": input_schema(name)} for name in TOOL_PARAMS]
