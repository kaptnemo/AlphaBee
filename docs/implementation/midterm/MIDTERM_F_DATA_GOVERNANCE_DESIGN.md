# F 数据契约与口径治理实施设计（P0–P2）

> **状态：已实施（2026-11-04，PR-1..PR-10）**。本设计收口 2026-11-02 对 midterm F 因子「数据获取 + 计算逻辑」的复核结论：P0 三项（当前生效、会改变信号）、P1 六项（口径/行业，影响 F 的解释力）、P2 四项（可追溯、PIT、校准与登记）。
>
> **关联文档**：
> - `docs/midterm/MIDTERM_DECISION_MODEL.md` §3.4（F 因子口径：水平 + 加速度 + 质量门）
> - `docs/implementation/midterm/MIDTERM_F_MARGINAL_DESIGN.md`（F 边际层数据层/评分层/影子期）
> - `docs/midterm/MIDTERM_FACTOR_DATA_DESIGN.md`（七因子数据获取设计）
> - `docs/midterm/DATA_CONTRACTS_VERIFIED.md`（Tushare 接口实测契约）
> - `docs/design/A_SHARE_FRAMEWORK_REVIEW.md`（A 股框架评审）
> - `docs/roadmap/ROADMAP.md`（行为变更登记表 / 顺延项表）
>
> **实施纪律**：canonical 字段与数据源映射走 `alphabee-schema-steward`；外部接口先冒烟后写码走 `data-source-contract`；产物结构变化走 `alphabee-pipeline-contract-steward`；每条行为变更按 ROADMAP 变更登记表登记。**本设计不新增外部数据源**，只增加既有 Tushare 接口的返回列。

---

## 0. 一页摘要

### 0.1 问题与修复总览

| ID | 问题（A 股语境） | 现状锚点 | 当前影响 | 修复动作 | 默认行为变化 |
|---|---|---|---|---|---|
| **P0-1** | beat/miss 用「最新预告期」对比「最新财报期」 | `factors.py::build_expectation_factor` / `score_engine._beat_bonus` / `_miss_penalty` | 错期窗口约 6 个月/年；600760 实帧 +0.2 虚假 beat | 给预告/财报加报告期字段；不同期 ⇒ 不产 beat/miss | **是**（F 变） |
| **P0-2** | 财报重述/快报/更正行按行序「保首条」 | `financial_fact.py::_dedup` | 可能取到初值/快报值；中芯 2024 年报实测取到 2 月值 | 补 `ann_date/report_type/update_flag`；按公告日+修订号排序去重 | **是**（数据变） |
| **P0-3** | Q1 单季 = 累计 ⇒ Δ≡0 ⇒ `M=0` | `factors.py::derive_marginal` / `score_engine._marginal` | 开 `on` 后 Q1 的 F 被系统性压低 30%；污染影子 S1/S4 | Q1 判定为「加速度不可识别」⇒ 边际降级 | 否（shadow 键变） |
| **P1-1** | 净利腿基数门槛用「1% × 累计营收」 | `factors.py::derive_marginal` | 低毛利公司（600028/601668）净利腿长期缺失 | 分母改为「单季营收」 | 否（边际层） |
| **P1-2** | OCF<0 即 −0.5，行业/季节盲 | `score_engine._operating_cashflow_penalty` | 建筑/地产/军工季末 OCF 为负被误罚；金融股不可比 | 改 TTM OCF；金融股跳过；`ocf_penalty_basis` 可回滚 | **是**（F 变） |
| **P1-3** | 只用归母净利，未用扣非 | `score_engine.fundamental_breakdown` / `models.FundamentalFactor` | 一次性收益被当作基本面改善 | 扣非数据层 + `f_recurring_mode`（off/shadow/on，默认 shadow） | 否（默认 shadow） |
| **P1-4** | Δ_rev 的分子用「营业总收入」，L 用「营业收入」 | `factors.py::derive_marginal` / `build_fundamental_factor` | Q1 的 Δ 变成口径噪声；毛利率差分口径近似 | 新增 canonical `operating_revenue`，边际统一用营业收入 | 否（边际层） |
| **P1-5** | 金融股毛利率缺失 ⇒ 边际层整体失效 | 同上 | 银行/保险/券商无边际信号；OCF 惩罚不适用 | 新增 `company_type`，`is_financial` 分支 | 是（随 P1-2） |
| **P1-6** | 分部数据半年频、无量级、混类别 | `factors.py::_segment_extremes` / `score_engine._segment_bonus` | H1 分部对比 Q3 整体；1% 小分部可加 0.2 | 期间对齐 + 占比门槛 + 只取按产品；`segment_bonus_mode` | **是**（F 变） |
| **P2-1** | 无报告期/公告日 provenance | `models.FundamentalFactor` / `midterm_reporter` | 1~4 月 F 用 Q3 但看不出；A+H 更晚 | `report_period/report_ann_date/report_age_days` + 报告行 | 否 |
| **P2-2** | 历史帧不可复现 | `data/midterm/state` 帧只存 F 值 | 重述后无法回放/校准 | 可选的 F 输入序列 append-only 快照 | 否 |
| **P2-3** | 阈值未按 A 股行业/板块校准 | `score_engine` 常量 `30/25/4` | F 在成长股二值化、在银行/公用事业钝化 | 扩展影子指标 + 离线网格校准报告 | 否（只出报告） |
| **P2-4** | 文档/ROADMAP 未登记 | — | 行为变更不可追溯 | 按 §16 清单登记 | 否 |

### 0.2 五条设计决策（本设计的选择，不再开放为「二选一」）

1. **P0 两项按 bug 修复，不加开关**：beat/miss 同期对齐、去重取最新修订，都是「数据/口径正确性」问题；用测试钉死，而不是提供「保留错误」的配置。
2. **Q1 边际「不可识别 ⇒ 降级」**：Q1 的单季同比恒等于累计同比，`Δ` 没有信息量；返回 `None`（F 回落到 L），而不是让 `M=0` 参与加权。
3. **OCF 惩罚改用 TTM**：用「上年年报 + 本期累计 − 上年同期累计」得到滚动 12 个月经营现金流，消除 A 股季末负现金流的结构性误罚；金融股显式跳过。
4. **扣非按保守锚 `min(归母, 扣非)`**：只要扣非可得且上年同期扣非为正，就用两者较小值作为净利项输入，防止一次性收益抬高 F；默认 `shadow`，先看影子差异再开 `on`。
5. **营收口径统一到「营业收入」**：新增 canonical `operating_revenue`（营业收入），`q_revenue_yoy`/`q_gross_margin` 统一用它；既有 `revenue`（营业总收入）语义不变，避免冲击 derived facts。

### 0.3 不变量（每个 PR 都必须验证）

- **I-1**：P0-3 / P1-1 / P1-3（shadow）/ P1-4 在 `off` 与 `shadow` 下**不改变 F**（只改数据/诊断键）；`on`/行为开关才改变 F。
- **I-2**：P0-1 / P0-2 是**行为修复**，允许 F/数据变化，但必须有「修复前必红、修复后必绿」的回归钉。
- **I-3**：P1-2 / P1-6 是 F 行为变更，必须有 config 回滚开关，并在 ROADMAP 登记。
- **I-4**：不新增外部接口；只增加 `income`/`fina_indicator`/`cashflow` 的同接口列。
- **I-5**：所有新增模型字段 additive、有默认值；旧帧 `model_validate` 不失败；golden 剔除集合显式扩展。

---

## 1. 背景与现状核实

### 1.1 复核结论

F 的取数链路（Tushare 四表 → adapter → canonical → `FactorSnapshot`）与「累计值差分还原单季」的骨架，是按 A 股披露制度设计的；但**口径对齐、披露时点、行业差异**三层仍有硬伤。当前生产 F（`f_marginal_mode=shadow`）实际只使用**累计同比 L + OCF 惩罚 + beat/miss + 分部 bonus**；真正解决累计滞后的边际层尚未开权重，且 Q1 结构性归零。

### 1.2 代码锚点

| 事实 | 锚点 |
|---|---|
| 财务四表采集、窗口、缓存 | `alphabee/agents/facts/tools/financial_fact.py:23-95` |
| `_dedup` 按 `period` 保首条（pandas 默认） | `financial_fact.py:19-20`；调用 `:79-84` |
| 采集 `fields` 不含 `ann_date/report_type/update_flag/comp_type/revenue` | `financial_fact.py:50-77` |
| adapter 只做列名重命名 | `alphabee/adapters/tushare.py:27-35` |
| F 的 canonical 映射 | `alphabee/adapters/tushare/financial_mapping.yaml:8-71` |
| `build_fundamental_factor` 取 `_first(fina/balance/cashflow)` | `alphabee/midterm/factors.py:590-639` |
| `derive_marginal` 的累计→单季还原与边界 | `factors.py:507-587` |
| `_Q_SMALL_BASE_RATIO = 0.01`（分母是累计营收） | `factors.py:363` |
| `build_expectation_factor` 取「最新报告期」预告 | `factors.py:642-682` |
| `ScoreEngine._beat_bonus/_miss_penalty` 无期间校验 | `alphabee/midterm/score_engine.py:168-190` |
| `_operating_cashflow_penalty` 只看累计 OCF 符号 | `score_engine.py:156-165` |
| `_segment_bonus` 只看「最快分部 − 整体营收」 | `score_engine.py:193-205` |
| `fundamental_breakdown` 是 F 唯一计算入口 | `score_engine.py:280-396` |
| `FundamentalFactor` / `ExpectationFactor` 模型 | `alphabee/midterm/models.py:141-181` / `:184-215` |
| 分类器 F 阈值 ±0.3 / 状态证据 | `alphabee/midterm/classifier.py:46-48` / `:164-197` |
| 影子观测 S4 = `f_marginal_source > 0` | `alphabee/midterm/marginal_shadow.py:366-415` |
| 分部数据结构含 `category` / `revenue_share` | `alphabee/company_track/contracts.py:14-45` |
| 分部取数优先东财、Tushare 兜底 | `alphabee/company_track/data.py:96-127` |

### 1.3 2026-11-02 实测证据（设计依据）

| 场景 | 实测 | 对 F 的含义 |
|---|---|---|
| 600760 2026-11-01 帧 | 财报 2026Q3 `netprofit_yoy=−26.00`；最新预告是 2026-07-15 的 **H1** 预告 `p_change=−58.23` | `−26 > −58.23` ⇒ **beat +0.2**；帧内 F=−0.7334，去掉后 F=−0.9334 |
| 中芯国际 688981 2024 年报 | `income` 同 `end_date` 三行：`20250212 update_flag=0` 57,745,538,740.4；`20250212 update_flag=1` / `20250328 update_flag=1` 57,795,570,000 | 现行 `_dedup` 保留 **57,745,538,740.4**（旧值） |
| 000002 2026Q3 `fina_indicator` | `update_flag=1` 行有 `profit_dedt=−2.153752e10`、`dt_netprofit_yoy=18.68`；`update_flag=0` 行为 NaN | 若行序反转，扣非字段会整列丢失 |
| Q1 单季 = 累计 | 600760 Q1：`or_yoy=−55.2311` = 派生 `q_revenue_yoy=−55.2311`；300750/002415 同样完全相等 | Q1 的 `Δ_rev/Δ_np ≡ 0` |
| 600028 / 601668 净利腿 | 1% × 9M 累计营收 220.63 亿 / 133.57 亿；上年同期单季净利 85.01 亿 / 77.78 亿 | 净利腿被 1% 门槛拒绝 |
| 601668 OCF | 2026Q1 −769.87、Q2 −284.61、Q3 −453.93、2025 年报 +205.37 亿 | 每年前三季固定吃 −0.5 |
| 601668 / 000002 / 601012 TTM OCF | +446.23 亿 / +26.79 亿 / **−9.27 亿** | TTM 口径可区分「季节性负」与「真实负」 |
| 000002 归母 | 2026Q3 归母 −239.44 亿，`netprofit_yoy=+14.53%`（亏损收窄），扣非 −215.38 亿 | L 把亏损收窄当正贡献 |
| 002517 恺英网络 2026Q1 | 归母 yoy `50.65`，扣非 yoy `5.75` | 一次性收益会把净利项推到饱和 |
| 600519 / 000333 Q1 | `or_yoy=6.538` vs 用营业总收入派生 `6.336`；`2.5501` vs `2.4546` | 营收同比口径错配制造假 Δ |
| 金融股 | 601318/600036/600030 `grossprofit_margin=None`；`comp_type=3/2/4` | 边际层整体失效；OCF 不可比 |
| 分部时点 | 002130/600760 `latest_period=20260630`，财报最新期 `20260930` | H1 分部对比 Q3 整体 |
| 688981 A+H | 2025Q3 `ann_date=2025-11-14`；2026-11-02 最新仍为 2026H1 | F 滞后 4 个月且无标记 |
| 25 只样本 | 22 只可算边际（88%），3 只金融全部不可算 | 覆盖率与行业结构相关 |

### 1.4 测试基线

`tests/midterm` 当前全绿（本次复核跑 `test_score_engine.py` + `test_f_marginal_scoring.py` + `test_marginal_derivation.py`：**130 passed**）。上述问题都是**行为/口径问题**，不是测试失败；P0-1 的 600760 行为甚至已被 `tests/midterm/test_score_engine.py:163-190` 固化为期望值。

---

## 2. 目标与非目标

### 2.1 目标

| 编号 | 目标 | 可测口径 |
|---|---|---|
| G1 | F 的 beat/miss 只在**同一报告期**发生 | 600760 帧 `beat=0`；同期帧 `beat=0.2`；空期 ⇒ 0 |
| G2 | 财报事实取**最新修订**，可追溯到 `ann_date` | 688981 2024 年报取 57,795,570,000；`_series` docstring 与实现一致 |
| G3 | Q1 边际降级，不参与加权 | Q1 帧 `_marginal=None`；`on` 的 F 与 `off` 逐值相同 |
| G4 | 低毛利公司的净利腿恢复 | 600028/601668 的 `q_net_profit_yoy` 非空 |
| G5 | OCF 惩罚对季节性/金融股中性 | 601668 无惩罚；601012 仍惩罚；600036 无惩罚 |
| G6 | 扣非可进入 F，且先影子 | `f_recurring_mode=shadow` 下 F 不变；`on` 下 002517 的净利项降到扣非口径 |
| G7 | 边际口径统一为营业收入 | 600519 Q1 `q_revenue_yoy == or_yoy`（6.538） |
| G8 | 分部 bonus 同期、有效、按产品 | 002130 Q3 帧无 bonus；H1 帧可加；2% 分部不加 |
| G9 | F 的报告期/公告日可读、陈旧可识别 | 报告出现「F 数据期：2026Q3（公告 2026-10-31，距今 2 天）」 |
| G10 | 影子/校准可回放输入 | 快照 append-only、幂等、可按 `(symbol, as_of, report_period)` 读回 |

### 2.2 非目标（防范围膨胀）

| 编号 | 非目标 | 理由 |
|---|---|---|
| N1 | 不改 classifier 状态公式、EV、position、caps/温度 | F 只是输入之一；改状态机属另一期 |
| N2 | 不改 E 引擎（consensus / 预告锚 / 证据规则） | 与 F 数据口径正交；E 生态已有单独设计 |
| N3 | 不做全市场行业中性化 / 银行专用指标（NIM/不良率） | 需要新数据源与更大校准；本期只做「金融股跳过 + 显式留痕」 |
| N4 | 不做亏损公司的归一化利润（扣非负基数仍回落归母） | 需要独立设计；本期只加 `dedt_base_negative` 标志 |
| N5 | 不做全历史 PIT 回填 | 只从本期开始 append；历史不可重造 |
| N6 | 不把 L 改成 TTM、不引入 QoQ | 设计已论证 QoQ 季节性噪声；TTM 属水平项重构，另立期 |
| N7 | 不改 `revenue`（营业总收入）的既有 canonical 语义 | 避免冲击 derived facts；新增 `operating_revenue` 单独承载营业收入 |

---

## 3. 契约与字段治理总览

### 3.1 canonical 字段新增（3 个）

| 字段 | category | type | unit | 描述 | 来源映射 | 消费者 |
|---|---|---|---|---|---|---|
| `operating_revenue` | financial | float | CNY | **营业收入**（与 `revenue`=营业总收入区分） | tushare `income.revenue` | `derive_marginal` 的 `q_revenue_yoy` / `q_gross_margin` |
| `net_profit_dedt_yoy` | financial | float | PERCENT | 扣非归母净利累计同比 | tushare `fina_indicator.dt_netprofit_yoy` | `f_recurring_mode` 的扣非锚 |
| `company_type` | financial | str | TEXT | 公司类型（1 一般工业 / 2 银行 / 3 保险 / 4 证券） | tushare `income.comp_type` | `FundamentalFactor.is_financial` |

**schema 修改**：
- `alphabee/schemas/financial.yaml`：追加上述 3 个字段（含 `category/type/unit/frequency/description/source_mappings/notes`）；`notes` 必须写明「`operating_revenue` ≠ `revenue`；`net_profit_dedt_yoy` 的负基数语义；`company_type` 为 Tushare 代码」。
- `alphabee/schemas/INDEX.yaml`：`financial.field_count` 42 → 45；在字段缺口表追加两条说明：① `q_*` 为派生字段，**不**进 canonical；② `report_period/report_ann_date` 为模型 provenance，**不**进 canonical。
- `alphabee/adapters/tushare/financial_mapping.yaml`：
  - `income` 段新增 `revenue: operating_revenue`、`comp_type: company_type`；
  - `fina_indicator` 段新增 `dt_netprofit_yoy: net_profit_dedt_yoy`；
  - 既有 `total_revenue: revenue`、`or_yoy: revenue_yoy` 不变。

### 3.2 模型字段新增（additive，全部有默认值）

| 模型 | 字段 | 类型/默认 | 用途 | canonical? | golden |
|---|---|---|---|---|---|
| `FundamentalFactor` | `report_period` | `str = ""` | L 使用的报告期（`YYYYMMDD`） | 否（provenance） | 剔除 |
| | `report_ann_date` | `str = ""` | 最新公告日 | 否 | 剔除 |
| | `report_age_days` | `int \| None = None` | 距今自然日 | 否 | 剔除 |
| | `marginal_period` | `str = ""` | 单季还原目标期 | 否 | 剔除 |
| | `is_financial` | `bool = False` | 银行/保险/证券 | 否（派生） | 剔除 |
| | `operating_cashflow_ttm` | `float \| None = None` | TTM 经营现金流（CNY） | 否（派生） | 剔除 |
| | `net_profit_dedt` | `float \| None = None` | 扣非归母净利（CNY，累计） | 是（既有） | 剔除 |
| | `net_profit_dedt_yoy` | `float \| None = None` | 扣非累计同比 | 是（新增） | 剔除 |
| | `net_profit_dedt_base_positive` | `bool \| None = None` | 上年同期扣非是否 > 0 | 否 | 剔除 |
| | `segment_period` | `str = ""` | 分部数据报告期 | 否 | 剔除 |
| | `segment_fastest_share` | `float \| None = None` | 最快分部的收入占比（%） | 否 | 剔除 |
| | `segment_basis` | `str = ""` | `product` / `all` | 否 | 剔除 |
| | `marginal_revenue_basis` | `str = ""` | `operating_revenue` / `total_revenue` | 否 | 剔除 |
| `ExpectationFactor` | `profit_forecast_period` | `str = ""` | 最新预告的报告期 | 否（provenance） | 剔除 |
| `CompanyStateArtifact` | `ocf_penalty_basis` | `str = ""` | 解析后的 OCF 口径（`ttm`/`sign`） | 否 | 剔除 |
| | `f_recurring_mode` | `str = ""` | 解析后的扣非模式 | 否 | 剔除 |
| | `segment_bonus_mode` | `str = ""` | 解析后的分部模式 | 否 | 剔除 |

### 3.3 config 新增（`MidtermClassifierSettings`）

```yaml
midterm:
  # P1-2：OCF 惩罚口径；ttm = 滚动 12 个月（缺省，A 股季节性中性）；sign = 旧口径（回滚）
  ocf_penalty_basis: ttm            # ttm | sign
  # P1-3：扣非保守锚三档；shadow = 算但不进 F（缺省）；on = 进 F
  f_recurring_mode: shadow          # off | shadow | on
  # P1-6：分部 bonus 口径；period_aligned = 期间对齐 + 占比门槛（缺省）；legacy = 旧口径
  segment_bonus_mode: period_aligned  # period_aligned | legacy
  # P2-2：是否落盘 F 输入序列快照（默认关，避免普通 run 写盘）
  persist_financial_inputs: false
```

- 缺段/缺字段 ⇒ 模型默认值（`ttm` / `shadow` / `period_aligned` / `false`）。
- **「缺字段」必须动态读 `MidtermClassifierSettings.model_fields[...]` 的 FieldInfo 默认值**（与 `_settings_default_marginal_mode` 同构），不能 `getattr(settings.midterm, key, None)` 后直接回落 legacy：测试 stub（`SimpleNamespace(midterm=SimpleNamespace(classifier_order=..., formula_revision=...))`）与旧 config 都属于「缺字段」，必须拿到模型默认，否则回退值与 settings 默认漂移。
- 读配置异常 / 取值非法 ⇒ 分别回退 `sign` / `off` / `legacy` / `false`（最保守，并 warning）。
- 显式参数（`decision_model.evaluate(..., ocf_penalty_basis=...)` 等）优先于配置。
- `f_marginal_mode` 保持不变。

**纯函数默认必须与生产默认分离**（与 `f_marginal_mode` 的「函数缺省 off、配置缺省 shadow」同构；避免直接调用 `compress_scores(snapshot)` 的测试/旧调用点被静默改行为）：

| 键 | `score_engine` 函数默认 | 生产默认（config/`decision_model`） | 回滚 |
|---|---|---|---|
| `ocf_penalty_basis` | `sign`（旧行为） | `ttm` | `midterm.ocf_penalty_basis: sign` |
| `f_recurring_mode` | `off`（旧行为） | `shadow` | `midterm.f_recurring_mode: off` |
| `segment_bonus_mode` | `legacy`（旧行为） | `period_aligned` | `midterm.segment_bonus_mode: legacy` |
| `marginal_mode` | `off`（现状） | `shadow`（现状） | `midterm.f_marginal_mode: off` |

即：**只有 `decision_model.evaluate/get_decision` 走生产默认**；任何直接调用 `fundamental_breakdown(...)`/`compress_scores(...)` 的代码保持旧行为，除非显式传参。

### 3.4 `score_breakdown` 新增诊断键（全部 `dict[str, float]`，缺失不写）

| 键 | 含义 |
|---|---|
| `forecast_period_matched` | 财报期与预告期是否一致（1/0） |
| `ocf_ttm` | TTM 经营现金流（CNY） |
| `ocf_penalty_basis` | 1 = TTM；0 = sign/回退 |
| `financial_sector` | 1 = 金融股（跳过 OCF/边际） |
| `np_term_recurring` | 扣非口径净利项（saturate 后） |
| `recurring_gap` | 归母 yoy − 扣非 yoy（pp） |
| `recurring_unavailable` | 扣非不可用/负基数（1/0） |
| `f_recurring_shadow` | 把扣非项代入后的 F（shadow） |
| `segment_period_mismatch` | 分部期 ≠ 财报期（1/0） |
| `segment_share_below_min` | 最快分部占比 < 10%（1/0） |
| `q1_marginal_degraded` | Q1 边际降级（1/0） |
| `report_period_mismatch` | `fina` 与 `income` 最新期不一致（1/0） |

### 3.5 golden 剔除集合

`tests/midterm/test_v1_artifact_golden.py` 的 `_canonical` 剔除集合追加：
- `FundamentalFactor`：`report_period`、`report_ann_date`、`report_age_days`、`marginal_period`、`is_financial`、`operating_cashflow_ttm`、`net_profit_dedt`、`net_profit_dedt_yoy`、`net_profit_dedt_base_positive`、`segment_period`、`segment_fastest_share`、`segment_basis`、`marginal_revenue_basis`；
- `ExpectationFactor`：`profit_forecast_period`；
- artifact 顶层：`ocf_penalty_basis`、`f_recurring_mode`、`segment_bonus_mode`。

断言 B 逐项钉默认值（空串/`None`/`False`）；golden 文件**不重写**，除非 P0-1/P1-2/P1-6 的 F 变化导致断言 A 失败（见 §19）。

---

## 4. P0-1：beat/miss 报告期对齐

### 4.1 现状

`build_expectation_factor` 从 `_latest_forecast_by_period(forecast_records)` 取 `periods[0]`（最新预告期）的 `min/max_change`（`factors.py:656-665`）；`fundamental_breakdown` 用 `f.net_profit_yoy`（最新财报期）直接比较（`score_engine.py:347-352`）。两者都没有期间字段，无法校验。

### 4.2 设计

**字段**
- `ExpectationFactor.profit_forecast_period: str = ""`；
- `FundamentalFactor.report_period: str = ""`（同时服务 P2-1）。

**builder**
- `build_fundamental_factor`：`report_period = _normalize_period(_first(fina).get("period")) or ""`。
- `build_expectation_factor`：`profit_forecast_period = _normalize_period(forecast.get("period")) or ""`。

**评分守卫**（`score_engine.py`）

```python
def _normalize_ymd(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""

def _periods_match(a: str, b: str) -> bool:
    aa, bb = _normalize_ymd(a), _normalize_ymd(b)
    return bool(aa and bb and aa == bb)

def _beat_bonus(f: FundamentalFactor, e: ExpectationFactor) -> float:
    if not _periods_match(f.report_period, e.profit_forecast_period):
        return 0.0
    if f.net_profit_yoy is None or e.profit_forecast_max_change is None:
        return 0.0
    return _BEAT_BONUS if f.net_profit_yoy > e.profit_forecast_max_change else 0.0

def _miss_penalty(f: FundamentalFactor, e: ExpectationFactor) -> float:
    if not _periods_match(f.report_period, e.profit_forecast_period):
        return 0.0
    ...
```

**语义**
- 任一期间为空 ⇒ **不产 beat/miss**（fail-closed：无法证明同期，就不使用）。
- `profit_forecast_period` 与 `report_period` 均为空（手工构造快照）⇒ 不产；单测需显式设置期间才断言 beat。
- `forecast_period_matched` 写入 `score_breakdown`（1/0），报告可见「beat/miss 是否同期」。
- **旧帧兼容**：已落盘 artifact 的 `score_breakdown` 不重算；旧快照若缺期间字段，重新 `evaluate` 时 beat/miss 归零（fail-closed）。这是预期行为——宁可不产，也不保留错期证据；回放旧帧应使用帧内已存的 F，而不是重新评估。

### 4.3 影响与测试

- **600760 实帧**：`report_period=20260930`、`profit_forecast_period=20260630` ⇒ `beat_bonus=0`，`f=−0.9334`（当前 −0.7334）。状态 S5 不变（`severe(F)` 在 |F|≥0.6 饱和）。
- 其他帧：1~4 月年报预告 vs Q3 实际、7 月 H1 预告 vs Q1 实际、10 月 Q3 预告 vs H1 实际，均不再误判。
- **测试更新**：
  - `tests/midterm/test_score_engine.py:163-190`：600760 用例改为 `beat_bonus=0`、`raw=-0.933333`、`f=-0.933333`；新增同期用例（`report_period="20260930"`、`profit_forecast_period="20260930"` ⇒ beat 0.2）。
  - `test_fundamental_beat_bonus` / `test_fundamental_inline_no_beat` / `test_fundamental_breakdown_output_saturation_flag`：给 `f.report_period` 与 `e.profit_forecast_period` 设同值，保留 beat 断言。
  - `tests/midterm/test_decision_model_smoke.py::test_langchao_000977_fixes_end_to_end`：当前快照无期间字段，期望 0.589 依赖 beat +0.2；补同期字段（如两处都设 `20241231`）保留该用例的 beat 语义；若刻意留空，则期望改 0.389。
  - 新增：空期间 ⇒ 不产 beat；预告期缺失 ⇒ 不产。
- **golden**：golden 快照没有 `profit_forecast_min/max_change`，预期不变；跑 `test_v1_artifact_golden.py` 确认。

### 4.4 回滚

无 config；回滚 = 撤销期间守卫（不推荐）。行为变更按 ROADMAP 登记（PR-1 行）。

---

## 5. P0-2：财报去重取最新修订

### 5.1 现状

`_dedup(df, "period")` = `drop_duplicates(subset=["period"])`，pandas 默认 `keep="first"`（`financial_fact.py:19-20`），不取 `ann_date/report_type/update_flag`。实测 `income`/`fina_indicator` 同 `end_date` 多行（`update_flag=0/1`；`fina_indicator` 的 0 行常为 NaN 扣非字段）。688981 2024 年报保留 57,745,538,740.4（2025-02-12）而非 57,795,570,000（2025-03-28）。

### 5.2 设计

**fields 扩展**（四个接口统一）

```python
# income
"ts_code,ann_date,end_date,report_type,update_flag,comp_type,"
"total_revenue,revenue,operate_profit,n_income,n_income_attr_p,ebitda,"
"basic_eps,interest_expense,income_tax,total_profit"

# fina_indicator
"ts_code,ann_date,end_date,update_flag,roe,roa,grossprofit_margin,netprofit_margin,"
"current_ratio,quick_ratio,debt_to_assets,or_yoy,netprofit_yoy,basic_eps_yoy,"
"interestdebt,profit_dedt,dt_netprofit_yoy,daa,fixed_assets,"
"saleexp_to_gr,adminexp_of_gr,finaexp_of_gr,rd_exp"

# cashflow / balancesheet：追加 ann_date,report_type,update_flag
```

**去重算法**（替换 `_dedup`）

```python
def _dedup(df: Any, col: str = "period") -> Any:
    if df is None or df.empty:
        return df
    work = df.copy()

    # 1) 合并报表优先；无 type=1 时保留全部（fail-open，不因过滤丢数据）
    if "report_type" in work.columns:
        type_txt = work["report_type"].astype(str)
        if (type_txt == "1").any():
            work = work[type_txt == "1"]

    # 2) 同报告期按 ann_date、update_flag 降序（字符串 YYYYMMDD 可直接比较）
    #    NaN/非 8 位 ann_date 回落 period，避免 "nan" 字符串在降序里排到数字之前
    ann_src = work["ann_date"].astype(str).str.replace("-", "") if "ann_date" in work else work[col].astype(str)
    work["_ann"] = ann_src.where(ann_src.str.len() == 8, work[col].astype(str))
    work["_upd"] = pd.to_numeric(work["update_flag"], errors="coerce").fillna(0) if "update_flag" in work else 0
    work = work.sort_values([col, "_ann", "_upd"], ascending=False, kind="stable")

    # 3) 同报告期取首条（= 最新公告 + 最新修订）
    work = work.drop_duplicates(subset=[col], keep="first").reset_index(drop=True)

    # 4) 清理 Tushare 内部列；保留 ann_date（provenance）与 comp_type（P1-5）
    drop_cols = [c for c in ("_ann", "_upd", "report_type", "update_flag") if c in work.columns]
    return work.drop(columns=drop_cols)
```

**关键边界**
- `ann_date` 缺失 ⇒ 回落 `period` 排序（与 `_ann_key` 既有语义一致）。
- `report_type` 无 1（例如只有母公司报表）⇒ 保留全部，再按 `ann_date/update_flag` 去重。
- `fina_indicator` 的 NaN 行：`update_flag=1` 行有值、`0` 行 NaN；排序后 `1` 在前，取到有值行。若两行同为 `1` 且其中一行 NaN，追加一条「同 `period` 优先非空 `profit_dedt/grossprofit_margin`」的稳定排序键（实现时用 `notna` 计数降序）。
- `ann_date` 保留在 `income`/`fina/cashflow/balancesheet` 行内；`report_type/update_flag` 去重后丢弃，避免 Tushare 字段泄漏到下游。

**文档修正**
- `_series` docstring（`factors.py:416-418`）当前写「以最新 ann_date 为准」，但实现没有取 `ann_date`；P0-2 后改成「由 `get_financial_fact` 在工具层按 `ann_date/update_flag` 去重，本函数只按 `period` 排序」。

### 5.3 影响与测试

- 688981 2024 年报取最终值 57,795,570,000；000002 `fina` 取到 `profit_dedt` 非空行。
- 重述/快报/更正公司的 F 变化；`income`/`fina` 的 `ann_date` 可用于 P2-1。
- **测试**（`tests/agents/facts/tools/test_financial_fact_dedup.py`，新增）：
  - 688981 三行 → 最终值；
  - 000002 `fina` 两行（1 有值 / 0 NaN）→ 非空扣非；
  - `report_type=1/4` → 取 1；
  - `ann_date` 缺失 → 回落 period；
  - `ref_dates` 仍为降序。
- 既有 `tests/agents/facts/tools/test_integration.py` 的 dedup 用例保持；可选加 `-m integration` 的 688981 实测（无 token 跳过）。

### 5.4 回滚

无 config；回滚 = 恢复 `drop_duplicates(keep="first")`（不推荐）。ROADMAP 登记 PR-2。

---

## 6. P0-3：Q1 边际降级

### 6.1 现状

Q1 的 `q_revenue_yoy == revenue_yoy`、`q_net_profit_yoy == net_profit_yoy`（实测 600760/300750/002415 完全相等），`Δ ≡ 0`；`_marginal` 返回 `(0, Q, 0)` 而不是 `None`（`score_engine.py:267-277`）。`on` 档 `F_growth = 0.7L + 0.3×0 = 0.7L`，**系统性压低 30%**；`shadow` 的 `f_shadow_delta = −0.3L` 污染 S1。

### 6.2 设计

**字段**
- `FundamentalFactor.marginal_period: str = ""`（单季还原目标期；由 `income` 最新期决定）。

**builder**
- `_latest_income_period(income)`：复用 `_normalize_period`，取 `max`；写入 `marginal_period`。
- 若 `marginal_period` 与 `report_period` 不同，`score_breakdown` 写 `report_period_mismatch=1`（P2-1 诊断）。

**评分层**

```python
def _is_first_quarter(period: str) -> bool:
    p = _normalize_ymd(period)
    return len(p) == 8 and p.endswith("0331")

def _marginal_legs(f: FundamentalFactor) -> list[float]:
    if _is_first_quarter(f.marginal_period):
        return []          # Q1：单季=累计，加速度不可识别
    ...
```

- `_marginal`：`deltas` 为空 ⇒ `None`；`fundamental_breakdown` 不写边际键，`f_marginal_source` 不出现。
- `on` 档：`marginal=None` ⇒ `f_growth=L`，F 与 `off` **逐值相同**。
- `shadow` 档：Q1 不再写 `marginal_raw/marginal_quality/marginal/f_shadow/f_shadow_delta/f_marginal_source`；`score_breakdown` 可写 `q1_marginal_degraded=1` 作为显式留痕。

**影子观测**
- `marginal_shadow.frame_stats` 增加 `report_period`、`is_q1`；
- `shadow_report` 增加 `n_q1_frames`、`coverage_ex_q1`、`median_abs_delta_ex_q1`；G-8 的 S1/S4 改用 **ex-Q1** 口径（保留旧字段作对照）。
- 理由：Q1 的 `Δ` 结构性为 0，不应计入「加速度覆盖率」分母，也不应把 `ΔF=−0.3L` 计入 S1。

### 6.3 影响与测试

- F 不变（`off`/`shadow`）；`on` 的 Q1 帧由 0.7L 回到 L。
- 影子 Q1 帧不再有边际键；覆盖统计排除 Q1。
- **测试**：
  - 新增 `test_q1_marginal_degrades_to_none`：`q_rev==rev`、`q_np==np`、`q_gm_pp` 有值 ⇒ `_marginal is None`；`fundamental_breakdown(on) == fundamental_breakdown(off)`；shadow 无边际键。
  - 更新 `tests/midterm/test_f_marginal_scoring.py:333`：Q1 合成数据改为断言「无边际键」；年报合成数据保留 `f_marginal_source=7`。
  - `test_marginal_derivation.py::test_d2_q1...` 保持（`derive_marginal` 仍产出 q_*，只是评分层不消费）。
- **golden**：golden 无多期数据 ⇒ 本来降级；不受影响。

### 6.4 回滚

无 config；回滚 = 删除 `_is_first_quarter` 分支。若未来要支持 Q1 的「加速度」语义，应改用「Q1 同比 vs 上年 Q4 单季同比」等口径，属另立设计。

---

## 7. P1-1：净利腿基数门槛改用单季营收

### 7.1 现状

```python
# factors.py:565 / :577
abs(quarter_revenue_base) >= _Q_SMALL_BASE_RATIO * current_revenue   # current_revenue = 累计营收
quarter_net_profit_base >= _Q_SMALL_BASE_RATIO * current_revenue
```

600028：1% × 9M 营收 220.63 亿 > 上年同期单季净利 85.01 亿 ⇒ 净利腿 None；601668：133.57 亿 > 77.78 亿 ⇒ None。低毛利行业长期失效。

### 7.2 设计

- 在计算净利腿前先取 `quarter_revenue = _single_quarter(revenue, target)`；
- 营收腿与净利腿的门槛均改为 `_Q_SMALL_BASE_RATIO * quarter_revenue`；
- `quarter_revenue is None or quarter_revenue <= 0` ⇒ 两腿均 None（分母无意义，保守）。

```python
quarter_revenue = _single_quarter(revenue, target)
if quarter_revenue is None or quarter_revenue <= 0.0:
    return result  # 只有毛利率腿可能已写入；营收/净利腿保持 None

...
and abs(quarter_revenue_base) >= _Q_SMALL_BASE_RATIO * quarter_revenue
...
and quarter_net_profit_base >= _Q_SMALL_BASE_RATIO * quarter_revenue
```

- 常量 docstring 改为「单季值(t−4) 的基数不稳门槛：|单季(t−4)| < 1% × 当期单季营收」。

### 7.3 影响与测试

- 600028/601668 的 `q_net_profit_yoy` 恢复；低毛利行业覆盖率提升。
- 真实低基数（单季净利 2 亿 / 单季营收 1000 亿）仍被拒。
- **测试**：
  - 更新 `test_d7_small_prior_year_net_profit_base_kills_net_leg`：当前夹具（单季营收 270，1% = 2.7；基数 3 ⇒ 会通过）需把基数改为 2.0 才继续断言 None；新增「基数 3、单季营收 270 ⇒ 通过」对照。
  - 新增 600028-like 夹具：累计营收很大、单季营收小、单季净利 85 亿 ⇒ 通过。
  - 新增 601668-like：同上。
- **行为**：仅边际层（shadow/on）；`off` 的 F 不变。

### 7.4 回滚

无 config；回滚 = 恢复累计营收分母（不推荐）。

---

## 8. P1-2：OCF 惩罚改 TTM + 金融股跳过

### 8.1 现状

`_operating_cashflow_penalty`：`OCF < 0` ⇒ −0.5（`score_engine.py:156-165`），用的是**累计** OCF。601668 每年前三季负、年报正；金融股 OCF 与利润质量不可比。

### 8.2 设计

**字段**
- `FundamentalFactor.operating_cashflow_ttm: float | None`；
- `FundamentalFactor.is_financial: bool`（P1-5）。

**TTM 计算**（builder）

```text
TTM_OCF(t) = FY(prev) + cum(t) − cum(t−4)   # t = Q1/Q2/Q3
TTM_OCF(t) = cum(t)                          # t = 1231（年报）
```

- 由 `cashflow` 记录构造 `_series(cashflow, "operating_cashflow")`；
- 缺 `FY(prev)` 或 `cum(t−4)` ⇒ `None`；
- 不做插值/回退到旧期。

**评分**

```python
def _operating_cashflow_penalty(f: FundamentalFactor, basis: str = "sign") -> float:
    if f.is_financial and basis == "ttm":
        return 0.0
    if basis == "ttm" and f.operating_cashflow_ttm is not None:
        return -_OCF_NEGATIVE_PENALTY if f.operating_cashflow_ttm < 0.0 else 0.0
    # legacy / TTM 缺失：回落累计符号（记录 basis=0）
    if f.operating_cashflow is not None and f.operating_cashflow < 0.0:
        return -_OCF_NEGATIVE_PENALTY
    return 0.0
```

- `score_breakdown`：`ocf_ttm`（值）、`ocf_penalty_basis`（1=TTM，0=sign/回退）、`financial_sector`（1/0）。
- `decision_model` 解析 `ocf_penalty_basis`（缺省 `ttm`；异常/非法 ⇒ `sign`），传给 `compress_scores` / `fundamental_breakdown`；`score_engine` 的 keyword 默认 `"sign"`，保证直接调用者（测试）行为不变。

**实测效果**
- 601668：累计 −453.93 亿，TTM +446.23 亿 ⇒ **无惩罚**；
- 000002：累计 −22.22 亿，TTM +26.79 亿 ⇒ 无惩罚；
- 601012：累计 −34.67 亿，TTM −9.27 亿 ⇒ 仍惩罚（真实负）；
- 600036/601318：`is_financial=True` ⇒ 跳过。

### 8.3 影响与测试

- F 变化：季末负现金流但 TTM 为正的公司上调 0.5；TTM 为负但累计为正的公司下调 0.5。需 ROADMAP 登记并重跑 golden（golden 无 OCF ⇒ 预期不变）。
- **测试**：
  - `test_fundamental_negative_cashflow_penalized`：`basis="sign"` 保持旧断言；新增 `basis="ttm"` + `operating_cashflow_ttm>0` ⇒ 0。
  - 新增 601668-like：`operating_cashflow=−453.93e8`、`ttm=+446.23e8` ⇒ 0。
  - 新增 601012-like：`ttm=−9.27e8` ⇒ −0.5。
  - 新增 600036-like：`is_financial=True`、TTM 负 ⇒ 0。
  - 新增 TTM 缺失 ⇒ 回落 sign（`ocf_penalty_basis=0`）。
- **config 回滚**：`midterm.ocf_penalty_basis: sign`。

---

## 9. P1-3：扣非保守锚（off / shadow / on）

### 9.1 现状

F 的净利项用 `net_profit_yoy`（归母，含非经常性损益）；`profit_dedt → net_profit_dedt` 已在 `fina` 采集中，但 `build_fundamental_factor` 未使用。示例文档明确要求「归母高增长不能直接作为 Fundamental Positive Evidence，必须拆分 recurring / non-recurring」（`docs/midterm/MIDTERM_EXAMPLE.md:133`）。

### 9.2 数据层

- 新增 canonical `net_profit_dedt_yoy`（Tushare `dt_netprofit_yoy`）；`net_profit_dedt` 既有。
- `FundamentalFactor`：
  - `net_profit_dedt`（CNY，累计）；
  - `net_profit_dedt_yoy`（PERCENT）；
  - `net_profit_dedt_base_positive: bool | None`：用 `fina[].profit_dedt` 序列在 `t−4` 的值 > 0；
  - 单季扣非（`q_net_profit_dedt_yoy`）**不在本期**：评分只用累计 `net_profit_dedt_yoy`；单季扣非另立期（避免改 `derive_marginal` 的 4 键契约与现有测试）。

### 9.3 评分层

新增 `f_recurring_mode`（`off` / `shadow` / `on`；生产默认 `shadow`，`score_engine` 函数默认 `off`，见 §3.3）：

```text
off    : np_term = saturate(net_profit_yoy, 30)                  # 现状
shadow : np_term_recurring = saturate(min(归母_yoy, 扣非_yoy), 30)
         F 仍用 np_term；写 np_term_recurring / recurring_gap / f_recurring_shadow
on     : np_term = np_term_recurring；F 变化
```

`f_recurring_shadow` 与 `f` 使用**同一组修正项与同一模式**（OCF/beat/miss/segment 均不变），只把净利项替换为 `np_term_recurring`；P0-1 的期间守卫仍优先生效（不同期时 beat/miss 为 0）。

**可用性**
- `net_profit_dedt_yoy is None` 或 `net_profit_dedt_base_positive is not True` ⇒ 回落归母项，写 `recurring_unavailable=1`；
- 负基数不参与百分比比较（与 `derive_marginal` 的负基数保护一致）；
- `min` 的语义：**只压不抬**——一次性收益被扣非压低；一次性损失（扣非低于归母）会压低 F，属保守取向，必须在 docstring 披露。

**实测**
- 002517 2026Q1：归母 50.65 / 扣非 5.75（正基数）⇒ `on` 的净利项从饱和 +1 降到 `saturate(5.75,30)=0.192`；
- 000002 2026Q3：归母 +14.53 / 扣非 +18.68，但上年同期扣非 −264.86 亿（负基数）⇒ `recurring_unavailable=1`，回落归母（本期不解决亏损归一化，见 N4）。

### 9.4 影响与测试

- 默认 `shadow`：F **不变**，只多诊断键（`np_term_recurring` / `recurring_gap` / `f_recurring_shadow`；不可用时加 `recurring_unavailable=1`）；`on` 才改变 F。
- **测试**：
  - 002517-like 合成：`off` 的 `net_profit_term` 饱和；`shadow` 有 `np_term_recurring≈0.192` 且 F 不变；`on` 的 F 下降。
  - 000002-like 负基数：`recurring_unavailable=1`，`on` 回落归母。
  - 扣非缺失：回落归母。
  - 与 P0-1 同期守卫组合：不同期时 beat 不产，扣非照常。
- **config 回滚**：`f_recurring_mode: off`。

---

## 10. P1-4：营收口径统一（新增 `operating_revenue`）

### 10.1 现状

- L 的 `revenue_yoy` = `or_yoy`（**营业收入**同比）；
- 边际的 `q_revenue_yoy` 用 `income[].revenue` = **营业总收入**（`financial_mapping.yaml:10`）；
- `q_gross_margin` 用「营业总收入 × 毛利率（分母为营业收入）」，存在已披露的近似（`MIDTERM_DECISION_MODEL.md` §3.4.5）。

实测 600519 Q1：`or_yoy=6.538` vs 用营业总收入派生 `6.336`；000333：`2.5501` vs `2.4546`。在 Q1 会把「结构性 0」变成 ±0.1~0.2pp 的口径噪声。

### 10.2 设计

- 新增 canonical `operating_revenue`（营业收入），映射 Tushare `income.revenue`；`income` fields 增加 `revenue`。
- `revenue`（营业总收入，`total_revenue`）保持不变，供既有 derived facts 使用。
- `derive_marginal` 内部选择营收序列：

```python
def _marginal_revenue_series(income):
    op = _series(income, "operating_revenue")
    if op:
        return op, "operating_revenue"
    return _series(income, "revenue"), "total_revenue"
```

- `q_revenue_yoy`、`q_gross_margin` 都用该序列；`FundamentalFactor.marginal_revenue_basis` 记录实际口径。
- `q_gross_margin` 的近似从「营业总收入 × 毛利率」变为「营业收入 × 毛利率」，与 `grossprofit_margin` 同分母；`MIDTERM_DECISION_MODEL.md` §3.4.5 的 R2 风险降级为「仅在 `operating_revenue` 缺失回落总营收时存在」。
- 不在本期补 `oper_cost`（N2/范围控制）；若未来要精确单季毛利率，另立设计。

### 10.3 影响与测试

- 仅边际层（shadow/on）；`q_*` 与 `f_shadow` 轻微变化，F 不变。
- **测试**：
  - 600519 Q1：`operating_revenue` 序列 ⇒ `q_revenue_yoy == or_yoy`（6.538，±1e-6）；
  - 000333 Q1：2.5501；
  - 缺 `operating_revenue` ⇒ 回落 `revenue`，`marginal_revenue_basis="total_revenue"`；
  - `q_gross_margin` 与「营业收入 × 毛利率」一致。
- **文档**：更新 `MIDTERM_DECISION_MODEL.md` §3.4.5 的近似披露；`MIDTERM_F_MARGINAL_DESIGN.md` §3.5 同步。

### 10.4 回滚

无 config；回滚 = 删除 `operating_revenue` 分支（回落总营收）。

---

## 11. P1-5：金融股识别（`company_type` / `is_financial`）

### 11.1 现状

601318/600036/600030 的 `grossprofit_margin=None` ⇒ `q_gross_margin_pp=None` ⇒ `_marginal=None`（边际层整体失效）；OCF 符号惩罚对金融股不适用。

### 11.2 设计

- canonical `company_type`（TEXT）映射 Tushare `income.comp_type`（实测：1 一般工业、2 银行、3 保险、4 证券）；builder 从 `income[0].company_type` 读取（`comp_type` 在 `income` 表，不在 `fina`）。
- `FundamentalFactor.is_financial = company_type in {"2","3","4"}`。
- `comp_type` 缺失 ⇒ `False`（fail-open），并在 `missing_facts` 登记 `company_type`。
- 生效点：
  - P1-2：`ocf_basis="ttm"` 且 `is_financial` ⇒ 惩罚 0；
  - `_marginal`：`is_financial` ⇒ `None`（显式，不依赖毛利率缺失的偶然性）；
  - `score_breakdown`：`financial_sector=1`；
  - 报告：F 分解行追加「金融股：OCF/边际不适用」。
- 不引入银行专用指标（NIM/不良率/拨备），属 N3。

### 11.3 影响与测试

- 金融股 F 不再被 OCF 符号误罚；边际层显式失效但不误导。
- **测试**：
  - 601318/600036/600030-like：`company_type=3/2/4` ⇒ `is_financial=True`；OCF 负 + `ttm` ⇒ 0；`_marginal=None`。
  - `company_type` 缺失 ⇒ `is_financial=False`、`missing_facts` 含 `company_type`。
- **行为**：随 P1-2 的 F 变更；config 回滚 `ocf_penalty_basis: sign`（legacy 下金融股仍按旧符号惩罚）。

---

## 12. P1-6：分部 bonus 期间对齐 + 占比门槛 + 按产品

### 12.1 现状

`_segment_extremes` 取 `latest_segments()` 的所有分项 max/min（`factors.py:316-347`），不看 `category`、不看 `revenue_share`；`_segment_bonus` 与 `f.revenue_yoy` 比较（`score_engine.py:193-205`）。实测 002130/600760 的分部 `latest_period=20260630`，财报为 20260930；002130 同时有「电子通信」母项与「通信线缆/电子材料」子项，且混入按地区分项。

### 12.2 设计

**字段**
- `FundamentalFactor.segment_period: str = ""`；
- `FundamentalFactor.segment_fastest_share: float | None`；
- `FundamentalFactor.segment_basis: str = ""`（`product` / `all`）。

**提取**（`_segment_extremes_v2`，保留旧函数做兼容包装）

```python
_SEGMENT_MIN_SHARE = 10.0  # 最快分部的最小收入占比（%）

def _seg_value(s, name):
    return s.get(name) if isinstance(s, dict) else getattr(s, name, None)

def _segment_extremes_v2(segments, *, category="按产品分类", min_share=_SEGMENT_MIN_SHARE):
    items = segments.latest_segments() if hasattr(segments, "latest_segments") else ...
    product = [s for s in items if _seg_value(s, "category") == category]
    pool = product or items                     # 无按产品 → 回落全部（basis="all"）
    rows = [
        s for s in pool
        if _opt(_seg_value(s, "revenue_yoy")) is not None
        and (_opt(_seg_value(s, "revenue_share")) is None
             or _opt(_seg_value(s, "revenue_share")) >= min_share)
    ]
    if not rows:
        return None, None, getattr(segments, "latest_period", ""), "all", None
    fastest = max(rows, key=lambda s: _opt(_seg_value(s, "revenue_yoy")))
    slowest = min(rows, key=lambda s: _opt(_seg_value(s, "revenue_yoy")))
    return (
        _opt(_seg_value(fastest, "revenue_yoy")),
        _opt(_seg_value(slowest, "revenue_yoy")),
        getattr(segments, "latest_period", ""),
        "product" if product else "all",
        _opt(_seg_value(fastest, "revenue_share")),
    )
```

**评分**

```python
def _segment_bonus(f: FundamentalFactor, mode: str = "legacy") -> float:
    divergence = _segment_divergence(f)
    if divergence is None or divergence <= _SEGMENT_DIVERGENCE_THRESHOLD:
        return 0.0
    if mode == "legacy":
        return _SEGMENT_DIVERGENCE_BONUS
    # period_aligned：期间未知 ⇒ fail-open 旧行为；两期均知且不同 ⇒ 不产
    if f.segment_period and f.report_period and f.segment_period != f.report_period:
        return 0.0
    if f.segment_fastest_share is not None and f.segment_fastest_share < _SEGMENT_MIN_SHARE:
        return 0.0
    return _SEGMENT_DIVERGENCE_BONUS
```

- `score_engine` 函数默认 `legacy`；`decision_model` 解析 config 默认 `period_aligned` 并显式传入（见 §3.3）。

- `score_breakdown`：`segment_period_mismatch`、`segment_share_below_min`。
- `fetch_business_segments` 的 `min_share`/`drop_other` 参数可保留现状（0/False），过滤在 `_segment_extremes_v2` 内做，避免改 collector 默认行为。

### 12.3 影响与测试

- Q3/H1 错期帧不再加 0.2；小分部不再触发；母子公司/地区分项被过滤。
- 002130 当前 `divergence=37.27−22.34=14.93 <15` 本就不加；新增「H1 分部 vs Q3 财报」用例确认即使 >15 也不加。
- **测试**：
  - 002130-like：segment_period=20260630、report_period=20260930、divergence=20 ⇒ 0；
  - 同期 H1 帧：divergence=20 ⇒ +0.2；
  - 2% 占比分部 yoy=50、整体=10 ⇒ 0；
  - `category` 混合：按产品 20pp vs 按地区 30pp ⇒ 只取按产品；
  - `segment_period` 或 `report_period` 缺失 ⇒ legacy（fail-open）。
- **config 回滚**：`midterm.segment_bonus_mode: legacy`。

---

## 13. P2-1：报告期 provenance 与陈旧标记

### 13.1 设计

- `FundamentalFactor`：`report_period`（P0-1）、`report_ann_date`、`report_age_days`、`marginal_period`。
- `get_factor_snapshot` 在构建后按 `as_of_date = today` 计算 `report_age_days = (today − ann_date).days`；`ann_date` 缺失 ⇒ `None`。
- `midterm_reporter._render_f_breakdown` 追加一行：

```text
F 数据期: 2026Q3（公告 2026-10-31，距今 2 天）
F 数据期: 2026Q3（公告 2026-10-31，距今 128 天，**陈旧**）
```

- `_F_STALE_DAYS = 120`（模块常量，不进 config；后续可校准）。A+H 公司（688981 2025Q3 公告 2025-11-14）会自然显示陈旧。
- 报告/任务记录只读字段，不重算。

### 13.2 测试

- builder 设置 `report_period/report_ann_date`；
- reporter 渲染含/不含「陈旧」两态；
- 旧帧（无字段）渲染保持原行（不出现新行）。

### 13.3 影响

纯展示/provenance；F 不变。

---

## 14. P2-2：F 输入序列快照（最小 PIT）

### 14.1 目标

让影子/校准能回答「这帧 F 是用哪些原始值算的」，并在重述后仍可回放；不做全历史回填（N5）。

### 14.2 设计

新增 `alphabee/midterm/financial_inputs.py`：

```python
class FinancialInputSnapshot(BaseModel):
    schema_version: str = "1"
    symbol: str
    as_of_date: str                 # YYYY-MM-DD
    report_period: str              # YYYYMMDD
    report_ann_date: str
    source: str = "tushare"
    series: dict[str, dict[str, float]]  # field -> {period: value}
    content_hash: str

def build_financial_inputs(fin_data: dict, *, as_of_date: str) -> FinancialInputSnapshot
def append_financial_inputs(snapshot, base_dir: Path | None = None) -> bool   # 幂等 append
def load_financial_inputs(symbol, *, base_dir=None) -> list[FinancialInputSnapshot]
```

- `series` 只存 F 需要的 6 条：`revenue`、`operating_revenue`、`net_profit_attr_p`、`net_profit_dedt`、`gross_margin`、`operating_cashflow`（每条 8 期，约 48 个点）。
- 目录：`data/midterm/financial_inputs/<symbol>.jsonl`；`append` 以 `(symbol, as_of_date, report_period, content_hash)` 幂等；原子追加；坏行跳过。
- 接线：`get_factor_snapshot` 在 F 构建后，仅当 `midterm.persist_financial_inputs=true` 时写入；纯函数 `build_fundamental_factor` 不写盘。
- 保留：不自动清理；按需归档。

### 14.3 测试

- `build` 纯函数稳定、hash 稳定；
- 同内容两次 append ⇒ 1 行；
- 不同 as_of/report_period ⇒ 追加；
- 读回按 as_of 升序、坏行跳过；
- flag=false ⇒ 零文件。

### 14.4 影响

无决策路径变化；只增加可选 I/O。

---

## 15. P2-3：影子观测扩展与阈值校准

### 15.1 指标扩展（`marginal_shadow.py`）

| 指标 | 口径 | 用途 |
|---|---|---|
| S1 `ΔF` | ex-Q1 的 `f_shadow−f` 中位数/P90 | 开 `on` 门槛（≤0.10） |
| S2 翻转率 | ex-Q1 的状态翻转占比 | ≤10% |
| S3 危险翻转 | 现判 S5 → 影子 {S1,S2,S3} | ≤3% |
| S4 覆盖率 | ex-Q1 的 `f_marginal_source>0` 占比 | ≥85% |
| S5 OCF 命中率 | 按 `ocf_penalty_basis` 分组的命中率、按报告季度/行业 | 判断 TTM 修正效果 |
| S6 beat/miss 同期率 | `forecast_period_matched=1` 的帧占比 | 判断 P0-1 生效面 |
| S7 扣非差异 | `np_term_recurring` 与归母项的分布 | 决定 `f_recurring_mode=on` |
| S8 分部分布 | `segment_bonus` 触发率、期间错配率 | 判断 P1-6 |

### 15.2 校准脚本

- 扩展 `scripts/f_marginal_shadow.py`（或新增 `scripts/f_calibration.py`），输出 JSON + Markdown；
- 网格：`_FUNDAMENTAL_YOY_SCALE ∈ {20,30,40}`、`F_ACCEL_SCALE ∈ {15,25,35}`、`_OCF_NEGATIVE_PENALTY ∈ {0.3,0.5,0.7}`、`F_MARGIN_DELTA_SCALE ∈ {3,4,6}`；
- 样本：真实帧 ≥50、覆盖 ≥20 只标的、至少包含 4 个报告季度；否则输出「样本不足，不校准」；
- 评估：状态稳定性、S1–S4、前向收益（若 `market_regime/forward_returns.py` 可用）；
- 产物：`docs/implementation/midterm/F_CALIBRATION_REPORT.md`（只出报告，不自动改常量）；
- 开 `f_marginal_mode=on` 的前置：G-8 全绿（S1–S4 ex-Q1）+ 单独 ROADMAP 登记 + 人工评审；开 `f_recurring_mode=on` 的前置：S7（扣非差异分布）可解释 + 单独登记 + 人工评审。两者互不替代。

### 15.3 测试

- 指标纯函数（S1–S8）用合成帧；
- 样本不足分支；
- JSON 输出 schema 稳定。

---

## 16. P2-4：文档与 ROADMAP 登记

| 文件 | 改动 |
|---|---|
| `docs/roadmap/ROADMAP.md` | 每个 PR 一行（见 §19）；P0-1/P0-2 登记为 bug 修复；P1-2/P1-6 登记为行为变更；P1-3 登记为「默认 shadow，无 F 变化」 |
| `docs/guide/CONFIGURATION.md` | `midterm` 段追加 `ocf_penalty_basis` / `f_recurring_mode` / `segment_bonus_mode` / `persist_financial_inputs` 四键的语义、fail-open、回滚 |
| `docs/midterm/MIDTERM_DECISION_MODEL.md` | §3.4 公式更新（beat/miss 同期、OCF TTM、扣非 shadow、Q1 降级、分部期间对齐）；§3.4.5 口径近似 R2 收敛；新增 §3.4.6 Q1 降级说明 |
| `docs/implementation/midterm/MIDTERM_F_MARGINAL_DESIGN.md` | §3.2 Q1 语义、§3.5 营收口径、§5.2 影子指标 ex-Q1、§7.1 G-8 口径 |
| `docs/midterm/MIDTERM_FACTOR_DATA_DESIGN.md` | §2.2 数据源表补 `revenue/comp_type/ann_date/update_flag/dt_netprofit_yoy`；§2.3 字段表补 3 个 canonical |
| `docs/midterm/DATA_CONTRACTS_VERIFIED.md` | 追加 2026-11-02 实测：`update_flag` 重复行、`comp_type` 取值、`dt_netprofit_yoy`、`income.revenue` vs `total_revenue` |
| `docs/implementation/README.md` | `midterm/` 行追加本设计文档 |
| `alphabee/schemas/INDEX.yaml` | `financial.field_count` 42 → 45 |
| `config.yaml.example` | `midterm` 段补四个新键（注释含默认值与回滚） |

---

## 17. 测试矩阵

### 17.1 新增/修改测试

| 测试文件 | 用例 | 期望 |
|---|---|---|
| `tests/agents/facts/tools/test_financial_fact_dedup.py`（新） | 688981 三行 | 57,795,570,000 |
| | 000002 fina 两行（1 有值/0 NaN） | `profit_dedt` 非空 |
| | `report_type=1/4` | 取 1 |
| | `ann_date` 缺失 | 回落 period |
| | `ref_dates` 降序 | 保持 |
| `tests/midterm/test_score_engine.py`（改） | 600760 帧 | `beat=0`、`f=−0.9333` |
| | 同期帧 | `beat=0.2` |
| | 空期间 | `beat=0` |
| `tests/midterm/test_factors.py`（改） | `build_expectation_factor` | `profit_forecast_period` = 最新预告期 |
| | `build_fundamental_factor` | `report_period` / `report_ann_date` / `marginal_period` |
| `tests/midterm/test_marginal_derivation.py`（改） | D7 净利腿 | 新分母下重标定；新增 600028-like 通过 |
| | Q1 | `derive_marginal` 仍产出 4 个 q_* |
| `tests/midterm/test_f_marginal_scoring.py`（改） | Q1 三档 | Q1 无边际键；on==off |
| | 年报 | `f_marginal_source=7` |
| | 1% 门槛 | 低毛利通过、真实低基数仍 None |
| `tests/midterm/test_ocf_penalty.py`（新） | 601668-like TTM 正 | 0 |
| | 601012-like TTM 负 | −0.5 |
| | 600036-like 金融 | 0 |
| | TTM 缺失 | 回落 sign |
| `tests/midterm/test_recurring.py`（新） | 002517-like 50.65/5.75 | shadow 有键、F 不变；on 降 F |
| | 000002-like 负基数 | `recurring_unavailable=1` |
| | 扣非缺失 | 回落归母 |
| `tests/midterm/test_revenue_basis.py`（新） | 600519 Q1 | q_rev == or_yoy |
| | 000333 Q1 | 2.5501 |
| | 缺 operating_revenue | 回落 total_revenue |
| `tests/midterm/test_segment_bonus.py`（新） | 002130 H1 vs Q3 | 不加 |
| | 同期 H1 | +0.2 |
| | 2% 分部 | 不加 |
| | 期间缺失 | legacy |
| `tests/midterm/test_report_provenance.py`（新） | reporter 行 | 含/不含「陈旧」 |
| `tests/midterm/test_financial_inputs.py`（新） | 幂等/hash/坏行 | 见 §14.3 |
| `tests/midterm/test_marginal_shadow.py`（改） | ex-Q1 覆盖 | S4 分母排除 Q1 |
| `tests/midterm/test_v1_artifact_golden.py`（改） | 剔除集合 | 新字段不参与断言 A |
| `tests/midterm/test_decision_model.py`（改） | 模式解析 | 新增三个 resolve 的 fail-open |
| `tests/midterm/test_decision_model_smoke.py`（改） | langchao 快照 | 补同期字段（报告期/预告期同值）以保留 0.589；若留空则期望降为 0.389 |

### 17.2 回归命令

```bash
# 聚焦
poetry run pytest -q tests/midterm tests/agents/facts/tools tests/company_track
# 全量（非 integration）
poetry run pytest -q -m "not integration"
# 静态
poetry run ruff check alphabee tests scripts
poetry run mypy alphabee
```

### 17.3 变异探针（每个 P0/P1 修复必须至少一条）

| 变异 | 预期必红 |
|---|---|
| 删除期间守卫 | 600760 用例（beat 0.2） |
| 去重改回 `keep="first"` | 688981 用例 |
| 删除 Q1 降级 | Q1 `on==off` 用例 |
| 停用 TTM | 601668 用例 |
| 删除金融跳过 | 600036 用例 |
| 扣非改回归母 | 002517 `on` 用例 |
| 分部去掉期间检查 | 002130 Q3 用例 |
| 分部去掉占比门槛 | 2% 分部用例 |

---

## 18. PR 切分与依赖

| PR | 内容 | 依赖 | 行为面 | 回滚 | 门禁 |
|---|---|---|---|---|---|
| **PR-1** | P0-1：期间字段 + 守卫 + 测试 | — | F 变（错期 beat 消失） | revert | 600760/同期/空期 |
| **PR-2** | P0-2：fields + `_dedup` + 测试 | — | 数据变 | revert | 688981/000002/type |
| **PR-3** | P0-3：`marginal_period` + Q1 降级 + shadow ex-Q1 | PR-1（period 字段） | shadow 键变；F 不变 | revert | Q1 on==off |
| **PR-4** | P1-1 + P1-4：1% 分母 + `operating_revenue` + 测试 | PR-3 | 边际层 | revert | 600028/600519 |
| **PR-5** | P1-2 + P1-5：TTM OCF + `company_type` + config + 测试 | PR-2（ann_date/comp_type） | **F 变** | `ocf_penalty_basis: sign` | 601668/601012/600036 |
| **PR-6** | P1-3：扣非数据层 + `f_recurring_mode`（默认 shadow） | PR-1/PR-2 | 无 F 变化（shadow） | `f_recurring_mode: off` | 002517/000002 |
| **PR-7** | P1-6：分部期间/占比/类别 + config | PR-1（report_period） | **F 变** | `segment_bonus_mode: legacy` | 002130 |
| **PR-8** | P2-1：provenance + reporter 陈旧行 | PR-1 | 无 | revert | reporter 两态 |
| **PR-9** | P2-2：financial_inputs 快照 | PR-3/PR-4 | 无（默认关） | flag off | 幂等/hash |
| **PR-10** | P2-3 + P2-4：影子指标/校准脚本 + 文档/ROADMAP | PR-1..PR-9 | 无 | 删除脚本/回退文档 | 指标纯函数、文档清单 |

**顺序理由**：P0-1/P0-2 先修当前错误；P0-3 依赖 `report_period`；P1-4 与 P1-1 同属边际数据层；P1-2 依赖 `comp_type`（P0-2 的 fields）；P1-3/P1-6 依赖期间字段；P2 最后。

---

## 19. 行为变更与回滚矩阵

| 修复 | 默认行为变化 | 回滚 | ROADMAP 登记 |
|---|---|---|---|
| P0-1 beat/miss 同期 | **是**（错期 beat/miss 归零；600760 −0.7334→−0.9334） | revert（不推荐） | bug 修复行 |
| P0-2 去重取最新 | **是**（重述/快报公司数据变） | revert（不推荐） | bug 修复行 |
| P0-3 Q1 降级 | 否（F 不变；shadow 键变） | revert | 无行为变更（诊断） |
| P1-1 1% 分母 | 否（边际层） | revert | 无 F 行为变更 |
| P1-2 TTM OCF | **是**（F 变） | `ocf_penalty_basis: sign` | 行为变更行 |
| P1-3 扣非 | 否（默认 shadow）；`on` 时是 | `f_recurring_mode: off` | 默认 shadow 行；`on` 另登记 |
| P1-4 营收口径 | 否（边际层） | revert | 无 F 行为变更 |
| P1-5 金融股 | 是（随 P1-2） | `ocf_penalty_basis: sign` | 与 P1-2 同行 |
| P1-6 分部 | **是**（F 变） | `segment_bonus_mode: legacy` | 行为变更行 |
| P2-1/2/3 | 否 | revert/flag | 无 |

**golden 策略**
- P0-1/P1-2/P1-6 先跑 golden：若断言 A 失败，按「行为变更」重基线，并在 PR 描述记录旧/新值；
- P0-2 的 golden 输入为合成数据，无重复行 ⇒ 预期不变；
- P0-3/P1-1/P1-3(shadow)/P1-4 只改诊断键/边际键，golden 剔除后不变。

---

## 20. 验收标准

- [ ] `poetry run pytest -q tests/midterm tests/agents/facts/tools tests/company_track` 全绿；全量非 integration 全绿。
- [ ] 600760 帧：`beat=0`、`f=−0.9333`；同期夹具 `beat=0.2`。
- [ ] 688981 去重：57,795,570,000；`fina_indicator` 扣非非空。
- [ ] Q1：`_marginal=None`；`fundamental_breakdown(on)==off`；shadow 无边际键。
- [ ] 600028/601668：`q_net_profit_yoy` 非空。
- [ ] 601668/601012/600036：OCF 惩罚 0/−0.5/0。
- [ ] 002517：`shadow` 的 `np_term_recurring≈0.192` 且 F 不变；`on` 的 F 下降。
- [ ] 600519 Q1：`q_revenue_yoy == or_yoy`（6.538）。
- [ ] 002130 Q3：segment bonus=0；同 H1 帧 +0.2；2% 分部 0。
- [ ] reporter 出现「F 数据期」；>120 天出现「陈旧」。
- [ ] `ruff check` / `mypy alphabee` 全绿。
- [ ] ROADMAP / CONFIGURATION / DECISION_MODEL / F_MARGINAL / FACTOR_DATA / DATA_CONTRACTS / INDEX / README / config.example 全部更新。
- [ ] 每条行为变更有一条变异探针转红。

---

## 21. 未决问题与决策记录

| 编号 | 问题 | 本设计选择 | 备选/后续 |
|---|---|---|---|
| D1 | TTM 缺失时 OCF 怎么判 | 回落累计符号（`ocf_penalty_basis=0`），不静默免罚 | 也可「缺失不罚」；需影子数据决定 |
| D2 | 金融股识别用 `comp_type` 还是行业 | `comp_type`（同接口、稳定、实测 2/3/4 可用） | 行业 L1 作为 fallback（P2） |
| D3 | 扣非用 `min` 还是「扣非优先」 | `min(归母, 扣非)`（只压不抬） | 若误伤一次性损失，可在影子期后改「扣非优先」 |
| D4 | 分部占比门槛 10% | 10.0（结构性示意） | 用影子期/行业分布校准 |
| D5 | Q1 是否允许「Q1 vs 上年 Q4」的加速度 | 不做（本期降级） | 另立设计，需季节性校准 |
| D6 | PIT 快照范围 | 只存 F 的 6 条序列（最小可用） | 全表快照/外部存储（后续） |
| D7 | `report_age_days` 阈值 120 天 | 模块常量 | 可按板块/披露日历配置化 |
| D8 | 是否把 `report_period` 注册 canonical | 否（模型 provenance） | 若下游多模块消费再走 schema 流程 |

---

## 22. 风险登记

| 编号 | 风险 | 触发条件 | 缓解 |
|---|---|---|---|
| R1 | P0-1 fail-closed 误伤「真实同期但期间缺失」的旧调用 | 手工构造快照、第三方调用未填 period | 单测覆盖空期间；文档要求 builder 必填；报告显示 `forecast_period_matched` |
| R2 | P0-2 排序依赖 `ann_date/update_flag` 可用性 | Tushare 某些接口不返回或返回空 | 缺失回落 `period` 排序；`report_type` 无 1 时保留全部；集成冒烟 |
| R3 | TTM OCF 改变 F 面过大 | 所有现金流季节性公司 | config `ocf_penalty_basis: sign` 一行回滚；先跑全量影子对照 |
| R4 | 扣非 `min` 对一次性损失过度保守 | 扣非显著低于归母（资产减值/处置损失） | 默认 shadow；S7 观察；必要时改「扣非优先」或加损失豁免 |
| R5 | 分部 10% 门槛误杀合法小分部亮点 | 小占比高增长业务 | config `segment_bonus_mode: legacy`；影子期校准门槛 |
| R6 | `comp_type` 缺失导致金融股漏判 | 个别发行人/接口缺列 | fail-open 非金融 + `missing_facts` 登记；后续行业 L1 fallback |
| R7 | PIT 快照文件增长 | 长期 tracking/多次查询 | 默认关；按 symbol 分文件；定期归档；只存 6 条序列 |
| R8 | Q1 降级降低影子覆盖率 | Q1 帧占比高 | `coverage_ex_q1` 单独报告；G-8 用 ex-Q1；不把 Q1 计入分母 |
| R9 | `operating_revenue` 与既有 `revenue` 混用 | 下游误读两个 canonical | schema notes + 字段名区分；`marginal_revenue_basis` 记录实际口径 |

---

## 附录 A：代码锚点速查

| 文件 | 函数/位置 | 本设计的改动 |
|---|---|---|
| `alphabee/agents/facts/tools/financial_fact.py` | `_dedup` / `get_financial_fact` | fields + 排序去重 + `ann_date` 保留 |
| `alphabee/adapters/tushare/financial_mapping.yaml` | `income` / `fina_indicator` | 3 个新映射 |
| `alphabee/schemas/financial.yaml` | 字段定义 | 3 个新 canonical |
| `alphabee/midterm/factors.py` | `build_fundamental_factor` / `derive_marginal` / `_segment_extremes` | 期间/基期/营收/TTM/分部/扣非字段 |
| `alphabee/midterm/score_engine.py` | `_operating_cashflow_penalty` / `_beat_bonus` / `_miss_penalty` / `_segment_bonus` / `_marginal` / `fundamental_breakdown` | 期间守卫 / TTM / 扣非 / 分部 / Q1 |
| `alphabee/midterm/models.py` | `FundamentalFactor` / `ExpectationFactor` | additive 字段 |
| `alphabee/midterm/decision_model.py` | `resolve_*` / `evaluate` / `_writeback_f_breakdown` | 三个 resolve + 透传 + provenance |
| `alphabee/midterm/marginal_shadow.py` | `frame_stats` / `shadow_report` | ex-Q1 + S5–S8 |
| `alphabee/orchestrator/nodes/midterm_reporter.py` | `_render_f_breakdown` | 「F 数据期」/「陈旧」行 |
| `alphabee/midterm/financial_inputs.py`（新） | 快照模块 | P2-2 |
| `scripts/f_marginal_shadow.py` / `scripts/f_calibration.py`（新） | 校准 | P2-3 |

## 附录 B：实测复算

| 用例 | 输入 | 期望 |
|---|---|---|
| 600760 | 2026Q3 财报 + 2026H1 预告 | `beat=0`；`f=−0.9333` |
| 600760 同期 | 2026Q3 财报 + 2026Q3 预告 | `beat=0.2` |
| 688981 2024 年报 | 三行重复 | 57,795,570,000 |
| 600028 净利腿 | 单季营收 ~8000 亿、单季净利 85 亿、基数 85 亿 | 通过 |
| 601668 OCF | cum −453.93、TTM +446.23 | 0 |
| 601012 OCF | cum −34.67、TTM −9.27 | −0.5 |
| 600036 OCF | is_financial=True | 0 |
| 002517 扣非 | 归母 50.65 / 扣非 5.75 | shadow `np_term_recurring≈0.192` |
| 600519 Q1 | `or_yoy=6.538` | `q_revenue_yoy=6.538` |
| 002130 分部 | H1 分部 / Q3 财报 | segment bonus=0 |

## 附录 C：公式速查

```text
# F 水平项（不变）
L = mean(sat(revenue_yoy,30), sat(net_profit_yoy,30), sat(eps_growth_yoy,30))

# 期间守卫（P0-1）
beat = periods_match(report_period, forecast_period) and np_yoy > fc_max ? +0.2 : 0
miss = periods_match(report_period, forecast_period) and np_yoy < fc_min−20 ? −0.3 : 0

# OCF（P1-2）
TTM_OCF(t) = FY(prev) + cum(t) − cum(t−4)   # Q1–Q3
TTM_OCF(t) = cum(t)                          # 年报
penalty = is_financial ? 0 : (TTM_OCF < 0 ? −0.5 : 0)

# 扣非（P1-3，on）
np_term = sat(min(归母_yoy, 扣非_yoy), 30)   # 扣非可用且上年同期扣非>0
        = sat(归母_yoy, 30)                  # 否则

# 边际（P0-3/P1-1/P1-4）
Q1 ⇒ marginal = None（F=L）
Δ_rev = q_revenue_yoy（operating_revenue） − revenue_yoy（or_yoy）
Δ_np  = q_net_profit_yoy − net_profit_yoy
M_raw = mean(sat(Δ,25)); Q = clip(1+q_gm_pp/4, .2, 1); M = M_raw×Q

# 分部（P1-6）
bonus = +0.2 iff segment_period == report_period
        and fastest_share ≥ 10%
        and fastest_yoy − revenue_yoy > 15
```
