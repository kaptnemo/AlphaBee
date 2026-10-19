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

`midterm` 段承载中期决策层的分类器顺序与一致预期（consensus）数据源。**缺段 ⇒ 两个键都取内置默认值**，旧 `config.yaml` 仍可 import 且行为与改造前一致：

```yaml
midterm:
  classifier_order: etf              # etf = E→T→F（基准）；tef = T→E→F（价格先行变体）
  consensus_source: report_list      # report_list（默认） | f10_forecast | auto
```

- `classifier_order`：`etf`（默认，E→T→F）或 `tef`（T→E→F 价格先行变体）；缺段 / 非法值 / 读配置异常一律 fail-open 回落 `etf`（`alphabee/midterm/classifier_dispatch.py`，行为变更登记见 `docs/roadmap/ROADMAP.md`）。
- `consensus_source`：一致预期数据源的 provider 链（`alphabee/collectors/consensus/eastmoney.py::build_consensus`），取值与语义如下。

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
