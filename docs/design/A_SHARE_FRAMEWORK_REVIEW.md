# 长期持续研究框架设计评审（结合 A 股特点）

> **状态：评审文档（2026-10 完成，尚未实施）**。本文档是对「中期决策层（midterm）→ 跨期跟踪（tracking）→ 市场状态与语境（market_regime / domain_context）→ 偏离控制与数据治理（deviation / company_track）」四个集群的**设计级评审**，以代码为准（多份设计文档状态行已滞后于实现，见 §8）。评审结论分 P0/P1/P2 与"建议降级"四档，均附代码锚点；**尚未进入实施**。
> 关联文档：`docs/design/RESEARCH_CONTINUUM_DESIGN.md`、`docs/design/DEVIATION_CONTROL_FRAMEWORK.md`、`docs/midterm/MIDTERM_DECISION_MODEL.md`、`docs/midterm/MIDTERM_INVESTMENT_ROADMAP.md`、`docs/roadmap/COMPANY_TRACK_ROADMAP.md`、`docs/roadmap/MARKET_REGIME_ROADMAP.md`、`docs/roadmap/DOMAIN_CONTEXT_ROADMAP.md`、`docs/roadmap/REVIEW_REGIME_DOMAIN_CONTEXT.md`（评审小组笔记之一，已落盘）。
> 若据此立项实施，契约改动必须走 `alphabee-pipeline-contract-steward` / `alphabee-schema-steward` skill 流程。

---

## 0. 评审范围与方法

- **评审对象**：整个长期持续研究框架（研究连续体 + 中期决策层 + 偏离控制 + 公司赛道），**主线视角是 A 股市场结构**。
- **方法**：四路并行深读（① 中期决策层；② 跟踪对账与触发层；③ 市场状态与语境层；④ 偏离控制与路线图层），设计文档与实现代码**双向核实**，文档状态行一律以代码与 `docs/roadmap/ROADMAP.md` 为准；评审小组笔记由主审逐条抽验。
- **验证标注**：本文档标记 ⭐ 的发现由主审对照代码**复现验证**；其余由评审小组核实并给出 `文件:行号` 锚点，实施前应按锚点复核。

## 1. 总体结论

框架的**工程骨架是健康的**：确定性纯函数内核、"复用而非重写"、append-only 持久化、软状态与熵驱动的保守化、行动红线（`require_human_confirm` 恒 `False`）、fail-open 与同源压缩纪律，均值得肯定；且已做对一批 A 股适配（§7）。但框架的多条**核心设计假设是按"美股式信息结构"写成的**，与 A 股在五个维度系统性错位（§2）。**问题集中在假设层与接线层，不在代码质量层**——这决定了修复以"改假设、补语义、接断线"为主，而非重写引擎。

---

## 2. 五个系统性错位（主题级结论）

### 2.1 信息顺序假设倒置：设计押注 E>T>F，A 股现实是价格抢跑

`docs/midterm/MIDTERM_DECISION_MODEL.md` §4.3 明文规定顶部判定顺序 **E → T → F**："盈利预测先停止上修，再股价 RS 下降，最后财报才真正变坏"，S3→S4 的判定"不能等 F 的财报拐点"。这条设计假设存在一个**活跃且及时无偏的一致预期流**（美股式机构信息流）。

A 股现实恰恰相反：研报覆盖稀疏且**滞后于行情**（研报常是"行情确认"而非"预期领先"）；题材/政策驱动下股价先行、预期跟随、财报最后确认是常态。对多数 A 股标的，真实的信号顺序更接近 **T → E → F**。按 E>T>F 的优先级，S3→S4 判定会在"主题退潮、股价已破位"时仍等待 E 转平——设计中最聪明的部分，在 A 股可能系统性慢半拍。

### 2.2 数据生态假设：把"期望中的锚"当成了 A 股真锚

E 引擎的数据现实（评审小组实测）：`alphabee/collectors/consensus/engine.py` 用东财**研报列表**聚合（非盈利预测 datacenter），rating 严重右偏、target_price 覆盖率仅 4/41、中小票 coverage_count 常 0–2；revision **没有最小覆盖门槛**（单份研报调价即产 strong 证据），而 C 因子已有单样本护栏（`alphabee/midterm/factors.py:431-439`）——consensus 却没有。

A 股真正的预期锚是**业绩预告**：强制披露条件明确（扭亏/首亏/续亏必预告）、区间制、可被修正（预告"变脸"本身就是高信息量事件）。但 `alphabee/midterm/evidence_rules.py` 不认 `forecast_type`（预增/预减/扭亏/首亏/续亏/续盈），区间一律按符号计证，`update_flag` 未映射、同报告期取首条可能拿到**被更正前的旧预告**做 beat/miss 基线。

### 2.3 交易制度在决策层缺席：仓位带按"可自由交易"假设设计

T+1、涨跌停（±10%/±20%/ST ±5%/北交所 ±30%）、停牌、退市整理期，在 `alphabee/midterm/position.py` 的三轴仓位乘法中**零体现**：S1"试探 5–10%"在一字涨停日买不进，S5"清仓"在跌停/停牌日卖不出（且逐帧重触发 exit 信号），低价股整数手使小权重失真；一字板 `turnover≈0` 被 `alphabee/midterm/score_engine.py:269-270` 判为"不拥挤"——恰好读反。成交额占比已算入 C 因子却**不回灌执行折减**。`ExitCondition` 契约仅五种（`alphabee/midterm/models.py:492-498`），设计文档 MIDTERM_S1_S4 §37 的 Price/Risk Stop 未落码。

### 2.4 生命周期不完备：S0–S5 对 ST/退市/次新股失语

S0–S5 覆盖"研究候选→退出"，但 A 股研究对象的真实生命周期含：**次新股**（无历史帧，首帧=基线登记，无法 diff）、**ST/\*ST**（全代码零引用——S3 持 15–20% 仓位可伴随 *ST 走向面值退市而无任何数值止损）、**停牌**（无交易日历，`elapsed_days` 按日历日算，停牌日复用旧行情被当"无变化=稳定"）、**退市整理期与退市**（数据断供→degraded 帧+stale 告警无限循环，无"研究终结"终态）。面值退市（连续 20 日收盘<1 元）是**可提前监测的确定性事件**，框架无此规则。

### 2.5 市场结构假设：沪深300 + 估值均值回归 vs 政策市 + 结构性行情

`alphabee/market_regime/score_engine.py` 三引擎（估值 30/趋势 40/流动性 30）全部指数输入只取沪深300，隐含"单一市场档位 + 估值均值回归 + 全市场同涨同跌"。A 股现实：**政策是第一驱动变量**（2015 去杠杆、2021 教培/双减、2024-09"924"）而政策在 schema 与 domain primitives 中**零建模入口**；**结构性行情是常态**（2023 AI/TMT 结构牛中 hs300 走弱→档位"风险增加"，个股论点被系统性误导）；**涨跌停情绪温度计已采集未消费**——`alphabee/collectors/market_regime/breadth.py:64-67` 已取 `limit_up_count/limit_down_count` 并注册 schema，但全仓无规则消费（死字段），连板高度/炸板率未采集。且 regime 的个股流水线注入节点（Phase 4 `load_market_regime_context`）**未实现**——市场状态层与个股决策层之间目前是一条断线。

---

## 3. 设计假设 × A 股现实 对照表

| # | 设计假设（出处） | A 股现实 | 后果 | 严重度 |
|---|---|---|---|---|
| 1 | 一致预期活跃、及时、无偏（E 核心因子，§4.3 E>T>F） | 研报稀疏滞后右偏，覆盖集中头部 | 中小盘 E 恒 None 或噪声驱动；S3→S4 判定慢半拍 | 🔴 |
| 2 | 证据独立、log-odds 可加（`midterm/bayes.py:38-39`） | 会计差错更正/预告变脸会**作废**历史证据 | 无 retraction 语义，错数字永久污染后验 | 🔴 |
| 3 | 决策即时可执行（`midterm/position.py`） | T+1、涨跌停、停牌、整数手 | 仓位带信号不可执行，一字板拥挤度读反 | 🔴 |
| 4 | 价格触发阈值 5% 通用（`tracking/triggers.py:82`） | 双创 ±20%、ST ±5%、北交所 ±30% | 双创噪声击穿；ST 涨停（≈4.95%）恰被 `abs<=threshold` 漏判 ⭐ | 🟠 |
| 5 | 财报/预告连续节奏到达（无日历概念） | 强制披露窗口（1/31、4/30、8/31、10/31）+ 预告强制条件 | 财报季证据脉冲误触监控；"该出未出"信号丢失 | 🟠 |
| 6 | 帧按交易日推进（`midterm/diff.py:143-146` 按日历日） | 停牌日无行情 | 停牌=稳定，velocity 稀释，复牌一字板被当巨量归因 | 🟠 |
| 7 | 历史数值不重述（append-only 反漂移） | 差错更正、商誉减值暴雷、一次性损益 | 下帧 F"变化"被当新信息归因 | 🔴 |
| 8 | 沪深300 代表全市场 + 估值均值回归 | 结构性行情、风格轮动、政策市 | 单维档位误导个股论点；政策无入口 | 🟠 |
| 9 | S0–S5 状态完备 | ST/退市/次新股 | 生命周期失语，无制度性止损 | 🟠 |
| 10 | 告警由人消费、7 天保鲜（`tracking/triggers.py:85`） | 公告流连续（减持/问询/解禁） | 固定天数保鲜与事件流错配；STALE_EXPIRED 每周必发淹没真事件 | 🟡 |
| 11 | 研报聚合可作预期锚（`collectors/consensus/engine.py`） | 业绩预告才是真锚 | E 权重错位；无 coverage 门槛 | 🟠 |

---

## 4. 分模块发现

### 4.1 中期决策层（引擎在，假设错位最多）

机制完整（软状态、三轴正交、五层差分、反证权重 1.2、clamp 0.05–0.95），但存在**一条已复现验证的机制缺陷**（§6 P0-1）与五条假设错位（§2.1–2.4）。`evidence_rules` 的同源压缩只做"帧内同报告期"，**不跨帧幂等**。

### 4.2 跟踪与触发层（闭环在，事件通道盲区）

五种 TriggerKind 中 FINANCIAL_REPORT / ANNOUNCEMENT 是**代理判定**（fundamental 字段刷新、news_title 变化），`tracking/triggers.py` 模块 docstring 自证"不为此新造字段（纪律优先）"。A 股高信息量事件——**问询函/监管函、减持计划、回购、股权激励、ST 警示、解禁、龙虎榜、机构调研、涨跌停封板**——全部无 kind 也无数据源（grep 零命中）。"不扩枚举"的纪律当初防范围膨胀，如今成为事件盲区的制度性原因。reconcile 七问只落地 5/7（Q4 假设登记、Q5 预测逐条判定缺失）。

### 4.3 市场状态与语境层（骨架健康，断线两条）

regime 引擎确定性内核与 YAML 数据驱动是优点；**涨停情绪数据采了不用**、政策零入口、风格第二轴缺失；且 regime **未注入个股流水线**。domain_context 仅 6 primitives + 2 playbooks（猪周期、矿业服务），白酒（预收款蓄水池）、地产（结转节奏）、券商（beta 弹性）、光伏/锂电（产能周期）、CXO、银行、半导体全部缺失——茅台落入空驱动的兜底框架（primary_drivers 为空）。

### 4.4 偏离控制与数据治理层（落地最完整，缺口在语义层）

偏离控制 F0–F5 全链条落地（分类法→账本→契约→恢复阶梯→度量层）。缺口：CLASS_BY_CATEGORY 无政策突变/监管问询/审计非标/差错更正/减持专属 category；账本 append-only 自身无碍，但 **FactorSnapshot/evidence_log 无重述语义**；C 引擎正确规避了北向（2024-08 披露规则变化），但两融/龙虎榜/减持（Tushare 可得）未接；company_track 缺**概念板块**叙事坐标系（track_label 是财务口径，与交易口径 T/C 不在同一坐标系），A 股同业高 beta 使 peer 基准退化为行业 beta 判断。

---

## 5. 已做对的 A 股适配（保持项）

评审同时确认以下适配**已正确落地，实施新改动时不得回退**：

1. **预告区间 beat/miss**：express 实际 vs forecast 预告区间，落在区间内 = neutral（无意外即无证据）——符合 A 股预告区间制；
2. **反证强制入账**（`tracking/scheduler.py` `enforce_attribution_accounting`）：每帧 diff 归因必须同时覆盖支持与反对证据，防止叙事漂移；
3. **行动红线**：`require_human_confirm` 恒 `False`，行动类输出永不自动执行——对 T+1/涨跌停下"信号与执行分离"是正确姿态；
4. **北向资金规避**：C 引擎未接北向日度净买入（2024-08 起停止实时披露）——对数据可得性变化的正确应对；
5. **A 股特有风险字段已在 schema**：`pledge_ratio`（股权质押）、`audit_opinion`（审计意见）、`goodwill`（商誉）已进 R/F 因子与采集层；
6. **同源相关证据压缩**（`evidence_rules.py` A1）：同报告期取首条、revision 按预测年度留最大——方向正确，缺的是跨帧幂等（§6 P0-1）；
7. **fail-open 纪律**：配置/数据缺失显式 None 并留痕，绝不静默回退 0。

---

## 6. 优先级清单

### P0 — 缺陷级，先修

| # | 问题 | 锚点 | 最小改动 | 实施状态 |
|---|---|---|---|---|
| P0-1 | **无新信息下置信度爬升**（⭐ 主审已复现）：revision 事件以快照日签名，同值跨帧换 id，逐帧以上帧后验为先验重复贡献 log-odds | `midterm/evidence_rules.py:388` + `tracking/scheduler.py:414` | 事件日取 consensus 自身 as_of 日期，id 不含快照日，同值幂等 | 已实施 `bb7c06b` |
| P0-2 | **重述/作废语义缺失**：log-odds 只加不减；差错更正使下帧 F 变化被当新信息归因 | `midterm/bayes.py:172`、`midterm/diff.py:183-217` | evidence_log 加 supersede 指针 + retraction 运算；映射更正标志→restatement 事件，受影响 FieldDelta 标 restated 不参与归因（不推翻 append-only，追加"作废事件"） | 已实施 `bb7c06b` |
| P0-3 | **停牌/交易日历 + ST/退市退出 kind** | `midterm/diff.py:143`、`midterm/models.py:492-498` | 接交易日历，elapsed_days 改交易日；新增 price_risk_stop / delist_warning 退出 kind，面值<1 元预警压仓位带 | 已实施 `cae758a` |
| P0-4 | **触发阈值制度感知 + 财报日历触发器**（零新数据源纯规则） | `tracking/triggers.py:82`、`midterm/diff_consumers.py:29-30` | price 阈值按板块涨跌停幅度归一；tv/evidence_rate 用真实历史回测标定；新增披露窗口（1/31、4/30、8/31、10/31）+ 预告强制条件日历触发 | 已实施 `cae758a` |
| P0-5 | **政策维度入口 + 涨停情绪温度计激活** | `collectors/market_regime/breadth.py:64-67`（死字段）、domain_primitives 无 policy_shock | 落地 policy_shock primitive + policy_regime 定性字段（先作 Issue 来源不进主权重）；limit_up/down 子分并入情绪调整项 | 已实施 `494b084` |
| P0-6 | **E 引擎护栏与锚重调** | `midterm/evidence_rules.py:360-397` | coverage<3 → e_revision=None + 显式 issue（照抄 C 的护栏）；业绩预告权重上调，研报 consensus 降为辅助 | 已实施 `494b084` |

### P1 — 重要增强

1. **仓位可执行性折减**（`midterm/position.py:197`）：涨停/停牌/成交额占比回灌 stock_weight 折减或置 restricted 新 reason，乘法结构不动；一字板时 C 置 neutral。
2. **预告修正与类型**（`midterm/evidence_rules.py:203-210` + `adapters/tushare/expectation_mapping.yaml`）：映射 update_flag、同报告期取 ann_date 末条；type 加权（扭亏/首亏 > 预增/预减 > 续盈）。
3. **中间态证据入窗**（`tracking/scheduler.py:215-221` window_texts 硬编码 None）：预告/快报文本进 evidence window 并参与 FINANCIAL_REPORT 判定。
4. **告警消费闭环 + 公告分级**（`tracking/triggers.py:395`）：ANNOUNCEMENT 由"新闻标题变化"升级为公告类型分级（问询>减持>回购>一般）。
5. **regime 风格第二轴 + 注入个股流水线**：board_divergence（hs300 vs 创业板/科创50）+ 风格标签；实现 load_market_regime_context 注入节点。
6. **C 引擎补资金真值**（`collectors/crowding/collect.py`）：接两融/龙虎榜/减持公告；北向改用季度持股变化。

### P2 — 可选增强

- 概念板块叙事层（concept_tags 用于 T/C 对齐，不进 peer 基准防污染）；
- 停牌/退市/ST 终态视图与次新股招股期基线（`tracking/status.py:63-70`）；
- domain primitives 扩充（白酒/地产/券商/光伏锂电/CXO/银行/半导体）；
- 北向季度持股、新基金发行补位（`market_regime/rules/risk_preference.yaml:47-63` 仅 ETF）。

### 建议降级的路线图条目（A 股现实下性价比低）

- **E revision 组合 Alpha 回测**：数据稀疏+右偏，短期难出结论；
- **Portfolio Allocator / Opportunity Radar**：个人账户 50–100 家覆盖性价比低；
- **explainer LLM 解释器**：midterm_reporter 已覆盖增量小；
- 三层仓位乘法本身**不再是缺口**，优先级应低于执行层约束。

---

## 7. 需要拍板的设计哲学问题

1. **E>T>F 是否按标的类型条件化**？大市值白马维持 E>T>F；题材/中小盘/政策驱动标的改 T 领先判定（或引入"信号顺序"作为可配置维度）。
2. **"不扩枚举"纪律 vs 事件通道需求**：TriggerKind 扩到公告类型级（问询/减持/解禁/ST）需要走 §14.3 修订流程——是否解除该纪律？
3. **log-odds 是否引入时间衰减**？A 股证据半衰期短（财报季脉冲+叙事漂移），纯累积+1 年一刀切滤旧（`midterm/decision_model.py:355`）是否改为按报告期覆盖或指数衰减？
4. **regime 是单档还是双档**（市场 + 风格）？结构性行情常态下，个股论点的市场约束应按其所属板块适用性修正。
5. **重述语义放哪一层**？append-only 帧不动的前提下，重述指针是放 evidence_log（作废事件）还是 FactorSnapshot（restated 标记）——影响 diff 归因口径。

---

## 8. 附注：文档状态行治理

评审过程中确认多份设计文档的**头部状态行滞后于代码**，建议下次动文档时以 `docs/roadmap/ROADMAP.md` 为权威同步刷新：

| 文档 | 自称状态 | 实际状态 |
|---|---|---|
| `docs/design/RESEARCH_CONTINUUM_DESIGN.md` | "§8 L2、W3、W5 仍未实现" | P1–P6 已全部提交（`e42aab2`…`6410cc5`），仅 §6.2 路由类未落地 |
| `docs/design/DEVIATION_CONTROL_FRAMEWORK.md` | "设计提案未实施" | F0–F5 六期全部落地并经第三方认证 |
| `docs/midterm/MIDTERM_INVESTMENT_ROADMAP.md` | "`alphabee/midterm/` 尚未落地" | E/T/C 三引擎、S0–S5、贝叶斯、三层仓位乘法均已实现并接入主图 |
