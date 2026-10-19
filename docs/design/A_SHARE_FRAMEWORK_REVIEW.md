# 长期持续研究框架设计评审（结合 A 股特点）

> **状态：评审文档（2026-10 完成；P0 六项与 TEF 变体已实施，P1/P2 未实施）**。本文档是对「中期决策层（midterm）→ 跨期跟踪（tracking）→ 市场状态与语境（market_regime / domain_context）→ 偏离控制与数据治理（deviation / company_track）」四个集群的**设计级评审**，以代码为准（多份设计文档状态行已滞后于实现，见 §8）。评审结论分 P0/P1/P2 与"建议降级"四档，均附代码锚点。**实施进展（2026-10-18）**：P0 六项已按 PR-A（`bb7c06b`）/ PR-B（`cae758a`）/ PR-C（`494b084`）全部落地（§6 表含实施状态列，ROADMAP 登记表第 81–83 行）；§2.1 的 TEF 顺序变体已按 PR-1（`3e28dcd`）/ PR-2（`9c82acc`）交付（登记表第 84–85 行），A/B 对比实验待执行；P1/P2 与"建议降级"条目未实施。
> 关联文档：`docs/design/RESEARCH_CONTINUUM_DESIGN.md`、`docs/design/DEVIATION_CONTROL_FRAMEWORK.md`、`docs/midterm/MIDTERM_DECISION_MODEL.md`、`docs/midterm/MIDTERM_INVESTMENT_ROADMAP.md`、`docs/roadmap/COMPANY_TRACK_ROADMAP.md`、`docs/roadmap/MARKET_REGIME_ROADMAP.md`、`docs/roadmap/DOMAIN_CONTEXT_ROADMAP.md`、`docs/design/REVIEW_REGIME_DOMAIN_CONTEXT.md`（评审小组笔记之一，已落盘）。
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

> 本节为展开版：每个错位按「机制剖析 → A 股实证 → 失配传导 → 修复状态 → 残留」五段展开。
> 修复状态以 2026-10-18 代码为准：P0 六项已按 PR-A（`bb7c06b`）/ PR-B（`cae758a`）/ PR-C（`494b084`）落地（ROADMAP 登记表第 81–83 行），TEF 顺序变体已按 PR-1（`3e28dcd`）/ PR-2（`9c82acc`）交付（登记表第 84–85 行）。

### 2.1 信息顺序假设倒置：设计押注 E>T>F，A 股现实是价格抢跑

**机制剖析**。`docs/midterm/MIDTERM_DECISION_MODEL.md` §4.3 明文规定顶部判定顺序 **E → T → F**："盈利预测先停止上修（E 转平），再股价 RS 下降（T 弱），最后财报才真正变坏（F 坏）"，S3→S4 的判定"不能等 F 的财报拐点"。这一顺序在 `alphabee/midterm/classifier.py` 中被落实为三处**门控**：S2 证据确认以 `e_up` 为门控（`e_up * (f_up + _not_up(t))`）、S4 充分定价以 `e_not_up` 为门控（`e_not_up * (t_down + v_down + c_down)`）、背离 case ② 把「E↑ + T↓」命名为"业绩上修 vs 股价走弱"的具名冲突。这套设计的完整前提是**美股式机构信息流**：卖方分析师覆盖活跃、预期更新及时且无偏、盈利预测修正**领先于**股价与财报——E 是整条退化链（S3→S4）的最先导信号，所以它当门控。

**A 股实证**。三个结构性事实使该前提在 A 股不成立：

1. **研报是"行情确认"而非"预期领先"**：券商研报出在行情启动**之后**是常态（题材启动→游资/机构买入→研报跟进确认）。2023 AI/TMT 主题行情中，算力链个股的盈利预测上修普遍发生在股价翻倍**之后**；研报调级本身右偏（买入/增持占比畸高）且常追涨杀跌——E 的"领先性"在多数标的上是幻觉。
2. **价格抢跑的制度土壤**：信息传导链（调研→内部→关联方→市场）叠加涨停板制度（封板=强烈共识信号），使股价在公告/研报之前就完成定价。"业绩预告/重大公告前异动"是监管层反复治理的现象，恰恰说明 T 先行的普遍性。
3. **题材/政策驱动下 E 长期缺席**：主题行情的核心驱动（政策、事件、情绪）不进分析师盈利模型，T 已剧烈反应而 E 纹丝不动——此时"等 E 转平"等于永远不判 S4。

**失配传导**。按 E>T>F 运行：① **S3→S4 判定系统性慢半拍**——主题退潮、股价已破位时，`e_not_up` 门控仍为 0（E 还停在"上修中"），S4 证据不激活，减仓信号迟到，回撤吃掉浮盈；② **S2 加仓同样滞后**——T 已先行确认时 E 门控不放行；③ **背离检测方向反了**——A 股题材行情里高频出现的是「T↑ + E↓」（价格先行、预期未跟上），而设计的具名背离是「E↑ + T↓」；E↑T↓ 在 A 股反而罕见，导致"市场知道什么我们不知道"的研究触发在题材行情中**失灵**。

**修复状态（2026-10-18）**。✅ **TEF 顺序变体已交付**（`3e28dcd`/`9c82acc`）：`alphabee/midterm/classifier_tef.py` 完全独立实现 T→E→F——S2 门控翻转为 `t_up * (f_up + _not_up(e))`（`:169`）、S4 门控翻转为 `t_down * (e_not_up + v_down + c_down)`（`:172`）、S5 加价格确认门控 `t_down * (e_down + 0.5 * f_down)`（`:174`）、背离 case ② 镜像为「T↑ + E↓」（"价格先行转强 vs 预期未跟上"）；`classifier_dispatch.py` + config `midterm.classifier_order`（缺省 `etf`，fail-open）+ tracking CLI `--classifier` 三通道切换；每帧 `CompanyStateArtifact.classifier_order` 记录 provenance。设计见 `docs/implementation/midterm/MIDTERM_CLASSIFIER_TEF_DESIGN.md`。

**残留**：① **A/B 对比实验尚未执行**——变体已交付，但"哪个顺序更早命中真实拐点、假信号更少"的结论未跑（实验指南见 TEF 设计 §9，基线已按 P0 落地后口径重定义）；② **每标的自动选序未做**（大市值白马维持 E>T>F、题材/中小盘改 T 先导的条件化选择是后续研究问题，非本变体范围）。

### 2.2 数据生态假设：把"期望中的锚"当成了 A 股真锚

**机制剖析**。E 引擎的数据底座（评审小组实测）：`alphabee/collectors/consensus/engine.py` 用东财**研报列表**聚合（非盈利预测 datacenter），rating 严重右偏、target_price 覆盖率仅 4/41、中小票 coverage_count 常 0–2；revision 事件生成**没有最小覆盖门槛**——单份研报调价即可产出 strong 证据，而 C 因子早已有单样本护栏（`alphabee/midterm/factors.py:431-439`），consensus 却没有。同时，A 股真正高质量的预期锚——**业绩预告**——在体系中地位错位：预告只作为 F 因子的 beat/miss 修正项（`_beat_bonus`/`_miss_penalty`）参与打分，不是 E 的主轴；`evidence_rules` 不认 `forecast_type`（预增/预减/扭亏/首亏/续亏/续盈），区间一律按符号计证；`update_flag`（预告修正标志）未映射、同报告期取首条可能拿到**被更正前的旧预告**做 beat/miss 基线。

**A 股实证**。两个数据生态的对比是决定性的：

1. **研报流 = 商业自发的稀疏滞后流**：券商覆盖集中在头部（大市值 20-50 家覆盖，中小市值 0-3 家），评级右偏、盈利预测质量参差、更新滞后于行情——它不是"活跃且及时无偏的一致预期流"。
2. **业绩预告流 = 制度强制的及时事件流**：亏损/扭亏/净利润变动 ±50% 的强制预告义务 + 区间制披露 + 1/31、7/15 窗口，使预告成为**监管制度保证的、带时间戳的、可修正的**预期事件；预告"变脸"（如 2018-2019 商誉减值潮中"预告预增→年报巨亏"）本身就是 A 股特有的高信息量事件——修正动作暴露了预期与事实的断裂。

**失配传导**：① 中小盘 `e_revision` 由单份研报驱动 → strong 假证据 → 叠加 P0-1 的跨帧重复计证，后验虚高；② S2/S4 门控被噪声 E 驱动（2.1 的慢半拍在数据层再放大一层）；③ 预告锚的制度化信息量（强制、及时、区间制、可修正）被浪费在 F 修正项上，E 主轴反而押在不可靠的研报流上。

**修复状态（2026-10-18）**。✅ **P0-6 已实施**（`494b084`）：`evidence_rules._revision_evidence` 与 `score_engine._revision` 双层 coverage<3 护栏（`_MIN_CONSENSUS_COVERAGE=3`，覆盖不足 ⇒ 不产 revision 证据 / `e_revision=None` 并登记 `consensus_coverage_low`）；E 方向分锚重调为 **0.6×预告锚（`_forecast_revision_anchor`，相邻报告期预告区间中点差）+ 0.4×研报修正**——业绩预告成为 E 主轴；`consensus_missing` Issue 留痕（D1_DATA，只留痕不降级）。配套（P0 PR-A，`bb7c06b`）：`forecast_update_flag` canonical 字段 + adapter 映射落地，同报告期按 `ann_date` 升序取末条。

**残留**：① `forecast_type` 加权（扭亏/首亏 > 预增/预减 > 续盈）未做（本评审 P1-2）；② 预告区间中点按符号计证仍是近似口径；③ 数据源未升级到东财盈利预测 datacenter（rating 右偏、target_price 稀疏的根因仍在，护栏只是"不信"，不是"有了更好的数据"）。

### 2.3 交易制度在决策层缺席：仓位带按"可自由交易"假设设计

**机制剖析**。`alphabee/midterm/position.py` 的三轴仓位乘法（`actual_weight = portfolio_exposure × stock_weight`，`:16-17`）只考虑单股上限/集中度（`restricted`，`:133/138`），**完全没有可执行性维度**：T+1、涨跌停、停牌、整数手在仓位决策中零体现。S4"减仓"/S5"清仓"在 diff 层只产**信号**，信号与执行之间没有制度约束建模；exit 信号在跌停日逐帧重触发（每帧都报"该清仓"却无法执行）；一字板 `turnover≈0` → 换手分位极低 → `_crowding` 判"不拥挤"（`score_engine.py:273/305-306` 的 `1.0 − 2.0×percentile` 映射）——**恰好读反**；成交额占比已算入 C 因子却**不回灌执行折减**。

**A 股实证**。制度约束是硬性的：① 一字涨停（巨量封单，买不进）与跌停板（流动性枯竭，卖不出）——极端情绪的交易日里仓位信号**不可执行**；② ST 股 ±5% 连板放大时滞；③ 停牌（并购重组/核查）期间完全无法交易，复牌常一字板跳空；④ T+1 使止损信号至少滞后一个交易日执行；⑤ 低价股 100 股/手的最小单位使小权重失真（1 元股 5% 仓位的冲击成本不可忽略）；⑥ 退市整理期（30 个交易日、10% 限制）流动性极差。

**失配传导**：① 减仓信号 → 跌停日无法执行 → 浮亏扩大，且 exit 信号逐帧重触发造成告警刷屏（噪声淹没真事件）；② S5 清仓 → 停牌期无法执行 → 复牌跳空，回撤远超仓位带设计假设；③ 假"不拥挤" → 一字板标的被误判为可加仓；④ 仓位带（S1 5–10%…S3 15–20%）在低价股/小盘股上的实际可执行上限远低于设计值。

**修复状态（2026-10-18）**。🟡 **部分修复（P0-3，`cae758a`）**：`alphabee/midterm/board_limits.py`（板块/ST 涨跌停映射）、`trading_calendar.py`（交易日历 + 停牌检测，fail-open）、`exit_conditions.py` 建立**首个退出条件生产点**（`price_risk_stop`：连续 2 日跌停或 5 日累计跌幅超 2×板块幅度，`:105`；`delist_warning`：面值<1 元观察线/窗口连续 20 日，`:126`）；`diff(trading_days/suspended_days)` 落帧。**未做**：仓位可执行性折减（本评审 P1-1）——`position.py` 仍无涨停/停牌/成交额折减；一字板拥挤度修正（本评审 P2）——误读路径仍在；ST 自动识别（顺延 RC-ST-NAME，需 `namechange` 数据源）。

**残留**："信号与执行分离"目前只是行动红线（`require_human_confirm` 恒 False）的口头原则，执行可行性尚未进入 `PositionDecision` 的数值面——减仓/清仓信号的可执行性仍由人脑判断。

### 2.4 生命周期不完备：S0–S5 对 ST/退市/次新股失语

**机制剖析**。S0–S5 六态覆盖"研究候选→退出"，隐含假设：标的一直上市、数据一直可得、帧序列单调推进。四个失语点：**次新股**（首帧=基线登记，无历史帧可 diff，5 年估值分位恒 None ⇒ V 因子失声）；**ST/\*ST**（全代码零引用——没有任何制度风险警示的建模入口）；**停牌**（原无交易日历，`elapsed_days` 按日历日，停牌日复用旧行情被当"无变化=稳定"，velocity 被稀释）；**退市整理期与退市**（数据断供→degraded 帧 + stale 告警无限循环，无"研究终结"终态，研究对象在框架里永远"等待研究"）。

**A 股实证**。退市制度是可枚举、可提前监测的确定性规则，框架却全部失语：**面值退市**（连续 20 个交易日收盘价<1 元——纯价格规则，完全可以提前预警）；**财务退市**（营收+净利润组合指标、净资产为负等）；**重大违法退市**（康美/康得新案例）；**ST 制度**（连续亏损/净资产为负/审计非标 → \*ST → 退市整理期 30 日 → 摘牌）。S3 状态持 15–20% 仓位可伴随标的走向 \*ST 与面值退市，而仓位带对此**零反应**。次新股侧还有解禁日历（首发/定增限售解禁，可提前数年预知日期的供给冲击）完全未建模。

**失配传导**：① \*ST/面值退市路径上无任何数值止损 → 单一标的尾部风险敞口失控；② 退市整理期无限告警（stale 每周必发、无终态）→ 告警体系信用透支；③ 停牌期假稳定 → 复牌一字板被当"巨量新信息"归因（diff 归因失真）；④ 次新股估值分位全 None → 三轴中的 V 轴（赔率门槛）失效，仓位决策退化为两轴。

**修复状态（2026-10-18）**。🟡 **部分修复（P0-3，`cae758a`）**：交易日历 + `trading_days/suspended_days` 落帧（停牌不再被当"稳定"，velocity/evidence_rate 分母改交易日）；`delist_warning` 退出条件（面值线规则落地，接入 exit 信号路径 → `MANUAL` 触发 → Tier 5 人工红线）。**未做**：ST 名称识别（RC-ST-NAME）；财务退市指标（营收+净利润组合、净资产为负）未建模；退市/研究终结的终态视图与次新股基线（本评审 P2）；解禁日历完全缺失。

**残留**：`delist_warning` 只覆盖面值线一条退市规则，ST/财务退市的制度风险仍在框架视野之外；退市整理期标的的"研究终结"语义缺位。

### 2.5 市场结构假设：沪深300 + 估值均值回归 vs 政策市 + 结构性行情

**机制剖析**。`alphabee/market_regime/score_engine.py` 三引擎（估值 30 / 趋势 40 / 流动性 30）的指数输入**全部只取沪深300**，估值引擎 = ERP 分位 + PE/PB 分位 → "分位越低越该买"（隐含价值投资的均值回归假设）；情绪面（成交额/融资/ETF）只作 ±5 微调、不进主权重；六阶段状态机按 Markov 渐进迁移；**政策在 schema 与 domain primitives 中零建模入口**；涨停家数/跌停家数已采集（`breadth.py`）并注册 schema 却**无任何规则消费**（死字段）。

**A 股实证**。四个结构性事实与设计假设冲突：① **政策市**——政策是第一驱动变量且常以**非渐进**方式出现：2015 清理配资（趋势反转）、2021 教培双减（行业逻辑一夜逆转）、2024-09"924"一揽子政策（指数 10 日 +25%）。Markov 渐进迁移无法表达政策 V 型反转；② **结构性行情是常态**——2023 年沪深300 走弱的同时 AI/TMT 主题翻倍，2021 年新能源结构牛与上证平盘并存。单一 hs300 档位把结构牛判成"风险增加"，个股论点被系统性压制（该加仓时减仓）；③ **风格轮动**——大小盘、价值/成长的年度级切换无任何维度；④ **涨停情绪是温度计**——涨停家数、连板高度、炸板率、涨跌停比是 A 股最灵敏的短期情绪指标，已采集未消费；流动性驱动（社融/M2/北向/两融/新基金发行）常与盈利无关地驱动牛市。

**失配传导**：① 结构牛中市场档位对个股论点的约束**方向性错误**；② 估值均值回归假设在政策拐点处失效（2015 牛市高 PE 分位持续上涨、924 行情低分位暴涨——分位信号两次反向）；③ 情绪温度计数据闲置，regime 对"情绪极端日"无感知；④ 更根本的断线：regime 的个股流水线注入节点（Phase 4 `load_market_regime_context`）**未实现**——市场状态层与个股决策层之间目前只有 midterm M 因子一条间接通道。

**修复状态（2026-10-18）**。🟡 **部分修复（P0-5，`494b084`）**：✅ `sentiment_heat` 规则激活（涨停家数分档 + 涨跌停比进入 risk_preference 调整项，四规则权重再平衡为 0.4/0.25/0.15/0.2，总分 ±5 封顶不变）；✅ `policy_shock` domain primitive 落地 + `policy_regime` canonical 字段注册 + 路由选中时产出 `policy_exposure` Issue（D4_STATE，只留痕不进主权重）。**未做**：风格第二轴 board_divergence（hs300 vs 创业板/科创50，本评审 P1-5）；`load_market_regime_context` 注入节点；北向季度持股/新基金发行补位（本评审 P2）；政策数据源（顺延 RC-POLICY-DATA，`policy_regime` 仍为占位字段）。

**残留**：单档 regime 对结构性行情的系统性误导**仍然存在**（P1-5 未做，这是本错位里最伤的一点）；政策目前只有"留痕"没有"评分影响"——政策拐点仍只能被趋势/情绪指标**滞后**反映。

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

> 实施进展标注（2026-10-18）：Q2、Q5 已有落地决议；Q1 部分落地；Q3、Q4 仍待拍板。

1. **E>T>F 是否按标的类型条件化**？大市值白马维持 E>T>F；题材/中小盘/政策驱动标的改 T 领先判定（或引入"信号顺序"作为可配置维度）。—— 🟡 部分：双实现 + 三通道切换已交付（§2.1），"每标的自动选序"仍未做。
2. **"不扩枚举"纪律 vs 事件通道需求**：TriggerKind 扩到公告类型级（问询/减持/解禁/ST）需要走 §14.3 修订流程——是否解除该纪律？—— 🟡 部分解除：`RESTATEMENT`/`REPORT_CALENDAR` 已按 P0 设计授权扩展（§6 P0-2/P0-4）；公告类型级扩展仍待拍板。
3. **log-odds 是否引入时间衰减**？A 股证据半衰期短（财报季脉冲+叙事漂移），纯累积+1 年一刀切滤旧（`midterm/decision_model.py:355`）是否改为按报告期覆盖或指数衰减？—— ⬜ 仍待拍板（P0-1 修的是跨帧重复计证，未引入衰减语义）。
4. **regime 是单档还是双档**（市场 + 风格）？结构性行情常态下，个股论点的市场约束应按其所属板块适用性修正。—— ⬜ 仍待拍板（§2.5 残留，P1-5 未做）。
5. **重述语义放哪一层**？append-only 帧不动的前提下，重述指针是放 evidence_log（作废事件）还是 FactorSnapshot（restated 标记）——影响 diff 归因口径。—— ✅ 已决议：evidence_log 作废事件（`supersedes` + `update_confidence_delta`，P0 PR-A）；FactorSnapshot restated 标记顺延 RC-FACTOR-RESTATE。

---

## 8. 附注：文档状态行治理

评审过程中确认多份设计文档的**头部状态行滞后于代码**，建议下次动文档时以 `docs/roadmap/ROADMAP.md` 为权威同步刷新：

| 文档 | 自称状态 | 实际状态 |
|---|---|---|
| `docs/design/RESEARCH_CONTINUUM_DESIGN.md` | "§8 L2、W3、W5 仍未实现" | P1–P6 已全部提交（`e42aab2`…`6410cc5`），仅 §6.2 路由类未落地 |
| `docs/design/DEVIATION_CONTROL_FRAMEWORK.md` | "设计提案未实施" | F0–F5 六期全部落地并经第三方认证 |
| `docs/midterm/MIDTERM_INVESTMENT_ROADMAP.md` | "`alphabee/midterm/` 尚未落地" | E/T/C 三引擎、S0–S5、贝叶斯、三层仓位乘法均已实现并接入主图 |
