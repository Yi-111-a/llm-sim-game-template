# 更新日志

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。引擎版本见 `backend/pyproject.toml` 与 `simcore/version.py`；剧本以 `requires_engine` 声明所需版本。

0.2 与 0.3 的引擎扩展都是在制作《天宝》（[Yi-111-a/tianbao](https://github.com/Yi-111-a/tianbao)）的九轮迭代中完成的：每一轮都由 Claude 与 GPT（Codex）经 MCP 整局试玩、写报告，再按报告修复。所有机制保持场景无关，由剧本 YAML 配置；旧剧本无需改动即可继续运行。

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
