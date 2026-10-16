# 剧本写作指南（Scenario Guide）

本文讲怎样写一个场景包（scenario pack）：逐个文件、逐个机制，每节都附可直接复制的小例子。所有例子都取自或对应于
`scenarios/_template`——它很小，但每种机制都有一个可运行的示范；遇到疑问时，最快的办法是打开模板对照。
一个写满的真实剧本见《天宝》的 [`scenarios/tianbao_751/`](https://github.com/Yi-111-a/tianbao)。

> 基本原则（贯穿全文）
> 1. **引擎是唯一事实来源**。剧本只声明数据与规则；数值变化只能来自 op（StateOp）与公式。
> 2. **文字里的每一个状态断言都要有条件守卫**。「北境总兵许定边奏」「御史出缺」「臣在北境」都必须在条件里写明，否则局势一变，文字就成了假话。
> 3. **奏疏先于它所决定的事件**。事件读取 `memorial:<id>` 的批答结果，在奏疏之后结算，不要让事件抢先把事办了。
> 4. **玩家看到的文字里不出现原始 id**（`raid_started`、`memorial:x`、`northern_guard`），一律写显示名。
> 5. **每改一次就跑三件事**：`sim validate`、`scripts/content_lint.py`、`scripts/simulate.py`。

## 目录

1. [场景目录结构](#1-场景目录结构)
2. [scenario.yaml](#2-scenarioyaml)：元信息与历法、资源、地区指标、官职、公式（收入 / 养兵 / 漂移 / 延期）、初始账目、危机计、难度、计数器、目标、结局与时限、战争与 AI 路线、settings
3. [characters.yaml](#3-charactersyaml)：人物、人设、扮演卡
4. [regions.yaml](#4-regionsyaml)：地区、邻接与行程、驿程、指标
5. [factions.yaml 与 armies.yaml](#5-factionsyaml-与-armiesyaml)
6. [条件 DSL](#6-条件-dsl)
7. [StateOp 速查与玩家操作分层](#7-stateop-速查与玩家操作分层)
8. [events.yaml](#8-eventsyaml)：事件、抉择、远地事件
9. [memorials.yaml](#9-memorialsyaml)：奏疏、谏言人、所议之案、变体、requires
10. [policy_cards.yaml](#10-policy_cardsyaml)
11. [新手引导](#11-新手引导-tutorial)
12. [assets.yaml、style.md、theme.css、prompts/](#12-资产与表现层)
13. [校验：sim validate](#13-校验)
14. [平衡：种子模拟](#14-平衡种子模拟)
15. [内容检查清单（content lint）](#15-内容检查清单)
16. [用 GPT / Claude 试玩](#16-用-gpt--claude-试玩)

---

## 1. 场景目录结构

```text
scenarios/<id>/
  scenario.yaml        必需：元信息、历法、资源、官职、公式、结局、settings……
  characters.yaml      人物
  regions.yaml         地区与邻接
  factions.yaml        势力（第一个为缺省玩家势力）
  armies.yaml          军队
  events.yaml          事件（含抉择）
  memorials.yaml       奏疏（每季的决策条目，剧情主线）
  policy_cards.yaml    可选：政令卡
  tutorial.yaml        可选：新手引导（也可写在 scenario.yaml 的 tutorial 键）
  assets.yaml          资产清单
  style.md             美术风格（图像生成提示词的前缀）
  theme.css            可选：只允许 :root 中的 CSS 变量
  prompts/*.md         可选：覆盖内置提示词（同名文件）
```

开始一个新剧本：

```bash
cp -r scenarios/_template scenarios/my_game
# 改 scenario.yaml 的 id、title、version；然后
cd backend && sim validate ../scenarios/my_game
```

`policy_cards.yaml`、`tutorial.yaml` 不存在时不计入剧本内容哈希（旧存档不会因此报「剧本已变更」）。

## 2. scenario.yaml

### 2.1 元信息与历法

```yaml
id: my_game
manifest_version: 1
version: "0.1.0"            # 剧本版本；存档头记录它与内容哈希，剧本改动后 /view 给出 scenario_changed 提示
requires_engine: ">=0.3.0"
title: 新朝开局
era: 架空古代
start_date: "1627-01-01"     # ISO 日期；决定开局季节
turn_unit: season            # month | season | year：影响行程「约 n 季」与驿程换算
currency: 银
calendar:
  eras:
    - {from_turn: 1, name: 新纪元, start_year: 1}                          # 可多段（改元），各段可带自己的 turn_format
    - {from_turn: 9, name: 永和, start_year: 1, turn_format: "{era}{year}载{season}"}
  seasons: [春, 夏, 秋, 冬]
  turn_format: "{era}{year}年{season}"     # 只能用 era / year / season 占位
  number_system: chinese                   # 「元年」「十二年」；arabic 为阿拉伯数字
  pre_game_turns: 0
```

玩家可见的文字里，回合一律显示为历法标签（「新纪元二年秋（第 6 回合）」），不出现 `T6`。

### 2.2 资源

```yaml
resources:
  - {id: treasury, name: 国库, initial: 100, min: 0, unit: 万两}
  - {id: authority, name: 权威, initial: 55, min: 0, max: 100, unit: 点}
  # 隐藏数值：玩家只看到「约 a–b（风闻）」。半宽 = estimate_width ×（1 + 积弊/100）
  - {id: raider_will, name: 北狄战意, initial: 50, min: 0, max: 100, unit: 点, hidden: true, estimate_width: 6}
```

- 隐藏资源在公开视图、简报、StateDigest、危机分项里都只给区间；变化量只给档位（「+3～+5」）。
- **情报**：旗标 `intel:<资源 id>`（或 `intel:<资源 id>:<来源>`）为真时区间减半。缺省精度上限 = 基础半宽 / 2，即**多路情报只减半一次**；要叠加，设 `settings.intel_min_width`（数值，或 `{资源: 数值}`）。选项提示里要写明「只减半一次 / 不叠加」，免得玩家以为能无限收窄。
- 隐藏数值不能出现在人设、奏疏摘要等文字里（「异志 40」会直接泄露真值）。

### 2.3 地区指标与量程

```yaml
stats:
  - {id: prosperity, name: 繁荣, range: [0, 100]}
  - {id: defense, name: 防务, range: [0, 100]}   # 战斗中守方加成 =（1 + 防务/100）；≥ 80 时攻方需两倍战力
  - {id: tax, name: 税基, range: [0, 100]}
```

开局值与一切 `adjust_region` 都截入 `range`，截到边时事实注明「已至上限」。

### 2.4 官职

```yaml
offices:
  - {id: sovereign, name: 君主}
  - {id: chancellor, name: 宰相, requires: {ability: 5}}                         # requires：特质下限
  - {id: frontier_marshal, name: 北境总兵, requires: {ability: 5}, army: northern_guard, seat: frontier}
```

- `army`：此职所统军队——任命即兼统，免职即罢其统帅；`seat`：任所（未写时取所统军队的驻地）。二者都不写为京官。
- 任命规则只有一条：玩家层的任命 / 易帅缺省 `dispatch: true`（人启程赴任，到任前副将暂摄）；剧本层（奏疏、事件）缺省 `false`（遥领）。京官（无 seat、不统兵）写 `dispatch: true` 时，此人启程赴御前（召外官入朝任京职）。回执、事实、官职总表用同一套措辞。
- `settings.max_concurrent_offices` 控制兼职上限（缺省 1）。

### 2.5 诏令类别

```yaml
edict_categories:
  - {id: finance, name: 财政, hint: 税赋、拨款与账目}
  - {id: military, name: 军务, hint: 调兵、驻守与出征}
```

前端按类别给出文本框；接了模型时，文字由诏令解释器转为玩家层 ops。**离线（mock）时文字一律「仅记录」**（回执写「未接模型：御笔只记入史册」），所以剧本的主要决策必须走奏疏、事件抉择与政令卡。

### 2.6 公式：收入、养兵、漂移、延期

公式是安全求值的算术表达式：`+ - * /`、比较、`min` / `max` / `clamp(x, lo, hi)`；变量是资源 id、`turn`，地区公式另有该地区全部 stats 与 `controlled`。

```yaml
formulas:
  income:
    # per_region（缺省 true）：每个地区算一次再相加；controlled_only：只算本方控制的地区
    tax:   {resource: treasury, formula: "tax * 0.02", controlled_only: true, label: 诸地税银}
    grain: {resource: grain, formula: "prosperity * 0.05", controlled_only: true, label: 诸地粮赋}
    canal: {resource: grain, formula: "10", per_region: false, label: 漕运}   # 全国一笔；可用 counter_<id>、controlled_regions
  upkeep:   # 养兵：按本方各军「现有」兵额逐季计费，增兵即增此项（factions / armies 可限定范围）
    - {resource: grain, per: 10000, amount: 2, label: 军粮}
  drift:    # 每季漂移（结算第 9 步），乘难度 drift_scale
    border_alarm: "0.5 + 0.1*(raider_will-50) - 0.05*(authority-55)"
    raider_will: "0.5"
  execution:   # 诏令「有司稽迟」的延期概率 =（base_delay + 积弊×corruption_factor − 权威×authority_factor）× 难度 delay_scale
    authority_resource: authority
    corruption_resource: corruption
    base_delay: 0.02
    authority_factor: 0.0002
    corruption_factor: 0.0005
```

- 延期的掷骰按**条款组**进行：同一奏疏选项 / 同一张政令卡 / 同一段诏令要么一起执行，要么一起延期，不会拆成一半。
- 季报的对账（期初 → 诏令 → 延期执行 → 经常收支 → 事件 / 选择 → 期末）与经常收支明细会按 `label` 归类，所以每个收入 / 养兵项都写上 `label`。

### 2.7 初始账目

```yaml
ledger:   # 开局即有的经常账目；T0→T1 的第一次结算就计入。剧本定额缺省不可由玩家撤销
  - {id: capital_stipend, name: 京官俸米, category: grain, amount: -1.5, reason: 京官俸禄}
  - {name: 修河专款, category: treasury, amount: -5, kind: recurring, turns: 4, cancellable: true}
```

> 教训：T1 才由事件设立的经常账目，T0 那次结算就漏记了。固定开支请写在这里，或在设立时同时补扣首季。

### 2.8 危机计

```yaml
crisis:
  resource: border_alarm          # 用哪个资源当危机计（可以是隐藏资源：此时总值与变化只给区间）
  title: 边警
  bands:
    - {max: 37, name: 安靖}
    - {max: 59, name: 烽燧}
    - {max: 100, name: 告急}
  terms:   # 与 drift.border_alarm 的分项一一对应，各项之和 = 每季漂移
    - {id: base, label: 边地常警, expr: "0.5"}
    - {id: will, label: 北狄战意, expr: "0.1*(raider_will-50)"}       # 引用隐藏资源的分项只显示区间
    - {id: authority, label: 朝廷威望, expr: "-0.05*(authority-55)"}
```

引擎在每季结算后给出分项（下次结算的预计漂移）和「本季危机变化」拆分（漂移、难度加成、诏令、抉择、事件冲击、截断）。分项必须与漂移公式一致，否则玩家看到的解释与实际对不上。压力项建议只计超出部分（`max(0, …)`）并加上限，避免「一味示好把危机存成负数」。

### 2.9 难度档

```yaml
difficulties:
  - {id: easy, label: 宽和, description: 北狄战意增长较慢，府库稍丰。, drift_scale: {raider_will: 0.5}, start: {treasury: 120}, delay_scale: 0.5}
  - {id: normal, label: 常例, description: 标准设定。}
  - {id: hard, label: 多事, description: 事件更频，府库开局即紧。,
     drift_scale: {raider_will: 1.3, border_alarm: 1.15}, gravity_scale: {"*": 1.2, envoy: 1}, start: {treasury: 80}, delay_scale: 1.5}
default_difficulty: normal
```

`drift_scale`（漂移乘数，键为资源 id 或 `*`）、`gravity_scale`（事件概率乘数，事件 id 优先于 `*`，结果截到 1）、`start`（开局资源）、`delay_scale`（延期概率乘数）。开局 `new_game(difficulty=…)` / `POST /sessions {difficulty}` 选档。用 `scripts/simulate.py --difficulty hard` 分别验证每一档。

### 2.10 计数器

```yaml
counters:
  - {id: broke_streak, mode: streak, conditions: {kind: resource, id: treasury, operator: lte, value: 0}}   # 不满足即归零
  - {id: alarm_seasons, mode: count, conditions: {kind: resource, id: border_alarm, operator: gte, value: 60}}  # 只累加
```

条件里用 `{kind: counter, key: broke_streak, operator: gte, value: 2}` 读取。前缀 `_agenda:` 为引擎保留。

### 2.11 目标

```yaml
objectives:
  - id: border
    title: 北境安靖
    detail: 终局时边警 < 38，且北境不失。     # 写清楚判定口径；提前胜利 / 失败的条件也要写进来
    conditions: {kind: resource, id: border_alarm, operator: lt, value: 38}
    fail_conditions: {kind: region_controller, region: frontier, faction: raiders}
```

状态为 `on_track / at_risk / failed / achieved`。

### 2.12 结局、失败与时限

```yaml
game_length: {last_turn: 8, overtime_flag: raid_started, overtime_last_turn: 10, fallback_ending: uneasy_peace}

endings:
  - id: fail_capital
    kind: fail                      # 失败结局
    priority: 1                     # 每季结算末尾按 priority 从小到大判定，命中第一个即终局
    title: 京城失守
    conditions: {kind: region_controller, region: capital, faction: raiders}
    text: 北狄铁骑直抵城下，京城不守。【达成条件】京城为北狄所据。
  - id: border_secured
    priority: 10
    title: 北境安靖
    illustration: ending.border_secured     # 可选；写了就必须在 assets.yaml 中存在
    conditions:
      all:
        - {kind: turn, operator: gte, value: 8}
        - {kind: resource, id: border_alarm, operator: lt, value: 38}
    text: 新朝初年，边地烽燧渐稀……【达成条件】终局时边警 < 38。
  - id: uneasy_peace
    priority: 99
    title: 隐忧未除
    conditions: {kind: turn, operator: gte, value: 999}   # 只作 fallback_ending
    text: 时限已至……（开放结局。）
```

- 到 `last_turn` 仍无结局命中即用 `fallback_ending`；`overtime_flag` 为真时时限延至 `overtime_last_turn`（例如「乱已起则打完再说」）。
- 下一次结算就到时限时，引擎自动给出「下次结算即终局」提示——不必再写同义事件。
- 终局后不能再下诏、处置、推进，但 `rewind` / `fork` 仍可用；`/view.review` 给出终局复盘。
- **结局文字只写条件保证的事**。「朝堂议论渐平」须有朝争上限条件，「旧部受代」须有兵权条件；同一标题按状态分几段写（如《天宝》E1 按朝争高低分两段）。末尾写一句【达成条件】，玩家复盘时一目了然。
- 「最差选择也能拿好结局」是常见平衡事故：用 `simulate.py --policy last` 检查。

### 2.13 战争与 AI 行军

```yaml
wars:
  - {a: court, b: raiders}         # 也可写 [court, raiders]；与 relations < 0 一样视为敌对

ai_routes:
  - faction: raiders
    route: [steppe, frontier, pass, capital]    # 不在途、未与敌相持的军队每季沿路线前进一格
    hold_if_outnumbered: true                   # 本区兵力（× 素质）不少于前方敌军才进兵
    armies: [raider_host]                       # 可选：只驱动这些军队
    conditions: {kind: flag, key: raid_started, value: true}
```

战斗：同区敌对军队交战。战力 = 兵 × 素质 ×（守方 1 + 防务%）× 军令系数 × 将才（`settings.battle.commander_factor`，可选）× 天时（±15%）；败方折三成并退往相邻己方地区（无路则溃散），胜方折一成；攻方得胜且守军尽去则易主。战报给出全部因素。

### 2.14 廷议议题

```yaml
debate_topics:
  - {id: frontier, keywords: [北境, 边饷, 北狄], stances: {wen: oppose, wu: support}}   # 键为 bloc 或人物 id
settings:
  bloc_names: {wen: 文臣, wu: 武臣}                 # 理由里的派系显示名（不写则只说「本派」，绝不暴露 id）
  bloc_interests: {wu: {northern_guard: 30, 北境: 20}}   # 派系对实体 / 关键词的利害权重
```

立场的优先级：奏疏 `advisors` → `debate_topics` → 议题点名的实体与亲疏 → 忠诚 → 种子决断（详见 [PROMPTS.md](../PROMPTS.md)）。

### 2.15 settings

```yaml
settings:
  player_office: sovereign          # 玩家本人 = 此官职在任者；或 player_character: <人物 id>
  player_faction: court             # 缺省为第一个势力
  ruler_title: 陛下                 # 诏书记忆里的称谓
  max_concurrent_offices: 2
  hidden_traits: [ambition]         # 不公开的人物特质
  trait_names: {rashness: 轻躁}     # 特质显示名
  afterlife_chat: false             # 死者能否召对
  capital_region: capital           # 京城：廷议在场、驿程、御前的基准（缺省为玩家所在地）
  agenda: {audience_per_season: 2, debate_per_season: 1}   # 朝会日程上限；remote_reply: next_season | immediate
  courier: {late_threshold_days: 60}                      # 驿程超过此日数：奏疏、军报晚一季到，诏令晚一季达
  autonomy:                                               # 将领自主：对有统帅之军的军令，按统帅特质算拒命 / 擅动概率
    formula: "min(0.6, 0.06 * max(0, 6 - loyalty) + 0.04 * max(0, ambition - 6))"
    by_order: {hold: "min(0.6, 0.06 * max(0, rashness - 5))"}  # march | move | detach | hold | attack | scorch
    outcomes: {delay: 0.5, alter: 0.3, sortie: 0.2}            # 迁延一季 / 改作他令 / 擅自出战
    levels: [0.08, 0.2]                                         # 风险「低 / 中 / 高」的分界（回执只给等级，不给概率）
  postures: {hold: {defense_bonus: 0.2}}                    # 覆盖军令姿态系数（hold / attack / scorch）
  battle: {commander_factor: "1 + 0.04*(ability - 5)"}     # 将才：统帅亲在军中时战力乘此式（变量为统帅特质，缺省 5；截在 0.5–1.5）
  player_ops: {allow: [], deny: [], statuses: [imprisoned, exiled, retired]}   # 玩家层白名单的调整，见 §7
  intel_min_width: 2                                        # 情报精度下限（半宽）；不写则情报只减半一次
  flag_labels: {raid_started: 北狄寇边}                     # 结局条件与复盘里的旗标显示名
  counter_labels: {broke_streak: 国库告罄连续季数}
  turn_unit_label: 季                                       # 行程文字的单位（缺省按 turn_unit）
  player_project_max_gain: 10                               # 玩家工程每项指标增益上限
  player_project_cost_per_point: 10                         # 每点增益至少花费
  troop_hint_min: 2000                                      # content_lint W14：选项改动兵额达此数须在提示里写明
```

**朝会日程与在场**：召对（面见、书面奏对、遣使问对）与廷议各有每季上限，用完返回 `AGENDA_EXHAUSTED`；不在京者不能当廷议（`NOT_PRESENT`），只能递书面议状、下季送达；远地召对的回奏下季才到。这些限制让「问遍所有人再决定」变成需要取舍的资源——写奏疏时也要照顾这种真实感：远方的将领不会「当廷」进言。

## 3. characters.yaml

```yaml
- id: general
  name: 许定边
  title: 北境总兵                    # 头衔；官职改变后人设卡自动显示现任官职，旧头衔标「旧衔」
  faction: court
  office: frontier_marshal            # 主职；兼职写 offices: [a, b]
  bloc: wu                            # 派系（廷议立场）
  region: frontier
  traits: {ability: 6, loyalty: 6, ambition: 7}   # 0–10；hidden_traits 中的不公开
  persona: 长年驻守边地的将领，重视粮道和侦察，对朝中文臣的掣肘多有怨言。
  speech_style: 直接务实，少作夸饰。
  knowledge: [边防, 行军, 漠北部落]    # StateDigest 会优先给出这些地区 / 话题相关的事实
  card:                               # 扮演卡：引擎不读状态，只给召对 / 廷议的模型看
    greeting: 末将许定边，边事紧要，长话短说。
    examples:
      - 斥候不出塞，北狄虚实便只是风闻。
```

- `persona` 只写**不随局势变化**的性格与立场；不要写「现任某职」「正在某地」「年事已高、明年将卒」，也不要写隐藏数值。会变的事实由引擎的 StateDigest 提供。
- 开场白里的旧官衔会被引擎替换为现任官职；但最好别在 greeting 里写官衔。
- 召对时模型只拿到：人设卡 + 该人物可知的事实摘要（所在、相邻、knowledge 中的地区）+ 他的记忆（最近 12 条）。死者、系狱者拒绝召对。

## 4. regions.yaml

```yaml
- id: frontier
  name: 北境
  controller: court                  # 缺省为玩家势力
  courier_days: 40                   # 驿程日数（未写时按到京城的行程估 3 / 15 / 40 / 70 日）
  map: {x: 76, y: 16, polygon: [[68, 9], [83, 6], [89, 17], [78, 24], [68, 21]]}
  adjacency:
    - {to: pass, travel_turns: 1}    # 行程回合数；寻路取总行程最短
    - {to: steppe, travel_turns: 1}
  stats: {prosperity: 35, order: 50, defense: 40, tax: 4}
```

- 调动可以指向不相邻的目标，引擎寻路并整段出发（「自京城启程赴北境（经雁口关，约 2 季）」）；在途者不能再调。
- **不要在文字里写死行程**（「两季可至」）：寻路结果会随控制权变化。写「行程见回执」。
- `courier_days` 超过阈值的地区：奏疏显示「某月某日自某地奏到」并晚一季出现；作用于该地的诏令条款整组延至下季（「驿程遥远，令达于下季」）；该地事件的军报晚一季送达（状态即时改变）。
- 外域地区（非玩家控制）在摘要中只给控制者与驻军。

## 5. factions.yaml 与 armies.yaml

```yaml
# factions.yaml —— 第一个势力为缺省玩家势力
- {id: court, name: 朝廷, relations: {raiders: -40}}
- {id: raiders, name: 北狄, relations: {court: -40}}

# armies.yaml
- {id: northern_guard, name: 北境守军, faction: court, region: frontier, strength: 8000, commander: general}
- {id: capital_guard, name: 禁军, faction: court, region: capital, strength: 6000}        # 无统帅：副将暂摄
- {id: raider_host, name: 北狄骑兵, faction: raiders, region: steppe, strength: 9000, quality: 1.1, commander: raider_chief}
```

敌对（关系 < 0 或在 `wars` 中）势力的人物不可拘拿；兵变首领应随叛军 `set_character_faction`。

## 6. 条件 DSL

条件可嵌套 `all` / `any` / `not`，列表等同 `all`。比较符 `eq | gt | gte | lt | lte`（缺省 `eq`）。

| kind | 字段 | 例 |
|---|---|---|
| `turn` | `value` | `{kind: turn, operator: gte, value: 3}`（指正在结算进入的回合） |
| `resource` | `id, value` | `{kind: resource, id: treasury, operator: lte, value: 0}` |
| `flag` | `key, value` 或 `operator: exists / absent` | `{kind: flag, key: "memorial:m_frontier_pay", value: deny}` |
| `flag_age` | `key, operator, value` | 旗标设下后经过的回合：`{kind: flag_age, key: raid_started, operator: gte, value: 2}` |
| `counter` | `key, operator, value` | `{kind: counter, key: broke_streak, operator: gte, value: 2}` |
| `region_stat` | `region, stat, operator, value` | `{kind: region_stat, region: frontier, stat: prosperity, operator: lt, value: 35}` |
| `region_controller` | `region, faction` | `{kind: region_controller, region: capital, faction: raiders}` |
| `character_status` | `character, value` | `{kind: character_status, character: scholar, value: alive}` |
| `character_trait` | `character, trait, operator, value` | `{kind: character_trait, character: general, trait: loyalty, operator: lte, value: 3}` |
| `character_region` | `character, region, arrived?` | `{kind: character_region, character: general, region: frontier}`；`arrived: true` 时在途者不算（启程赴任者的 region 仍是出发地，要对他下 `move_character` 之类的令就加这一条） |
| `office_holder` | `office, character`（`null` = 出缺） | `{kind: office_holder, office: censor, character: null}` |
| `army_strength` | `army` 或 `faction`，`operator, value` | `{kind: army_strength, faction: raiders, operator: lte, value: 7000}` |
| `army_commander` | `army, character`（`null` = 无统帅），`operator: eq / ne` | `{kind: army_commander, army: capital_guard, character: null}` |
| `army_region` | `army, region`，`operator: eq / ne` | `{kind: army_region, army: capital_guard, region: frontier}` |
| `relation` | `a, b, operator, value` | `{kind: relation, a: minister, b: general, operator: lt, value: 0}` |

批答过的奏疏写旗标 `memorial:<id> = <选项 id>`；处置过的抉择写 `choice:<id>` 与 `event:<事件 id>:choice`；触发过的事件写 `event:<事件 id>:fired`。这些都可以在条件里读。

## 7. StateOp 速查与玩家操作分层

| op | 主要字段 | 玩家层 |
|---|---|---|
| `adjust_resource` | `resource, delta, reason` | 否 |
| `adjust_region` | `region, stat, delta` | 否（工程竣工可） |
| `adjust_relation` / `adjust_trait` | — | 否 |
| `adjust_army` | `army, delta?, quality_delta?, reason` | 否（`quality_delta` 调素质，截在 0.1–3.0；整训、括虚额、器甲用它） |
| `merge_army` | `army, into, reason` | 否（偏师归建：兵并入同势力的 `into`，素质按兵额加权，原军归零） |
| `set_flag` / `adjust_counter` / `set_counter` | — | 否 |
| `add_ledger` | `name, category, amount, kind: one_off\|recurring, turns?, cancellable?, id?` | 否（写 `id` 则同 id 替换、可被 `cancel_ledger` 指名撤销） |
| `cancel_ledger` | `ledger_id` | 是（不可撤销的除外） |
| `appoint` / `dismiss` | `character, office, dispatch?` | 是 |
| `set_character_status` | `character, status, cause` | 是（仅系狱 / 流放 / 致仕，对象须为本方或在本方控制之地；`HOSTILE_TARGET`） |
| `set_character_faction` / `set_army_faction` / `set_region_controller` | — | 否 |
| `move_character` | `character, to_region` | 是 |
| `move_army` / `march` | `army, to_region, with_commander = true` | 是 |
| `detach` | `army, strength, to_region, new_army_name?, commander?` | 是（分兵；全军请用 move_army） |
| `set_army_commander` | `army, character \| null, dispatch?` | 是 |
| `set_army_posture` | `army, posture: hold\|attack\|scorch` | 是 |
| `start_project` | `name, region, cost_per_turn, turns, on_complete` | 是（增益受上限约束） |
| `add_standing_order` / `cancel_standing_order` | `text, ops, until_turn?` / `id` | 是（所含 ops 同样按玩家层检查） |
| `remember` | `character, fact` | 是 |
| `choose_event_option` | `choice_id, option_id` | 否（用 choose 工具） |

- **剧本层**（奏疏选项、事件效果、抉择、工程竣工、常设令执行）可用全部 op；**玩家层**（客户端模型的 ops、诏令文本解释、政令卡）只有白名单。越权返回 `OP_NOT_PERMITTED`，说明正确途径。
- 调整白名单：`settings.player_ops` 写列表 = 整体替换；写 `{allow, deny, statuses}` = 追加 / 移除 / 指定可用处置（加 `dead` 即允许赐死）。
- 数值类事实自动写成「国库 −20（北境冬衣）」；`reason` 就是玩家看到的原因，请写人话。

## 8. events.yaml

```yaml
- id: raid_begins
  title: 北狄寇边
  kind: conditional               # scheduled | conditional | random 需要插图资产；system 为记账事件（不展示）
  text: 北狄骑兵越境南下，北境告急。
  region: frontier                # 远地（驿程超阈值）则军报晚一季
  when: {turn: 3}                 # 可选；与 {kind: turn, value: 3} 同义
  conditions:
    all:
      - {kind: flag, key: raid_started, operator: absent}
      - any:
          - {kind: resource, id: border_alarm, operator: gte, value: 40}
          - {kind: flag, key: "memorial:m_frontier_pay", value: deny}     # 由奏疏的批答驱动
  gravity: 0.6                    # 条件满足时的触发概率（× 难度 gravity_scale）
  repeatable: false               # true 时配合 cooldown（回合）
  importance: 2                   # 可选：展示优先级（终局复盘的取舍也参考它）
  effects:
    - {op: set_flag, key: raid_started, value: true}
    - {op: adjust_resource, resource: border_alarm, delta: 8, reason: 北狄寇边}
  illustration: event.raid_begins.illustration
```

**事件抉择**：

```yaml
- id: envoy
  title: 北狄遣使
  kind: scheduled
  text: 拓跋烈遣使至京，请开边市，言辞倨傲。
  when: {turn: 2}
  gravity: 1
  choices:
    - id: receive
      text: 厚礼相待，许开边市
      effects: [{op: adjust_resource, resource: raider_will, delta: -6, reason: 许开边市}]
      memory: {character: raider_chief, text: 新纪元初，南朝厚礼相待，许开边市}
    - id: hostage
      text: 扣留使者，以为人质
      conditions: {kind: resource, id: authority, operator: gte, value: 60}   # 不满足时不可选
      effects: [{op: adjust_resource, resource: authority, delta: 3, reason: 扣留北狄使者}]
  default_choice: receive          # 逾期执行
  choice_deadline: 1               # 季
```

要点：

- **奏疏先于它决定的事件**。若奏疏问「是否出兵」，出兵的事件就必须读 `memorial:<id>`，在奏疏之后结算；不要写一个固定回合的事件先把兵派出去（《天宝》自玩 #14：「颜真卿已出守，奏疏才问是否出守」）。
- 事件效果「按固定军队 id 扣兵」要小心：局势变化后那支军队可能已换了阵营（#23）。
- 记账性质的事件（只改内部旗标）写 `internal: true` 或 `kind: system`，不会出现在玩家的事件列表里。
- 同一事件未决时不会重复触发；repeatable 事件用 cooldown 防刷屏。
- **同一季的事件按文件顺序判定，前面事件的效果会影响后面事件的条件。**《天宝》第 10 轮：「范阳安堵」（危机计 −8）排在「范阳起兵」之前，危机计高时两者同季都触发，结算卷轴上既写安堵又写起兵。不要靠挪顺序修——挪到起兵之后，安堵的 −8 就再也救不了当季，稳健打法胜率掉了 5 个百分点。正确做法是给前一个事件加守卫：**它的效果之后仍必然触发后一个事件时，前一个就不触发**（危机计 ≥ 起兵门槛 + 8 时不报安堵）。改完扫几百个种子确认「同季同现」为 0，再跑平衡模拟确认胜率不变。

## 9. memorials.yaml

奏疏是每季的决策条目，也是剧情的主线。采纳某个选项就把它的 ops 加入诏书草案；确认诏书后写入 `memorial:<id> = <选项 id>`。

```yaml
- id: m_frontier_pay
  turn: 0                     # 可见回合 [turn, turn + expires_after]；也可只写 conditions
  expires_after: 1
  title: 北境请饷
  summary: 北境总兵许定边奏：边军冬衣未备，请拨银二十万两。
  region: frontier            # 显示「某月某日自北境奏到」；远地晚一季
  conditions: {kind: office_holder, office: frontier_marshal, character: general}   # 守卫 summary 里的「北境总兵许定边」
  proposal: grant             # 「所议之案」：廷议的赞成 / 反对针对它
  keywords: [北境, 边饷]       # 廷议议题匹配
  advisors:
    - {character: general, stance: support, option: grant, line: 边军衣甲破敝，冬衣不备，一冬难熬。, requires: {office: frontier_marshal}}
    - {character: minister, stance: oppose, option: half, line: 国用方紧，宜先拨其半，余俟秋税。, requires: {office: chancellor}}
  options:
    - id: grant
      label: 照准，拨银二十万两
      ops:
        - {op: adjust_resource, resource: treasury, delta: -20, reason: 北境冬衣}
        - {op: adjust_resource, resource: border_alarm, delta: -3, reason: 边军得饷}
      memory: {character: general, text: 开岁之初，陛下准拨北境冬衣银二十万两}   # 采纳时写入人物记忆
      hint: 国库 −20，边警 −3                                                   # 玩家看到的代价
    - id: deny
      label: 驳回
      ops: [{op: adjust_resource, resource: authority, delta: 1, reason: 驳回请饷}]
      hint: 权威 +1；边军怨望，第三季起北狄或乘虚寇边
```

**谏言人**：

- 已故、系狱、流放者自动不显示（致仕者仍可进言）。`requires: {office, region, alive}` 进一步约束：`office` 须任其一，`region` 须身在其一且不在途；`alive: false` 允许引用已不在者的「旧议」。
- 第一人称说「臣在京」「臣督办」的谏言必须用 `requires.region` 保证其人确实在那里；出镇在外者另写远方来奏的口吻。
- `option` 写明此人主张哪个选项；`proposal` 写明所议之案。几个方案并列（不是一准一驳）时一定要写，否则廷议只能「诸案并陈，未有定案」。

**按局势变化的变体**：同一议题在不同局势下应换一套说法，用 `variant_of` 声明，并在条件里彼此排斥，免得局势一变又冒出一条同类奏疏、重复领取收益：

```yaml
- id: m_harvest_good
  turn: 1
  title: 秋粮入仓
  conditions:
    all:
      - {kind: region_stat, region: frontier, stat: prosperity, operator: gte, value: 35}
      - {not: {kind: flag, key: "memorial:m_harvest_poor", operator: exists}}
  options: [...]
- id: m_harvest_poor
  variant_of: m_harvest_good
  turn: 1
  title: 北境歉收
  conditions:
    all:
      - {kind: region_stat, region: frontier, stat: prosperity, operator: lt, value: 35}
      - {not: {kind: flag, key: "memorial:m_harvest_good", operator: exists}}
  options: [...]
```

写作要点（都来自 GPT / Claude 的整局试玩报告）：

- **每个状态断言都要有守卫**：「某官某人」→ `office_holder`；「某职出缺」→ `office_holder … null`；「某人在某地」→ `character_region` 或谏言 `requires.region`；「禁军久无宿将」→ `army_commander … null`。固定在 T0 且不延续的条目可以依赖开局状态。
- **选项名实相符**：标题说「另择」「任命」「分兵一部」「出兵」，ops 里就要有 `appoint` / `detach` / `march`；真在后续事件里任命的，写 `appoints_via: <事件 id>`。调全军写「整军」，调一部用 `detach`。
- 选项摘要由引擎生成（`ops_summary`），隐藏数值只写方向（「北狄战意 ↓（风闻）」）；`hint` 由你写，要与 ops 一致。
- **末季**（`last_turn − 1`）的奏疏若选项都在调兵，另给一个「维持现状」，并在提示里提醒行程——调动预计抵达晚于时限时引擎会注明「本局内到不了」。
- 启用 `formulas.upkeep` 后，增兵的选项要写明此后的养兵负担。
- 每季至少给 2–3 条可批的奏疏（`simulate.py --coverage` 检查）；中后期避免同质（赈济、漕运反复出现）。
- 远地奏疏：可见季写在 `turn` 上；写在 conditions 里时，奏到那一季条件必须仍成立。

### 局势变了，旧奏疏要换说法

固定回合的史事奏疏默认「历史照常进行」。玩家一旦改变了前提，就要给它做变体（`variant_of` + 互斥守卫，见 E4），否则会出现《天宝》第 10 轮那样的矛盾：

- **人已不在其位**：安禄山已被留京夺镇，后面的「禄山请讨契丹」「禄山岁贡」「禄山请增兵」仍写成他从范阳上奏。变体改由新帅或「范阳诸将」上奏，原条加 `not` 留京旗标。
- **人已不在京**：杨国忠已出镇剑南，奏疏谏言仍写「当面力谏」「入辞，泣言」。远地人物的谏言写成「驰表」「表奏」，注明发出地点；在京与不在京各给一条带 `character_region` 守卫的谏言。
- **数目要写全**：选项改动 ≥ 2000 兵时，标题或提示里写出这个数（W14）；调兵用 `detach` 分出新军，而不是直接扣掉原军兵额——扣掉的兵就从地图上消失了。

## 10. policy_cards.yaml

政令卡是「奏疏之外朝廷日常能做的事」，玩家选卡、填参数，展开为诏令条款（回执 `source: card`）。

```yaml
- id: card_appoint
  category: 人事              # 军务 | 内政 | 人事 | 外交 | 财计
  title: 拜官
  summary: 以一位在京之人任一出缺之职。
  params:
    - {name: who, kind: character, label: 人选, filter: {present: true, can_hold_office: true}}
    - {name: office, kind: office, label: 官职, filter: {vacant: true}}
  ops:
    - {op: appoint, character: "{who}", office: "{office}"}             # 整值替换（保留类型，须加引号）
    - {op: remember, character: "{who}", fact: "蒙陛下拜为{office.name}"}   # {参数.name} 换成显示名
  cost_hint: 无。每两季限用一次。
  cooldown: 2                  # 季；有冷却的卡一道诏书只能用一次
  requires: {kind: resource, id: treasury, operator: gte, value: 30}   # 可选：不满足时不可用
```

参数 `kind`：`character | office | army | region | amount | choice`（amount 用 `min / max / default`，choice 用 `choices`）。`filter` 词汇：

| kind | filter |
|---|---|
| character | `faction`（own 缺省 / any / enemy / 势力 id）、`present`（在京且不在途）、`idle`、`holds_office`、`can_hold_office`、`commands_army`、`region`、`bloc`、`min_trait`（只许公开特质） |
| office | `vacant`、`with_army`、`ids` |
| army | `faction`、`idle`、`has_commander`、`min_strength`、`region` |
| region | `controller`（own / enemy / any 缺省 / 势力 id）、`has_own_army`、`not_capital`、`ids` |

规则：卡片只能用玩家层 op（引擎校验）；**卡片不直接改国势数值**，国势的起落以奏疏为主。钱粮类卡片写成 `start_project`，竣工效果每项 0 < delta ≤ 10，且增益合计 × 10 ≤ 总花费，否则玩家能用卡片「刷」指标。军务卡受将领自主影响，回执显示风险等级。

## 11. 新手引导（tutorial）

```yaml
tutorial:
  - {turn: 0, step: 1, title: 看国势, target: desk.resources, text: 顶栏列出国库、粮储与权威……}
  - {turn: 0, step: 2, title: 批答奏疏, target: memorial.list, text: 每条奏疏选一项批答……}
  - {turn: 0, step: 3, title: 颁诏与推进, target: desk.advance, text: 先预览回执、确认诏书，再推进一季。}
```

`target` 是前端的 `data-testid`（`<画面>.<元素>`，末段可为 `*`）；同一季 step 从 1 连续编号。`/view.tutorial` 给出当季步骤，`PUT /sessions/{id}/flags {tutorial: false}` 关闭。模板前端已有 `desk.resources`、`memorial.list`、`memorial.item.<id>`、`memorial.option.<id>.<选项>`、`edict.preview`、`edict.confirm`、`desk.advance`、`choice.list`、`ending.panel`。

## 12. 资产与表现层

```yaml
# assets.yaml
manifest_version: 1
entries:
  - {id: character.general.portrait, kind: portrait, used_by: "character:general:portrait",
     path: assets/characters/general/portrait.webp, size: [768, 1024], format: webp,
     prompt: "边将肖像，甲衣与风尘，北地关隘背景", status: missing}
  - {id: event.raid_begins.illustration, kind: event, used_by: "event:raid_begins:memorial",
     path: assets/events/raid_begins.webp, size: [1344, 768], format: webp, prompt: "骑兵越过边墙，烽火台燃起狼烟", status: missing}
```

- 必需槽位缺省为 `character.{id}.portrait`、`region.{id}.card` 与 5 个 `ui.*`；`scheduled / conditional / random` 事件需要 `event.<id>.illustration`（或事件 `illustration` 指定的 id），结局写了 `illustration` 也必须存在。
- 改命名方案：

  ```yaml
  # scenario.yaml
  asset_requirements:
    character: "portrait.{id}.default"   # null 关闭该类要求
    region: null
    ui: [map.base, ui.desk]
  ```

- 缺文件时 `/api/assets/{scenario}/{id}` 返回 SVG 占位图；`sim gen-assets <id> --only missing` 按 `style.md` + 种类模板 + 条目 prompt 生成并回写 sha256。图像里不要有文字（界面文字一律由 DOM 渲染）。
- `theme.css` 只允许 `:root` 中的 CSS 变量；`prompts/<内置文件名>.md` 覆盖内置提示词（只影响本场景），`prompts/chronicle_templates.yaml` / `audience_templates.yaml` 覆盖离线纪事 / 召对模板。

## 13. 校验

```bash
cd backend
sim validate ../scenarios/my_game
```

`sim validate` 检查结构与引用：YAML 结构、id 唯一、人物 / 地区 / 军队 / 官职 / 势力 / 资源引用、所有 op 的结构、条件 DSL（未知 kind、比较符、缺字段）、奏疏的谏言人 / option / proposal / requires、事件抉择与 default_choice、结局与 game_length、难度档、养兵、初始账目、player_ops、agenda / courier / autonomy / postures、政令卡（参数、filter、占位符、白名单）、tutorial、资产清单（尺寸、路径不出场景目录、必需槽位）、文本长度。错误逐条列出，退出码 1。

`sim replay <session>` 用当前剧本从开局重放存档，逐回合比对状态哈希（剧本改动后检查旧存档用）。

## 14. 平衡：种子模拟

```bash
cd backend
python ../scripts/simulate.py ../scenarios/my_game --seeds 200                 # none / first / last / random 四种策略
python ../scripts/simulate.py my_game --policy last --difficulty hard
python ../scripts/simulate.py my_game --policy first --seed 3 --trace          # 单局逐季轨迹（奏疏、抉择、事件、战斗）
python ../scripts/simulate.py my_game --backend service --seeds 20             # 走真实的 draft → confirm → end_turn（含延期）
python ../scripts/simulate.py my_game --coverage                               # 每季最少可见奏疏条数
```

输出每种策略的结局分布、平均终局回合、危机计终局值与峰值、目标达成率。模板的参考结果（200 种子、常例）：

| 策略 | 结果 |
|---|---|
| none（不批奏疏） | 「隐忧未除」约 87%，「北境安靖」约 13% |
| first（处处取首项） | 「隐忧未除」100%（处处示好：边警低，但权威不足） |
| last（处处取末项） | 「隐忧未除」100%（驳饷招来寇边，时限延长） |
| random | 「北境安靖」约 40% |
| 「明君」（脚本内自定义：照准、遣斥候、任御史、拒使……） | 常例 100%，多事约 84% |

平衡目标建议：

- 「不作为」应当大概率落到中下结局；「处处取首项 / 末项」这种不看局势的策略不应拿到最好结局；
- 用心的策略在常例下应能稳定达成好结局，在高难度下仍有相当失败率；
- 每个结局（含失败）都应可达——为难以触达的结局写「探针」策略；
- 危机计的终局值分布不要贴着上下限。

需要命名策略（「明君」「史实」「各结局探针」）时，照《天宝》的 `tools/sim_policies.py` 写一个剧本专用脚本：在 `simulate.py` 里策略就是一个函数 `policy(options, rng) -> option_id`，可以导入 `play_engine` 复用（见上表最后一行的写法）：

```python
import sys; sys.path.insert(0, "../scripts")
import simulate as S
WISE = ["grant", "scout", "appoint", "store", "relief", "rebuff", "hold"]
def wise(options, rng):
    return next((o for o in WISE if o in options), options[0] if options else None)
scenario = S.load_scenario(S.resolve_pack("my_game"))
S.report("wise", [S.play_engine(scenario, wise, seed, None, 60) for seed in range(200)], "engine", None, "边警")
```

engine 后端直接执行采纳的 ops，不掷延期骰、不计驿程与将领自主，适合看结局分布；要看精确规则，用 `--backend service`。

## 15. 内容检查清单

`scripts/content_lint.py` 检查「文字是否与状态相符」——这一类错误 `sim validate` 查不出来，却是 GPT / Claude 整局试玩报告里最多的：

```bash
python ../scripts/content_lint.py ../scenarios/my_game            # 有错误时退出码 1
python ../scripts/content_lint.py my_game --strict                # 警告也算失败（CI 对模板这样跑）
```

| 代码 | 检查 | 来源（《天宝》试玩） |
|---|---|---|
| E1 | op、谏言人、memory 引用的人物 / 地区 / 军队 / 官职 / 资源 / 势力都存在 | 引入未建模的人物 |
| E2 | 玩家可见文字（标题、摘要、谏言、选项、提示、结局、目标、引导、卡片、人设）不出现原始 id、snake_case 键、`memorial:` 之类 | 纪事泄露内部旗标 id |
| E4 | 同标题奏疏须以 `variant_of` 声明，且条件中互相排除 | 同季冒出两条同类奏疏、重复领取收益 |
| E5 | 政令卡占位符有定义、参数都被用到、`cost_hint` 不空 | — |
| E6 | 引导 target 形如「画面.元素」、step 连续、指向的奏疏当季可见 | — |
| W1 | 条件读取的旗标没有任何 op 设置（拼写错误？） | — |
| W2 | 文字称「某官某人」「某官出缺」而无 `office_holder` 守卫 | 已任京兆尹仍称出缺；在任却说「已不在其位」 |
| W3 | 选项标题说任命 / 分兵 / 出兵，ops 却没有做 | 「另择留后」没有任命；「一部兵马」整军调走 |
| W4 | 情报选项没说「只减半一次」 | 情报「减半」与区间变化对不上 |
| W5 | 末季奏疏只有调兵选项 | 末季缺「维持现状」 |
| W6 | 增兵不提养兵负担 | 扩军不增军费 |
| W7 | 文字写死行程季数 | 选项写死行程，与寻路不符 |
| W8 | 隐藏数值与数字同现 | 人设直接提到隐藏数值 |
| W9 | 改变局势的选项没有 `hint` | — |
| W10 | 远地奏疏把回合写在 conditions 里 | 驿程晚到时条件已不成立 |
| W11 | 本方地区缺 `courier_days` | — |
| W12 | 事件与奏疏写同一个旗标、可能先于奏疏发生、却不读 `memorial:<id>` | 奏疏晚于事件 |
| W13 | 玩家可见文字写回合代号（「T19」「至 T23」），应写历法 | 结局【达成条件】里出现 T23 |
| W14 | 选项改动兵额 ≥ `settings.troop_hint_min`（默认 2000）却没写这个数 | 「九曲出击」漏写陇右军 −5000；「调禁军五千戍关」兵员凭空消失 |

规则是保守的通用版；剧本特有的检查（具名史事须有旗标依据、某派系口吻须在京……）写进你自己的 lint，参见《天宝》的 `tools/content_lint.py`（28 项）。

人工检查清单（lint 覆盖不到的）：

- [ ] 结局正文只写条件保证的事；每个结局都写了【达成条件】。
- [ ] 人设不含会过时的状态、不含隐藏数值。
- [ ] 每季有 2–3 条以上可批的奏疏，且中后期不同质。
- [ ] 奏疏的「所议之案」与谏言主张写全，廷议不会「诸案并陈」。
- [ ] 事件效果按「当时的局势」作用（按阵营 / 条件选军队），不按固定 id 盲扣。
- [ ] 平叛 / 危机解除之后有善后阶段，不会突然无事可做。
- [ ] 失败结局有预警（危机计档位、提前一季的奏疏或事件），玩家来得及应对。
- [ ] 所有文字 UTF-8（无 BOM），字体覆盖剧本用到的全部字符。
- [ ] 同一季可能同时触发的事件，结论不互相矛盾（见第 8 节「同一季的事件按文件顺序判定」）。
- [ ] 旧奏疏对玩家改变过的前提（人被夺职、人已出京）都有变体（见第 9 节）。

## 16. 用 GPT / Claude 试玩

模拟脚本只会按选项位置选择；真正暴露「文字与状态不符」「说明看不懂」「数字前后矛盾」的，是让一个模型从头到尾玩一局并写报告。见 [mcp-playtesting 技能](../skills/mcp-playtesting/SKILL.md)：

```bash
python scripts/agent_play.py codex  --scenario my_game --run gpt-01 --seed 7
python scripts/agent_play.py claude --scenario my_game --run claude-01 --seed 7
```

每份报告的问题按「引擎 / 剧本」归类、修复、再测；《天宝》用十轮这样的循环把问题从「机制错误」收敛到「叙事一致性与说明口径」。

**还要看真实界面。** 模型经 MCP 读的是文字简报，看不到网页上的样子。《天宝》第 10 轮在录制宣传片时才发现几处 MCP 试玩漏掉的问题：廷议标题把给模型看的选项编号（`deny`、`grant`）原样显示在网页上；结算卷轴的插图还没加载就截了图；同一季既报「安堵」又报「起兵」。每轮至少用 Playwright 把开局、一季完整流程、叛乱局和结局各截一遍图，人工看一遍。

**让玩家自己玩、模型当大臣。** 引擎的模型服务除了 OpenAI 兼容接口，还有 `claude_cli` / `codex_cli`：直接调用本机已登录的 Claude Code / Codex 扮演群臣，不需要 API key（见 `simcore/llm/cli_agent.py`）。试玩时用它在网页里玩几季，看大臣的话是否贴合局面——这是 MCP 试玩之外的另一种视角：MCP 是「模型当皇帝」，这里是「模型当大臣」。
