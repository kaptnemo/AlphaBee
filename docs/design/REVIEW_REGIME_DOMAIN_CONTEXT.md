# 评审笔记：市场状态（regime）与领域语境（domain context）

## 一、机制摘要

**market_regime**（纯确定性、无 LLM）：三引擎加权打分——估值 30%（ERP 历史分位×50% + PE 分位×30% + PB 分位×20%，`rules/valuation.yaml:56-60`）、趋势 40%（均线结构 30% + 市场宽度 40% + 动量 30%，`rules/trend.yaml:58-62`）、流动性 30%（利率周期 40% + M1-M2 30% + 社融拐点 30%，`rules/liquidity.yaml:56-60`）；总分 = 30/40/30（`rules/market_score.yaml:9`），风险偏好（成交额/融资/ETF）仅作 ±5 调整项、不进主权重（`score_engine.py:379-398`）。历史特征无前视：10 年滚动分位、20 日动量、90 日利率变化（`score_engine.py:221-303`）。五档暴露带按总分 85/70/50/30 映射仓位 80-90% 至 0-20%，单周 ±10% 限幅（`position.py:116-140`）；六阶段状态机 + Markov 迁移约束（`regime_classifier.py:62-91`）+ 相似历史搜索（`forward_returns.py`）。数据源 tushare/akshare auto 合并（`data.py:104-116`），宽度仅有 akshare 当日快照。

**domain_context**：6 primitives（commodity_cycle / capacity_cycle / cost_curve / working_capital_stress / biological_inventory / project_delivery）+ 2 playbooks（hog_cycle、mining_services）+ generic_fundamental 兜底。ContextRouter 规则打分：track_label=3 > industry/sub_industry=2 > sw_code/分部/财务结构各+1 > business_model=1，top-1 决胜（`context_router.py:43-50,223-265`）；未命中回退兜底，身份信号全空才 degraded 留痕（`context_router.py:298-361`）。DriverProfile artifact 经 resolve_driver_profile 节点注入 synthesize_insights；`research.py` 为 Tier B LLM 研究 + Tier C 确定性收敛。行业生命周期启发：增速≥10% 成长期、负值衰退期（`industry/nodes.py:211-225`）。

## 二、设计假设清单

1. 市场状态可由估值/趋势/流动性三维 + 情绪微调充分刻画，30/40/30 权重是经验常数；
2. 沪深300 可代表全市场——估值/均线/动量输入全部只取 hs300（`score_engine.py:235-263`）；
3. 指标可计算：10 年历史可回填、宽度可算——但 breadth_above_ma60_pct 实为 schema field_gaps（无数据源），实际退化为单日 up_stock_ratio 截面；
4. 估值均值回归：分位越低越该买，隐含价值投资假设，与 A 股动量/结构市相悖；
5. 单一市场总分 → 统一仓位带：假定对所有个股同样适用，无 beta/风格区分；
6. 阶段迁移 Markov 渐进：政策冲击式 V 型反转无法表达；
7. 流动性用慢变量（利率/M1-M2/社融）代理资金面，隐含"宏观信用领先权益"；
8. domain_context：track_label/申万标签可靠、且公司可映射到单一主导框架（top-1）；静态 YAML 只存条件、运行时状态存值。

## 三、与 A 股特点的摩擦点（核心）

**1. 政策市：无建模入口。** market_regime schema 无任何政策字段；domain_primitives 无 policy_shock（roadmap Layer 1 已列未落地）；subsidy_policy/五年规划/国产替代/减持新规/退市新规全部缺失，`project_delivery.yaml:18` 仅在文本提"政策扰动"。2015 去杠杆、2024-09"924"行情这类政策驱动拐点只能被趋势/情绪指标滞后反映。

**2. 资金面覆盖不全。** 有 M1-M2/社融/利率（不错的宏观信用代理），但无北向资金、新基金发行、解禁/IPO 节奏；两融仅余额偏离。A 股"流动性牛"常与盈利无关，±5 情绪微调权重不足。

**3. 风格切换缺失。** regime 是单维市场档位：无大小盘、价值/成长、主题炒作维度；指数输入全押 hs300，cyb_pe_ttm 已采集（`schemas/market_regime.yaml:190`）却不进评分。2023 AI/TMT 结构牛中 hs300 弱 → 档位"风险增加"，个股论点被系统性误导；且 Phase 4 注入节点未实现，个股流水线目前根本不消费 regime 状态。

**4. 结构性行情/指数失真。** 上证 vs 沪深300 vs 创业板/科创50 分化无度量；宽度虽为全市场口径，但主指标无源、退化为单日截面（噪音大）。市场级 regime 对"个股所在板块"的约束被隐含忽略。

**5. 涨跌停情绪温度计：采集了但没用。** `collectors/market_regime/breadth.py:64-67` 已取 limit_up_count/limit_down_count，schema 已注册，但无任何规则消费（死字段）；连板高度/炸板率/晋级率未采集。A 股最灵敏的情绪指标被闲置。

**6. 北向资金零接入。** 全仓库无 northbound/hk_hold；亦未记录 2024-08 后日度净买入停止披露、仅剩季度持股的规则变化。

**7. domain primitives 覆盖缺口。** 已有：猪周期✅、矿业服务✅。缺失：白酒（预收款/批价/动销）、地产（结转节奏）、券商（beta 弹性）、光伏/锂电（产能周期/补贴退坡）、CXO（订单/投融资）、银行（净息差/不良）、半导体（国产替代/稼动率）。茅台 golden 落入 generic_fundamental，其 primary_drivers 为空（`generic_fundamental.yaml:18`）→"驱动: —"。roadmap"待拍板"的 8 原型层未实现（research.py 中无 brand_consumer 等 id）。

## 四、缺口与改进建议

**P0-1 政策维度建模入口。** 锚点：`schemas/market_regime.yaml`（无政策字段）、`domain_primitives/`（无 policy_shock.yaml）。最小改动：落地 policy_shock primitive（roadmap 已有定义）+ schema 增 policy_regime 定性字段，作为 regime 风险提示/Issue 来源，先不进主权重。

**P0-2 激活涨停情绪温度计。** 锚点：`breadth.py:64-67` 死字段、`trend.yaml:33` 依赖无源指标。最小改动：新增 sentiment 子分（涨停家数分档 + 涨跌停比）并入 risk_preference 调整项；主宽度无源时显式提示缺口而非静默退化。

**P1-3 风格/结构第二轴。** 锚点：`score_engine.py:235-244`（仅 hs300）、schema:190 cyb_pe_ttm 未用。最小改动：RegimeSnapshot 增 board_divergence（hs300 vs 创业板/科创50 20 日收益差）与风格标签，供个股流水线按板块适用性修正市场档位。

**P1-4 注入个股流水线 + 适用性修正。** 锚点：`MARKET_REGIME_ROADMAP.md:359-361`（load_market_regime_context 未实现）。最小改动：实现 skip 语义节点，将 regime + board_divergence 作为 review_thesis/report 的背景约束注入。

**P2-5 北向/基金资金面补位。** 锚点：`risk_preference.yaml:47-63`（仅 ETF）。最小改动：按 2024 后披露规则用季度持股变化替代日度净买入；新基金发行加入风险偏好采集。
