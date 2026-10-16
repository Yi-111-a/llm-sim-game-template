# 更新日志

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。引擎版本见 `backend/pyproject.toml` 与 `simcore/version.py`；剧本以 `requires_engine` 声明所需版本。

0.2 与 0.3 的引擎扩展都是在制作《天宝》（[Yi-111-a/tianbao](https://github.com/Yi-111-a/tianbao)）的九轮迭代中完成的：每一轮都由 Claude 与 GPT（Codex）经 MCP 整局试玩、写报告，再按报告修复。所有机制保持场景无关，由剧本 YAML 配置；旧剧本无需改动即可继续运行。

## [未发布]

### 新增（《宣和》制作中回写，见 `backend/tests/test_xuanhe_engine.py`）
- `adjust_army.quality_delta`：军队素质可由 op 调整（截在 0.1–3.0），事实与选项摘要写「素质 a→b」「素质 ±x」。`delta` 改为可缺省（只调素质时不写兵额）。
- `settings.battle.commander_factor`：统帅亲在军中时按其特质乘战力（公式，未写的特质按 5，截在 0.5–1.5）；战报因素写「将才（某人）±x%」，战力算法写「×将才」。未配置的剧本战力公式不变。
- `add_ledger.id`：剧本可给账目命名 id；同 id 再设即替换（不重复计入），之后可以 `cancel_ledger` 指名撤销（如「复置应奉司」后再罢）。
- 他国、敌方之人（势力不同于 `settings.player_faction`）不能召对、不能廷议或递书面议状（`CHARACTER_UNAVAILABLE`「非本朝臣属」），`/present` 也不列；剧本写满外方人物（金主、辽主、降将）时，召对名单不再混入外人。
- 京城本地的奏疏写「某月某日奏上」，不再写「自京城奏到」（`courier.arrival_text`；远地与晚到的照旧）。
- 京官（无任所、不统兵之职）的任命写了 `dispatch: true` 时，此人启程赴御前（召外官入朝）；以前京官无任所，`dispatch` 被忽略，人留在原地。玩家层任命缺省即 `dispatch: true`，故玩家任远地之人为京官也会召其入京；剧本层缺省 `false`，只授职不动人。
- 奏疏谏言人此刻势力不同于 `player_faction`（降而复叛的将领、敌国君臣）时不显示、不进言，与召对、廷议一致。
- 条件 `character_region` 可加 `arrived: true`：人须在此地且不在途中。启程赴任者的 region 仍是出发地，旧写法会把「已上路的人」当作仍在原地，随后对他的 `move_character` 被拒（CHARACTER_UNAVAILABLE）。
- 败军退走时，在军中的统帅随军同退，事实写「某军退往某地，某人随军」；以前军队退了，统帅却留在原地（常在敌境）。遥领的统帅不动。
- 奏疏 `cooldown: N`：常设条目处理满 N 季后重新呈上（`memorial:<id>` 保留上次批答，事件照读；再批重新计时）。以前只能用一项零费用工程在竣工时清旗标，这些「勘会」工程挤满玩家的工程列表。
- 简报「可召对人物」不再列他国、敌方之人（他们本就不能召对）。
- 条款随前条延期（`delay_kind: dependency`）：预览按「全部条款都执行」逐条校验，之后有条款被判延期（驿程、有司稽迟、将领自主）时，倚赖它的条款（如先令某人致仕、再以他人补其官缺）在预览里写「可行」、确认时却被拒。现在拟诏与确认时都用本季照常执行的条款重演一遍，倚赖延期条款者随之延至下季，回执写「随前条延期」。（gpt-01 #1）
- 廷议：`debate_topics` 的人物 / 派系立场表是对奏疏原议（`proposal`）而言的；玩家把原议的反面付廷议（如议「谢绝金使」而原议是「结盟」）时，表中的赞否随之对调，理由注「所议之案与原议相反」。以前清议一派会被派去「反对谢绝」、主张结盟，与人设相悖。（gpt-01 #3）
- 条件 `season`：`{kind: season, value: 冬}` 或 `value: [秋, 冬]`（`operator: ne` 取反），按当前季（结算中即正在结算的这一季）判断。节令事件（冰封、上元灯会）以前只能按回合号硬凑，常出现在错的季节。
- 致仕者仍可在奏疏下进言，但名下注「（致仕在家，上书言）」。（claude-01 C-3）
- 简报：列「诸国关系」（本方对各方的关系值）；有风闻数值时注明区间每季重新估测、不宜前后相比看趋势；危机分项「其他」改写「取整、到上下限截断等」。（claude-01 B-5/B-6/B-7）
- 远地事件的钱粮数目当季即入对账、军报下季才到：当季事实加一句「远地有事，军报未到：其数目已计入本季对账」，军报到时钱粮事实注「已计入上季对账，军报今到」。以前对账里有数而事实里无据、到报时又像再入一笔。（claude-01 B-1/B-2）
- 玩家层越权提示改为「……剧本预设的工程竣工效果……」，不再与「工程竣工效果不允许」自相矛盾；远地召对没写 question 时提示回奏只泛论时事。
- `merge_army {army, into}`：偏师归建——兵额并入同一势力的另一军，素质按兵额加权，原军归零、在途与军令清空。剧本层操作，不在玩家白名单内。

### 新增
- 本机 CLI 模型服务 `claude_cli` / `codex_cli`（`simcore/llm/cli_agent.py`）：用本机已登录的 Claude Code / Codex 扮演群臣、写史笔、解释诏令，**不需要 API key**。每次调用起一个无头子进程：在空的临时目录运行、关掉 CLI 自带的工具与 MCP、提示词走 stdin、回复按 UTF-8 读取；Claude 支持流式答话，Codex 读 `-o` 输出的最后一条消息。没有原生函数调用，诏令解析自动退回「只返回 JSON」路径。找不到 / 未登录时给玩家看的中文提示。
- `guarded_chat` 的单次超时不低于 provider 声明的 `min_call_timeout`（穿过计费包装层），CLI 冷启动不会被 30 秒超时截断。
- `/api/settings/test` 对本机 CLI 真发一句话检查已登录、能回复，而不是只探测工具调用。
- `scripts/content_lint.py` 新增 W13（玩家可见文字写回合代号，如「T19」）与 W14（选项改动兵额 ≥ `settings.troop_hint_min`、默认 2000，却没在标题或提示里写出这个数）。
- `docs/SCENARIO_GUIDE.md`：同季事件的判定顺序与「靠守卫而不是挪顺序」去除矛盾；局势改变后旧奏疏的变体（夺职、出京、驰表）；试玩时补看真实界面与「模型当大臣」；`settings.troop_hint_min`。技能 `mcp-playtesting`、`create-llm-sim-game` 补上第 10 轮与首次发版的经验（含发版清单、Claude 桌面版 CLI 的 MSIX 路径）。

### 修复（《天宝》第 10 轮，GPT gpt-10 报告与宣传片拍摄中发现）
- 廷议：谏言人写明的主张与所议之案不在同一方时，立场改判为「反对」（原先会出现「赞成」却主张另一案）。
- 人设卡旧衔：只列已不在现职里的部分（原先「左相、判度支」会附「旧衔：左相，已非现职」）。
- 结算中由事件启程的人物：行程从本回合起算（原先简报写「预计本回合抵达」，实际要再等一季）。
- 延期生效的任命：回执与待办写「令达后即日视事」，生效后才写「即日视事」。
- 离线召对：说到自己所统之军时用自称，不以第三人称说自己。

## [0.3.0] — 2026-10-16

模板与文档的整理版，引擎运行行为与 0.2 系列最后一轮相同。

### 新增
- `docs/SCENARIO_GUIDE.md`：逐文件、逐机制的剧本写作指南，含可复制示例、校验、平衡模拟与内容检查清单。
- `scenarios/_template` 重写为全机制示范：带谏言人 / 所议之案 / 变体 / requires 的奏疏、事件抉择、结局 + 失败 + 时限与加时、三档难度、隐藏数值与情报、危机计、养兵与初始账目、战斗与 AI 路线、4 张政令卡、朝会日程、京城在场、驿程、将领自主、3 步新手引导。测试以「每项取首选」经 MCP 打到终局。
- `scripts/simulate.py`：场景无关的种子平衡模拟（none / first / last / random 策略，engine 与 service 两种后端，`--trace`、`--coverage`）。
- `scripts/content_lint.py`：场景无关的内容检查（原始 id 外泄、状态断言缺守卫、选项名实不符、变体互斥、奏疏先于事件等 17 项）。
- `scripts/agent_play.py`：让 Codex / Claude Code 经 MCP 整局试玩并写报告（含 Windows 下的编码、审批、冷启动超时处理）。
- 技能 `skills/mcp-playtesting`；其余技能卡按新机制与《天宝》的经验更新。
- 通用前端：读取 `/view`，支持奏疏批答、待决事件、目标与危机计、朝会日程、新手引导、终局面板、隐藏数值区间、`DRAFT_UNCONFIRMED` 时放弃草案再推进、对账显示；关键元素带 `data-testid`。

### 变更
- README 重写（英文摘要、功能表、《天宝》展示、MCP 接入、架构图、文档索引）。
- `scenarios/chongzhen_1627` 接入终局机制（16 回合时限、两个失败结局），版本 0.2.0。
- CI 与 `scripts/check.sh` 增加 `ruff check ../scripts`、模板的 content lint（`--strict`）与 20 种子模拟。

## [0.2.x] — 2026-10-16（《天宝》第 1–9 轮）

### 第 1 轮：机制骨架（`d5d908b`, `c033a4f`）
- 事件抉择（PendingChoice，逾期默认项）、结局与失败（优先级判定）、`game_length` 时限与加时、计数器（streak / count）。
- 隐藏数值与公开视图（风闻区间，积弊放大、情报收窄）；危机计（档位与分项）。
- 最小战斗（兵力 × 素质 × 城防 × 天时）与 AI 行军路线；地区控制者、军队统帅、官职兼任。
- 奏疏条目（memorials）：可见条件、谏言人、选项 ops，确认后写 `memorial:<id>`。
- 条件 DSL 扩展（flag_age、counter、region_controller、character_* 、office_holder、army_strength、relation）。
- REST v2（`/view`、抉择、廷议、谏言库、时间线）与 MCP 服务；剧本可配置必需资产 id。

### 第 2 轮：自玩修复（`9bd29ed`, `fd74f5d`）
- 诏书草案持久化（MCP 进程重启后仍可确认）；延期按条款组掷骰；事实带人名地名与原因；结算提速。
- 存档每回合带快照（剧本改动不再追溯改写旧回合，`scenario_changed` 提示，`sim replay` 校验）。
- 廷议立场按议题；寻路调动；奏疏处置写入人物记忆；难度档。

### 第 3 轮：离线叙事（`1b5c097`）
- 离线史官：基于事实的「是季」体纪事；离线召对 / 廷议依人设、立场与事实摘要生成；战报与事实全用显示名；内部事件不展示。

### 第 4 轮：GPT 试玩修复（`684e6d8`, `f0b2b72`）
- 玩家操作分层（`OP_NOT_PERMITTED`，`settings.player_ops`）；`march` 将随军行；谏言人在场校验（`requires`）。
- 危机计按结算后状态只算一次；分段对账；情报精度说明；终局条件可读化与终局复盘；`op_reference` 可按 op / 实体查询。
- `/api/integration`：本机 Codex / Claude Code 的现成接入命令。

### 第 5 轮（`26ad7db`）
- 统一的任命 / 易帅 / 赴任规则（`dispatch`，遥领与已启程）；官职—所在—所统—指挥状态总表；廷议立场指明支持哪个选项。
- 危机变化拆分（漂移、难度加成、事件冲击、诏令、抉择）；经常收支明细与取整差；「下次结算即终局」提示。
- 召对方式（面见 / 书面奏对 / 遣使问对）；`export_log` 结构化复盘；`army_commander` / `army_region` 条件。

### 第 6 轮（`289345e`）
- 廷议按「所议之案」定立场（`proposal`、谏言 `option`）；`DRAFT_UNCONFIRMED`（未确认草案阻止结算，可显式放弃）；季中新出奏疏提示。
- `detach` 分兵；按兵额计费的养兵（`formulas.upkeep`）；剧本级初始账目（`ledger`）；头衔随官职更新；复盘保留关键事件；导出含召对 / 廷议记录。

### 第 7 轮（`087bc30`, `ba66cce`）
- 草案与已确认回执措辞区分（「确认后本季即行」/「已行」）；「待办与在途」排期；回合以历法标签显示（不出现 T19）；大数以「万」表示。

### 第 8 轮（`cd0af0d`）
- 标准库实现的 MCP stdio 服务：握手由数十秒降到约 0.7 秒（引擎在后台加载）；仍建议 Codex 配 `startup_timeout_sec = 60`。
- 敌方人物不可拘拿（`HOSTILE_TARGET`）、`set_character_faction`；在途军队不再显示为已驻目的地；地区指标量程；战报给出因素说明。

### 第 9 轮（`313bbba`）
- 朝会日程上限（`settings.agenda`，`AGENDA_EXHAUSTED`）；远地召对的回奏下季送达。
- 廷议在场限制（`settings.capital_region`，`NOT_PRESENT`），不在京者递书面议状；`/present`。
- 驿程（`courier_days`、`settings.courier`）：远地奏疏晚一季奏到，诏令晚一季令达，军报晚到。
- 政令卡（`policy_cards.yaml`，参数按局势过滤，冷却，`requires`）；离线时文字诏令只记入史册（`OFFLINE_TEXT`）。
- 军令姿态（`set_army_posture`：坚守 / 进击 / 坚壁清野）与将领自主（`settings.autonomy`）；`/armies`。
- 新手引导（`tutorial`，`PUT /flags`）。

## [0.1.0] — 2026-10-15

- 确定性模拟引擎（WorldState、类型化 StateOp、规则校验、种子回合结算）、JSONL 事件溯源存档与分叉。
- OpenAI 兼容 provider 与 Mock、按角色路由、工具调用的 JSON 回退；诏令「草拟 → 预览 → 确认」。
- 场景包（YAML、历法、资产清单、主题 CSS、提示词覆盖）、`sim` CLI、FastAPI、React 前端、技能卡与 CI。
