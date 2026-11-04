# 配置说明

从 `config.yaml.example` 复制为 `config.yaml`，支持 `${ENV_VAR}` 和 `${ENV_VAR:default}` 占位符；`.env` 中的变量会在启动时自动加载：

```yaml
llm:
  api_key: "${LLM_API_KEY}"
  base_url: "${LLM_BASE_URL:https://api.deepseek.com}"
  model: "${LLM_MODEL:deepseek-chat}"

web_search:
  tavily:
    api_key: "${TAVILY_API_KEY:}"
  ddgs:
    region: "cn-zh"

tushare:
  api_key: "${TUSHARE_TOKEN:}"

data:
  root_dir: "${DATA_ROOT:data}"
```

## 环境变量

| 环境变量 | 说明 | 必须 |
|----------|------|------|
| `LLM_API_KEY` | 大模型 API 密钥 | ✅ |
| `LLM_BASE_URL` | 大模型 API 地址（默认 `https://api.deepseek.com`） | 可选 |
| `LLM_MODEL` | 模型名称（默认 `deepseek-chat`） | 可选 |
| `TUSHARE_TOKEN` | Tushare 数据 Token | 建议填写 |
| `TAVILY_API_KEY` | Tavily 搜索 API 密钥 | 可选 |
| `LANGFUSE_ENABLE` | 是否启用 Langfuse 追踪（默认 `false`） | 可选 |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Langfuse 可观测性 | 可选 |
| `LANGFUSE_BASE_URL` | Langfuse 服务地址（默认 `http://localhost:3000`） | 可选 |
| `DATA_ROOT` | 产物根目录（默认 `data/`） | 可选 |

## 中期分析（midterm）

`midterm` 段承载中期决策层的分类器顺序、状态证据公式、F 边际改善、F 口径治理四键与一致预期（consensus）数据源。**缺段 ⇒ 各键都取内置默认值**，旧 `config.yaml` 仍可 import 且行为与改造前一致：

```yaml
midterm:
  classifier_order: etf              # etf = E→T→F（基准）；tef = T→E→F（价格先行变体）
  f_marginal_mode: shadow            # off = 不算；shadow = 算但不进 F（缺省）；on = 进 F
  ocf_penalty_basis: ttm             # ttm = 滚动 12 个月（缺省）；sign = 累计符号（回滚）
  f_recurring_mode: shadow           # off = 只用归母；shadow = 算扣非但不进 F（缺省）；on = 进 F
  segment_bonus_mode: period_aligned # period_aligned = 期间对齐 + 占比门槛（缺省）；legacy = 旧口径
  persist_financial_inputs: false    # 是否落盘 F 输入序列 PIT 快照（缺省 false，普通 run 零 I/O）
  consensus_source: report_list      # report_list（默认） | f10_forecast | auto
```

- `classifier_order`：`etf`（默认，E→T→F）或 `tef`（T→E→F 价格先行变体）；缺段 / 非法值 / 读配置异常一律 fail-open 回落 `etf`（`alphabee/midterm/classifier_dispatch.py`，行为变更登记见 `docs/roadmap/ROADMAP.md`）。
- `f_marginal_mode`：F 因子「边际改善」三档（`off` / `shadow` / `on`），缺省 `shadow`；语义与 fail-open 落点见下节。
- `ocf_penalty_basis` / `f_recurring_mode` / `segment_bonus_mode` / `persist_financial_inputs`：F 数据契约与口径治理的四个开关，语义、默认值与回滚见「F 口径治理四键」。
- `consensus_source`：一致预期数据源的 provider 链（`alphabee/collectors/consensus/eastmoney.py::build_consensus`），取值与语义如下。

### `midterm.f_marginal_mode`

| 取值 | 语义 | `score_breakdown` |
|------|------|-------------------|
| `off` | 不算边际项（严格复现旧口径，故障隔离用） | 不含边际键 |
| `shadow`（**默认**） | 算 `M/Q/F_shadow` 但不进 F（影子期观测；F 与改造前逐值相同） | 含 6 个边际键（仅单季数据可用时；降级帧不含） |
| `on` | 边际项参与 F（`F_growth = 0.70·L + 0.30·M`；设计 §7.1 G-8 语料门槛未达标，**本轮不开启**） | 含 6 个边际键（仅单季数据可用时；降级帧不含） |

- **fail-open 落点**：配置缺段 / 缺该字段 ⇒ 取 `MidtermClassifierSettings` 的默认值（当前 `shadow`）；读配置异常 / 取值非法 ⇒ logger warning + 回落 `off`（最保守：连派生都不做，绝不抛）。
- **显式传参优先**：`decision_model.evaluate(..., f_marginal_mode=...)` / `get_decision(...)` 覆盖配置，与 `classifier_formula` 同构；显式值非法同样 warning + 回落 `off`。
- **回滚**：把本键改为 `off`（一行配置）；影子观测工具与代码路径保留。观测指标（S1–S4）与开权门槛（G-8）见 `docs/midterm/MIDTERM_DECISION_MODEL.md` §3.4，变更登记见 `docs/roadmap/ROADMAP.md`「行为变更登记」。

### `midterm.consensus_source`

| 取值 | 语义 | `ConsensusOutput.source` |
|------|------|--------------------------|
| `report_list`（**默认**） | 东财**研报列表**聚合（改造前的唯一路径，逐字不变） | `eastmoney:report/list` |
| `f10_forecast` | 东财 F10「盈利预测」页接口 + 快照序列自算 revision；不可用则**回落** `report_list` | `eastmoney:f10/profit_forecast`（回落时指向实际来源） |
| `auto` | 优先 `f10_forecast`，不可用回落 `report_list`（与 `f10_forecast` 同一条回落路径） | 同上 |

- **失败回落**：`f10_forecast` / `auto` 下新源不可用（网络失败 / 非法代码 / 无一致预期覆盖）⇒ 回落 `report_list`，并把固定文案写入 `ConsensusOutput.warnings` 留痕；`source` 诚实指向实际使用的来源。
- **非法值 / 配置异常**：非法取值 ⇒ logger warning + 同一条 warning 文本 + 回落 `report_list`（**绝不抛**）；配置段缺失或读配置异常 ⇒ 直接 `report_list`（不产生附加 warning）。
- **读取时机**：在**每次 `build_consensus` 调用时**读取配置（不在 import 期读取），改配置后下一次调用即生效，无需重启。
- **显式传参优先**：调用方可用 `build_consensus(..., consensus_source=...)` 覆盖配置；`snapshot_dir` 可覆盖快照落盘目录（默认 `<data_root>/consensus_snapshots/`）。
- **切换到新源后的语义变化（登记条款，勿混用历史帧）**：`coverage_count` 由「研报数」变「**机构家数**」（`yctj_list[].EPS_COUNT`），两种口径的帧**不可直接混比**（对比实验只在新源启用后的帧内进行）；`target_price` 在 f10 路径**恒 `None` + `consensus_missing`**（该接口无目标价字段）；`rating_mean` / `rating_upgrade_1m` / `rating_downgrade_1m` / `revision_breadth` 在 f10 路径**恒 `None`**（评级为 5 分制且无上期评级，与研报 3 分制不可比）；`eps_fy1_revision_1m/3m` 等 revision 由快照序列自算（1m/3m 窗口 30/90 天，`revision_acceleration` 需 ≥2 个可算点），**快照不足 ⇒ `None`**（不编造）；`as_of_date` 取服务端**数据刷新日**（`jgyc` 聚合行的 `PUBLISH_DATE`），不是研报日。接口契约与实测字段映射见 `docs/midterm/DATA_CONTRACTS_VERIFIED.md`。

### F 口径治理四键

> 设计依据：`docs/implementation/midterm/MIDTERM_F_DATA_GOVERNANCE_DESIGN.md` §8/§9/§12/§14；行为变更登记见 `docs/roadmap/ROADMAP.md`「行为变更登记」的 F 数据治理 PR-5/PR-6/PR-7/PR-9 行。

| 键 | 取值 | 默认 | 语义 | fail-open | 回滚 |
|---|---|---|---|---|---|
| `ocf_penalty_basis` | `ttm` / `sign` | 生产 `ttm`，`score_engine` 函数 `sign` | `ttm`：经营现金流惩罚用滚动 12 个月 OCF（`TTM_OCF(t) = FY(prev) + cum(t) − cum(t−4)`，年报 `TTM_OCF(t) = cum(t)`）；金融股（`company_type ∈ {2,3,4}`）在 ttm 口径下跳过；TTM 缺失回落累计符号并记 `score_breakdown.ocf_penalty_basis=0`。`sign`：旧口径，按报告期累计 OCF 符号判定（金融股不跳过） | 配置缺段/缺字段 ⇒ 模型默认 `ttm`；读配置异常/取值非法 ⇒ warn + `sign`（最保守，回滚旧行为） | `midterm.ocf_penalty_basis: sign` |
| `f_recurring_mode` | `off` / `shadow` / `on` | 生产 `shadow`，`score_engine` 函数 `off` | `shadow`：计算 `np_term_recurring`（`sat(min(归母_yoy, 扣非_yoy), 30)`）、`recurring_gap`、`f_recurring_shadow` 但不进 F；`on`：净利项改用扣非锚（`min` **只压不抬**）；扣非同比缺失或上年同期扣非 ≤0 ⇒ 回落归母并记 `recurring_unavailable=1`；`off`：只用归母（旧行为） | 配置缺段/缺字段 ⇒ 模型默认 `shadow`；读配置异常/取值非法 ⇒ warn + `off`（最保守，F 不变） | `midterm.f_recurring_mode: off` |
| `segment_bonus_mode` | `period_aligned` / `legacy` | 生产 `period_aligned`，`score_engine` 函数 `legacy` | `period_aligned`（**评分层**）：分部报告期与财报报告期**均可知且不同** ⇒ 不产 bonus；最快分部收入占比已知且 <10%（`_SEGMENT_MIN_SHARE`）⇒ 不产；任一期间缺失 ⇒ fail-open 按旧行为。`legacy`：只看 divergence >15pp（旧评分口径）。**注**：按产品分类 + 10% 占比的分部**提取**在数据层 `_segment_extremes_v2` 中始终生效（`legacy` 只回滚评分层两个门槛，不回滚数据层提取） | 配置缺段/缺字段 ⇒ 模型默认 `period_aligned`；读配置异常/取值非法 ⇒ warn + `legacy`（最保守，回滚旧评分行为） | `midterm.segment_bonus_mode: legacy` |
| `persist_financial_inputs` | `true` / `false` | `false` | `true` 时 `get_factor_snapshot` 在 F 构建后把 F 的 6 条输入序列（每条 ≤8 期）以 append-only JSONL 追加到 `data/midterm/financial_inputs/<symbol>.jsonl`，按 `(symbol, as_of_date, report_period, content_hash)` 幂等；写盘失败 fail-open（只 warning，不影响快照/决策）；`false` 时普通 run 零 I/O，`build_fundamental_factor` 始终为纯函数 | 配置缺段/缺字段 ⇒ 模型默认 `false`；读配置异常/取值非 bool ⇒ `false`（绝不误写盘） | `midterm.persist_financial_inputs: false`（或删除本键） |

**纯函数默认 vs 生产默认（不要混用）**：`score_engine` 的 `fundamental_breakdown(...)` / `compress_scores(...)` 保持旧行为（函数缺省 `sign` / `off` / `legacy` / `marginal_mode=off`），只有 `decision_model.evaluate` / `get_decision` 解析配置后显式传入生产默认（`ttm` / `shadow` / `period_aligned` / `shadow`）；`persist_financial_inputs` 的解析缺省与生产缺省同为 `false`。任何直接调用 `score_engine` 的代码不得依赖生产默认。

| 键 | `score_engine` 函数默认 | 生产默认（config / `decision_model`） |
|---|---|---|
| `ocf_penalty_basis` | `sign`（旧行为） | `ttm` |
| `f_recurring_mode` | `off`（旧行为） | `shadow` |
| `segment_bonus_mode` | `legacy`（旧行为） | `period_aligned` |
| `f_marginal_mode` | `off`（旧行为） | `shadow` |
| `persist_financial_inputs` | `false`（模块解析缺省） | `false` |

## 偏离控制框架（deviation）

`deviation.tracking` 段承载宏观环跟踪（tracking）的阈值与开关（P0 设计 §7.1）。缺段时全部取内置默认值，旧配置仍可 import：

```yaml
deviation:
  tracking:
    price_move_mode: absolute   # 行情异动阈值口径（absolute | limit_relative）
    price_move_ratio: 0.5       # limit_relative 触发比例（|change|/board_limit ≥ 0.5）
```

- `price_move_mode: absolute`（默认）= 现状绝对值口径：`|price_change_pct| > 5.0%` 触发。
- `price_move_mode: limit_relative` = 按板块涨跌停幅度归一：`|change|/board_limit ≥ price_move_ratio`
  即触发，且封板边界 `|change| ≥ board_limit − 0.5pp` 也触发（覆盖 ST ≈4.95% 与涨停 9.99% 漏判）。
  `price_move_ratio: 0.5` ⇒ 主板 5%、双创 10%、北交所 15%，与主板绝对口径 `5.0%` 一致。

`main.py` 会自动加载项目根目录下的 `.env`，配置文件则从 `config.yaml` 读取。
