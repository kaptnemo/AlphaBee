# beat/miss 经验 LR 离线研究设计（证据似然标定，B 类）

> 关联文档：
> - `docs/midterm/MIDTERM_EVIDENCE_EXTRACTION.md` — §6 `confidence_delta` 标定（本设计取代其手工阈值）、§7 去重与独立性（本设计沿用其口径）
> - `docs/design/RULE_MARKET_ADAPTATION_DESIGN.md` — D1 `BacktestHarness` 预留（本研究的 P4 收敛目标）
> - `docs/midterm/MIDTERM_FACTOR_DATA_DESIGN.md` — §3.2 预告/快报数据源（Tushare `forecast` / `express`）
> - `alphabee/midterm/evidence_rules.py` — 待标定常量 `_BEAT_WEAK_MAX` / `_BEAT_MEDIUM_MAX`、A1 相关证据压缩口径
> - `alphabee/midterm/bayes.py` — `_event_logodds` 的 LR 参数化 `(1±d)/(1∓d)`
> - `alphabee/market_regime/forward_returns.py` — 前向收益计算的复用蓝本

---

## 0. 定位与目标

**一句话**：用 A 股历史数据（业绩预告/快报 + 行情）做一次完全离线的频率研究，把
`evidence_rules.py` 里 beat/miss 的「证据强度」标定——目前是手工拍的阈值（5% / 15%）
与三档 `d ∈ {0.1, 0.3, 0.5}` → LR 公式 `(1+d)/(1−d)`——替换为**从历史频率估计的经验
似然比（empirical LR）**，使贝叶斯引擎的似然侧第一次有数据支撑。

**边界（本文档范围外，各自另立文档或列入后续批次）**：

| 范围外事项 | 原因 |
|---|---|
| LLM 判断（Stage B weak/medium/strong、insight confidence 标签）的校准 | C 类，需在线运行积累（同源 LLM 判断无法离线回放） |
| 先验 P(H) 的基率标定 | B 类但属先验侧，与本文档配套；本文档只输出研究所需的基率统计 |
| revision（分析师上修/下修）的经验 LR | 东财 consensus 历史深度有限，列为 P4 之后 |
| 反证权重 1.2×、钳位 [0.05, 0.95]、状态锚点表的校准 | 依赖本文档产出的证据级 LR 后才可进行，列为后续批次 |
| 跨渠道（数值 vs Stage B 定性）语义去重 | A 类已做通道解耦，模糊语义匹配过于脆弱，暂不做 |

---

## 1. 要标定什么：证据定义与统计对象

### 1.1 证据 E（beat/miss deviation）

与生产规则 `_classify_beat` 同口径：

```text
actual = express_net_profit_yoy        # 业绩快报实际净利润同比（raw: tushare express.yoy_dedu_np）
[lo, hi] = 业绩预告区间                 # raw: tushare forecast.p_change_min / p_change_max
deviation = actual − hi   （beat，> 0）
          = lo − actual   （miss，> 0，记负号表示方向）
inline（lo ≤ actual ≤ hi）→ 现有规则不产事件（"无意外即无证据"）
```

研究单位：**每个 (ts_code, end_date) 一条事件**——与 A1 相关证据压缩后的生产口径
完全一致（同报告期至多一条数值证据，见 `evidence_rules.py` 模块 docstring）。

### 1.2 结局 Y：thesis H 的代理

引擎里的 H =「thesis 成立」（LLM 生成、每次不同、不可回放）。本研究用可观测的代理：

```text
Y = 1  ⇔  未来 N 个交易日，个股相对基准的超额收益 > 0
```

- **主口径**：N = 126 交易日（≈6 个月，与 midterm 周级/事件驱动的生命周期匹配）；
  敏感性：63 / 252 交易日。
- **基准**：中证全指 `000985.CSI` 为主口径；敏感性：沪深300 `000300.SH`、申万 L1
  行业指数（行业中性化，需 `index_member_all` 归属，2000 积分）。
- 超额收益 = 个股复权收益 − 基准同期收益；`ann_date` 之后的**第一个交易日**起算。

### 1.3 目标量：经验似然比

对 deviation 分桶 b（初始桶沿用现有阈值做对照，最终桶由 §3.6 数据驱动）：

```text
LR(b) = P(E ∈ b | Y=1) / P(E ∈ b | Y=0)
```

等价估计（同一组计数，数值稳定，报告两者互验）：

```text
LR(b) = [ P(Y=1 | E∈b) / P(Y=0 | E∈b) ] ÷ [ P(Y=1) / P(Y=0) ]
```

- **inline 桶必须估计**：现有规则对它"不产事件"（隐含 LR=1）。研究要检验该假设；
  若 inline 桶 LR 显著偏离 1，说明「无意外即无证据」不成立，需要单独出设计变更。
- 输出主表形态：

| bucket（deviation pp） | n | P(Y=1\|b) | LR̂ | 95% CI | 平滑后 LR̂ |
|---|---|---|---|---|---|
| miss > 15 | … | … | … | … | … |
| miss 5–15 | … | … | … | … | … |
| inline | … | … | ≈1? | … | … |
| beat 0–5 | … | … | … | … | … |
| beat 5–15 | … | … | … | … | … |
| beat > 15 | … | … | … | … | … |

### 1.4 语义差与处理：LR 是「跑赢基准」的诊断度，不是严格 P(E|H)/P(E|¬H)

这是本设计**最诚实的边界**，写进报告首页：

1. 标定出的 LR 描述「该幅度 beat/miss 对未来跑赢基准的诊断力」，而引擎消费的是
   「证据对 thesis H 的诊断力」。二者只有在「H 成立 ≈ 未来跑赢基准」的代理假设下等价。
2. **缓解措施**：
   - 本标定只替换似然侧数值；先验侧基率标定（另立文档）后，先验也改用 P(Y=1) 基率，
     两侧语义统一对齐到 Y，代理假设自洽；
   - 报告必须同时输出**分层基率** P(Y=1)（全样本 / 按年份 / 按市值 / 按申万 L1），
     供先验标定复用；
   - 用分层 LR（§3.7）检验迁移性：若行业/年份/市值分层的 LR 差异显著，则说明证据
     强度需要条件化（如按行业乘系数），这是后续版本，不影响本批次先出全样本表。

---

## 2. 数据需求与接口契约（先冒烟，后使用）

> 纪律（`alphabee/data-source-contract`）：以下接口名/字段基于生产链路既有调用
> （`expectation_fact.py` / `TuShareHelper`），但**历史深度、积分门槛、缺失率必须
> P0 冒烟实测确认**，不凭接口名猜。

### 2.1 事件数据

| 接口 | 拉取方式 | 关键字段（raw） | 用途 |
|---|---|---|---|
| Tushare `forecast` | 全市场按 `ann_date` 区间**逐周**拉取（避免单次行数上限） | `ts_code, ann_date, end_date, type, p_change_min, p_change_max` | 预告区间 [lo, hi] |
| Tushare `express` | 同上 | `ts_code, ann_date, end_date, yoy_dedu_np` | 快报实际净利润同比 |

- 历史深度预期：`forecast` 约 2015 年起、`express` 更早（**以实测为准**）；研究窗口
  取两者交集。
- 积分门槛：**以实测为准**（冒烟失败立即记录，见 P0 清单）。
- 字段口径对账：研究用 raw 字段，但必须与生产 canonical 口径对账一次
  （`alphabee/adapters/tushare/expectation_mapping.yaml`：`p_change_min/max →
  profit_forecast_min_change/max_change`、`yoy_dedu_np → express_net_profit_yoy`），
  保证回填后的标定表作用于生产同口径数据。

### 2.2 行情与基准

| 接口 | 关键字段 | 用途 |
|---|---|---|
| Tushare `daily`（个股）+ `adj_factor` | `trade_date, close, adj_factor` | 个股复权收益 |
| Tushare `index_daily` | `trade_date, close` | 基准收益（000985.CSI / 000300.SH / 申万 L1 `sw_daily`） |

- 复权口径：后复权（`close × adj_factor / 最新 adj_factor`），与基准同口径可比。
- 注意：`forward_returns.py` 现用**日历日**窗口（`DEFAULT_HORIZON_DAYS=126`）；本研究
  改用**交易日**口径（停牌稳健），在文档中显式声明与既有引擎的差异及理由。

### 2.3 股票池与过滤

| 项 | 规则 | 动机 |
|---|---|---|
| 全历史池 | `stock_basic(list_status 全量，含 D/P)` | 消除幸存者偏差 |
| 次新剔除 | `list_date` 晚于事件日前 1 年 | 次新无预告基线、波动异常 |
| ST 剔除 | 事件日名称含 ST / `namechange` | 规则面干扰 |
| 上市状态 | 事件日处于交易状态 | 可交易性 |
| 预告类型 | 只保留 `type` 为有区间的正常预告（预增/预减/略增/略减/扭亏等，**以实测枚举为准**） | 区间口径一致 |

### 2.4 P0 冒烟清单（写代码前必须完成，输出 `接口核实` 结构化结论）

1. `forecast`：最早 `ann_date` 到哪年？全市场单周行数峰值？`p_change_min/max` 缺失率？
   `type` 取值枚举？所需积分？
2. `express`：同上，`yoy_dedu_np` 缺失率？
3. `daily` + `adj_factor`：复权链完整性（个别股票 adj_factor 缺失率）？
4. `index_daily`：000985.CSI 历史起点？
5. 拉取限频：全市场历史回填预计请求数 / 分钟级限频是否满足，是否需要断点续传。

---

## 3. 研究管线（scripts/research/beat_miss_lr/）

### 3.1 目录与脚本布局

```text
scripts/research/beat_miss_lr/
  smoke_test.py        # P0 冒烟：接口契约、历史深度、积分、缺失率（输出 接口核实 结论）
  fetch_events.py      # forecast/express 全市场拉取 → 事件表（断点续传）
  fetch_returns.py     # 个股/基准行情 → 前向超额收益（防前视）
  estimate_lr.py       # 分桶频率估计 + Dirichlet 平滑 + 块 bootstrap CI
  binning.py           # 单调性约束分箱 → 数据驱动阈值边界
  report.py            # calibration_report.md + params（beat_miss_lr.yaml）
data/research/beat_miss_lr/
  events.parquet       # 中间产物（gitignore）
  returns.parquet
  beat_miss_lr.yaml    # 标定产物（回填参数文件，版本化）
  calibration_report.md
```

### 3.2 事件对齐（与 A1 压缩口径一致）

```text
forecast/express 按 (ts_code, end_date) 对齐：
- 有 express 实际值 + 有效预告区间 → beat/miss/inline 事件（研究主干）
- 有 express、无有效预告 → 符号定方向退化路径（单独统计，不并入主干 LR）
- 无 express、只有预告 → 不产 beat/miss 事件（生产口径即如此，研究不纳入）
- 同 (ts_code, end_date) 多条记录 → 取首条（与 A1 的 period 唯一性一致）
```

事件表 schema：`ts_code, end_date, ann_date, forecast_lo, forecast_hi, express_yoy,
deviation, event_type ∈ {beat, miss, inline, sign_only}`。

### 3.3 前向超额收益（复用 `forward_returns.py` 的防前视模式）

- 起算日 = `ann_date` 后第一个交易日（盘后公告按次日起算，周末/节假日顺延）；
- 窗口内个股停牌日跳过、以「最后收盘 vs 起始收盘」计收益（与 `compute_forward_returns`
  的 `window.iloc[-1]` 同逻辑）；连续停牌 > 20 交易日的样本剔除并计数；
- **防前视断言**：任何事件不得使用 `ann_date` 之前的信息计算结局；窗口未走完的
  事件直接排除（沿用既有约束）；
- 退市个股：若窗口内退市，按「最后可交易价 → 0」计极端负收益并单独标记
  （与剔除法做敏感性对照）。

### 3.4 频率估计与平滑

对每个桶 b：

```text
n_11 = #(E∈b ∧ Y=1),  n_10 = #(E∈b ∧ Y=0)      （桶-结局计数）
P̂(E∈b | Y=1) = (n_11 + α) / (Σ_b n_1b + K·α)    # Dirichlet 平滑，α=0.5
P̂(E∈b | Y=0) = (n_10 + α) / (Σ_b n_0b + K·α)
LR̂(b) = P̂(E∈b | Y=1) / P̂(E∈b | Y=0)
```

- 平滑参数 α ∈ {0.5, 1.0} 做敏感性；样本量 < 30 的桶默认**合并**到相邻桶并注明；
- 同时输出贝叶斯翻转口径 LR 互验（§1.3 第二式）。

### 3.5 置信区间（重叠样本必须处理）

- **块 bootstrap**：以 (ts_code × 事件) 为块重抽样 1000 次，报告 LR 的 95% 百分位 CI；
- **重叠稳健性**：同一股票相邻报告期（间隔 3–6 个月）的 6 个月前向窗口互相重叠，
  样本不独立——额外做「每 (ts_code, 滚动 126 交易日) 只保留一条」的下采样估计，
  与全样本估计并列报告；两口径结论不一致时以保守（下采样）口径为准。

### 3.6 分箱与阈值（数据驱动，取代 5% / 15%）

1. 对 deviation 做**单调性约束分箱**（isotonic regression 或决策树桩 + 单调约束），
   拟合集上以「验证集区分度（AUC）最大化 + LR 单调」选桶边界；
2. 初始候选边界沿用现有 {−15, −5, 0, 5, 15} 作为对照基准（报告增量）；
3. 分箱搜索**只在拟合集**做（§5 防数据窥探）；
4. 产出建议阈值替换 `_BEAT_WEAK_MAX` / `_BEAT_MEDIUM_MAX`（如研究显示 5%/15% 应改为
   8%/20%）。

### 3.7 分层与迁移性检验

按以下维度分层估计 LR 并做异质性检验（分层内 n ≥ 50 才报告）：

- 年份（检验基率漂移：牛熊市 beat 的诊断力是否稳定）；
- 总市值三分位（小票 beat 是否噪声更大）；
- 申万 L1 行业（若分层 LR 差异大 → 记录为后续"条件化证据强度"的设计输入，本批次不回填）。

---

## 4. 回填引擎的两条路线

### 4.1 路线 1：重标定三档 d（最小改动）

- 用桶中位 LR 反解 `d = (LR−1)/(LR+1)`，取最接近的离散档替换 `STRENGTH_DELTA`，
  并同步替换 `_BEAT_WEAK_MAX` / `_BEAT_MEDIUM_MAX` 为分箱边界。
- 优点：零契约改动、零下游影响；缺点：连续经验 LR 被压缩成三档，信息损失明显，
  且 `(1±d)/(1∓d)` 的对称参数化未必贴合经验 LR 的形状（beat 与 miss 的 LR 曲线
  可能不对称）。

### 4.2 路线 2：经验 LR 直通（**推荐**）

- `EvidenceEvent` 新增可选字段 `log_odds: float | None = None`（经验 log-LR，由标定表
  按 (direction, deviation bucket) 查得；`None` 回退现有 `(1±d)/(1∓d)` 路径）；
- `_event_logodds` 优先消费 `log_odds`；
- 标定表 `alphabee/midterm/calibration/beat_miss_lr.yaml`：bucket 边界 + LR + 95% CI +
  `calibrated_at` + `sample_n` + 数据切分说明 + 生成脚本 commit hash；加载失败/缺失时
  回退模块常量（现有 5%/15% 与三档 d 成为**回退值**而非标定值）；
- `evidence_rules._make_event` 在产事件时查表填 `log_odds`；
- 反证 1.2× 与钳位边界不动（属于后续校准批次），但报告给出一致性检验（见 §6）。

> 决策：默认按路线 2 实施；若 P3 时发现下游有任何契约兼容风险，降级到路线 1 并记录。

### 4.3 回填纪律

- 参数文件版本化（含 `calibrated_at`、拟合/验证切分日期、脚本 commit hash）；
- 模块常量注释改为「标定表缺失时的回退值（结构性示意）」，并注明标定表路径；
- 回填 PR 必须同时更新：标定表加载单测、回退路径单测、经验 LR 生效的 bayes 单测。

---

## 5. 统计陷阱与对策（重点）

| # | 陷阱 | 对策 |
|---|---|---|
| 1 | 幸存者偏差（只用现存股票，beat 组好看） | `stock_basic` 全 list_status（含 D/P）；退市股结局按极端负收益处理并敏感性对照 |
| 2 | 前视偏差 | 结局只用 `ann_date` 后行情；防前视断言；窗口未走完的事件排除 |
| 3 | 重叠样本（相邻报告期前向窗口重叠） | 块 bootstrap CI + 滚动窗口下采样估计并列报告 |
| 4 | 基率漂移（牛熊市跑赢概率不同） | 按年份分层报告；主 LR 用全样本、分层表供条件化判断 |
| 5 | 数据窥探（边看边调桶） | 时间切分（拟合 ≤2023、验证 2024+）；分箱搜索只在拟合集；主结论以验证集为准 |
| 6 | deviation 右偏长尾 | ±100pp winsorize 敏感性；极端值单列 |
| 7 | 小样本桶 | 合并 + Dirichlet 平滑 + CI；n<30 不进标定表 |
| 8 | 研究口径 vs 生产口径漂移 | raw 字段与 canonical 映射对账（§2.1）；事件单位与 A1 压缩口径一致 |
| 9 | 公告日不可交易 | 起算日取 `ann_date` 后首个交易日；盘后公告不产生当日信息优势 |
| 10 | 行业/市值混杂（beat 公司与小票成长股重叠） | §3.7 分层；若混杂显著，主表同时给市值分位子表 |

---

## 6. 验证与验收标准

**研究侧**（P1/P2 交付物）：

- 数据集可复现：脚本 + 固定 seed + 数据版本快照（拉取日期/接口/字段清单）；
- `calibration_report.md` 必含：LR 主表（含 CI）、inline 桶检验、基率分层表、分箱对比
  （现有 5%/15% vs 数据驱动边界）、AUC 对比、重叠稳健性两口径、局限声明（§1.4 语义差
  置于报告首页）；
- **放行门槛**：验证集上经验 LR 的区分度（AUC）不低于现有三档标定，且 beat/miss 方向
  与幅度呈**单调**趋势；不满足则只出报告、不回填。

**回填侧**（P3 交付物）：

- 标定表缺失时引擎行为与现状完全一致（回退测试）；
- 标定表生效时：`_event_logodds` 使用经验 log-odds；单测覆盖 beat/miss/inline/缺失
  四类事件；
- 现有 `tests/midterm` + `tests/orchestrator`（479）全量通过；
- `evidence_rules.py` 常量注释完成溯源改造（回退值 + 标定表路径）。

---

## 7. 落地分期

| 期 | 内容 | 预估 | 产出 |
|---|---|---|---|
| **P0** | 数据冒烟：接口契约/历史深度/积分/缺失率（§2.4） | 1 天 | `接口核实` 结论 + 数据可行性判定 |
| **P1** | 最小闭环：单基准（中证全指）×126 交易日，初始桶对照，第一版 LR 表 + CI | 2–3 天 | `events/returns.parquet` + LR 主表草稿 |
| **P2** | 稳健性：分层/敏感性/时间切分/分箱搜索/重叠下采样 | 2 天 | `calibration_report.md` + 建议阈值 |
| **P3** | 回填：路线 2（经验 LR 直通）+ 单测 + 常量溯源 | 1 天 | 标定表 + 代码回填 PR |
| **P4** | 例行化：接入 `RULE_MARKET_ADAPTATION_DESIGN.md` D1 `BacktestHarness` 协议，季/年频再校准；revision 与先验基率标定另立批次 | 后续 | 持续校准闭环 |

> P0 失败处理：若积分/历史深度不满足（如 `forecast` 历史 < 5 年），降级方案为
> 只用 `express` 符号方向路径 + 预告区间缺失时用 `last_parent_net` 反推区间（需先
> 验证反推口径偏差），或改 AkShare 业绩预告接口交叉验证（同样先冒烟）。

---

## 附：关键设计决策速查

| 决策点 | 结论 | 备注 |
|---|---|---|
| 结局 Y | 未来 126 交易日相对中证全指超额收益 > 0 | 敏感性 63/252 日、沪深300/申万 L1 |
| 证据单位 | (ts_code, end_date) 一条事件 | 与 A1 压缩口径一致 |
| 主统计量 | LR = P(E\|Y=1)/P(E\|Y=0)，Dirichlet 平滑 + 块 bootstrap CI | 贝叶斯翻转口径互验 |
| 桶边界 | 数据驱动单调分箱，现有 {±5, ±15} 作对照 | 搜索只在拟合集 |
| 切分 | 拟合 ≤2023-12-31，验证 2024-01-01 起 | 防数据窥探 |
| inline 桶 | 必须估计并检验 LR≈1 | 若偏离则另出设计变更 |
| 回填路线 | 路线 2：`EvidenceEvent.log_odds` 直通 + 标定表 | 失败降级路线 1 |
| 标定表位置 | `alphabee/midterm/calibration/beat_miss_lr.yaml` | 缺失回退现有常量 |
| 先验基率 | 本设计只输出分层基率统计，回填另立文档 | 两侧语义对齐到 Y |
| 反证 1.2× / 钳位 | 本批次不动 | 后续批次校准 |
