# Phase 1 细化设计：行业知识工作流基础设施

> 所属方案：`docs/industry-context-injection-plan.md` Phase 1（行业知识工作流基础设施，后置）。
> 本文档把主计划里 Phase 1 的 5 条粗粒度任务展开为可落地的设计，并在动手前与当前代码逐项核对。
> 设计原则沿用主计划：数值基准层、canonical 字段单一命名空间、外部字段只在 adapter/采集层、降级显式留痕。

## 1. 目标与边界

Phase 1 只做一件事：**把"行业知识"变成可版本化、可复用、可审计的离线知识资产**——即行业研究工作流
（采集 → 归一化 → 基准 → 定性 → 审核 → 持久化）及其配套的基础设施（完整 artifact 契约、JSON
快照存储、过期/置信度元数据、CLI 入口）。

| 属于 Phase 1 | 不属于 Phase 1（留待后续） |
|---|---|
| 完整 `IndustryContextArtifact` 契约（v2） | 在线 `resolve_industry_context` 切换数据来源（Phase 3） |
| 离线工作流 `IndustryContextWorkflow` + 6 节点 | 引擎 `industry_thresholds` / `industry_trigger_rules`（Phase 4） |
| JSON 快照持久化（原子写、过期计算、列表/读取） | 冲突/验证/观点层消费行业上下文（Phase 5） |
| stale / confidence / source_refs / review_status 元数据 | 报告层行业字段与 review gate（Phase 6） |
| 定性字段的**轻量**合成通道（默认关闭） | 行业名规范字典与三处硬编码收敛（Phase 2） |

## 2. 现状核对（2026-08，与代码逐项对齐）

Phase 0 已落地的部分（作为本次设计的输入，不再重复建设）：

- `alphabee/industry/benchmarks.py`：`derive_benchmarks()` 中位数推导、`IndustryBenchmarks.to_fact_values()`。
- `alphabee/industry/data.py`：`fetch_peer_financials()`（Tushare index_member + fina_indicator，best-effort）。
- `alphabee/orchestrator/nodes/resolve_industry_context.py`：在线节点，已插入主图。
- `ArtifactType.INDUSTRY_CONTEXT`（`alphabee/core/schemas.py`）+ `IndustryContextArtifact` v1
  （`alphabee/orchestrator/contracts.py`，扁平 `benchmarks` 字典）。
- 引擎阈值回退链：`registry.py` 的 `threshold_context = {**fact_values, "value": ...}` 已支持相对基准。

### 2.1 核对中发现的一个 Phase 0 潜在缺陷：单位口径错配

逐条核对规则后确认：**Phase 0 的 `data.py::_canonical_record` 把 Tushare 的百分比原值（如
`roe=15.23`、`debt_to_assets=45.6`）直接当作 canonical 值注入 `fact_values`，而规则侧的单位与之
不一致**：

| canonical 字段 | schema 单位 | 规则侧使用方式 | Phase 0 注入值 | 结论 |
|---|---|---|---|---|
| `industry_avg_roe` | RATIO | `roe_level`: `value(≈0.15) >= industry_avg_roe * 1.5` | 15.2（百分比） | **错配**：相对阈值恒不命中，静默回退绝对阈值 |
| `industry_avg_debt_ratio` | RATIO | `debt_ratio`: `value(≈0.45) < industry_avg_debt_ratio * 0.8` | 45.6（百分比） | **错配**：恒命中 conservative，相对逻辑失效 |
| `industry_avg_gross_margin` | RATIO | 预留 | 32.1（百分比） | 错配（当前无消费规则） |
| `industry_revenue_yoy` | PERCENT | `market_share_change`: `revenue_yoy - industry_revenue_yoy`，阈值 ±5（百分点） | 12.3（百分比） | **一致**（公司侧 `revenue_yoy` 也是 PERCENT） |

这正是主计划 B3「口径风险」的一个具体实例：**单位不齐，直接相减/相乘产生错配**。Phase 1 的
`normalize` 节点必须把单位转换做进去（这是 Phase 1 修复 Phase 0 的一部分，属于正常演进，不改变
Phase 0 对外行为语义——规则本来就在按 RATIO 解释这些字段）。

> 注意：Phase 0 的单测没有暴露此问题，因为测试直接喂 ratio 值（`0.12`）给 `derive_benchmarks`，
> bug 只在 `data.py` 的取数转换层。Phase 1 会为转换层补单位断言测试。

### 2.2 设计输入约束

- Python 3.13（可用 PEP 695 泛型语法）。
- 离线流程形态参考 `alphabee/market_regime/`（`data.py` 编排 + `persistence.py` CSV 存储），
  不引入 LangGraph（离线批处理不需要流式/检查点）。
- LLM 实例一律经 `alphabee.utils.create_chat_model(component)` 创建，组件名新增 `agent.industry_research`。
- 数据根目录经 `alphabee.utils.storage.get_data_root()` 解析（尊重 `config.yaml` 的 `data.root_dir`）。

## 3. 关键设计决策

### D1 artifact 契约升级为 v2：三组基准字典 + 扁平 canonical 键

主计划 1.3 的契约把基准拆成 `valuation_benchmarks` / `financial_benchmarks` / `growth_benchmarks`
三个字典。但主计划示例里的键名带 `_median` 后缀（`industry_pe_ttm_median`），会与 schema
（`alphabee/schemas/industry.yaml`）和 `fact_values` 的既有 canonical 键（`industry_pe_ttm`、
`industry_avg_roe`…）形成**第二套命名宇宙**。决定：

- **三组字典的结构照主计划**（可读、可分类、便于按类别计算过期），
- **键名一律用既有 canonical 字段名，不带 `_median` 后缀**（`industry_pe_ttm` / `industry_pb` /
  `industry_avg_roe` / `industry_avg_debt_ratio` / `industry_avg_gross_margin` / `industry_revenue_yoy`），
  与 `fact_values` 注入完全同构——单一命名空间，防漂移。

```python
class IndustryContextArtifact(BaseModel):
    schema_version: str = "2"
    industry: str = ""                    # 展示名
    sub_industry: str = ""
    classification_standard: str = ""     # sw_l1 / sw_l2 / ths / custom
    industry_code: str = ""               # 匹配键的行业代码（sw_l1 → "801120.SI"）
    sw_code: str | None = None            # 申万源代码（sw 场景下与 industry_code 相同）
    as_of_date: str = ""                  # 数据截止日（YYYY-MM-DD）
    generated_at: str = ""                # 生成时间（ISO8601）
    stale_after: str | None = None        # 过期日（按类别最早到期，见 D6）
    source_refs: list[str] = []
    confidence: float | None = None       # 0-1，审核节点启发式计算

    # 定性（v1 保持空或轻量，见 DOMAIN_CONTEXT_ROADMAP 划界）
    lifecycle_stage: str | None = None
    business_model_summary: str | None = None
    industry_chain: dict[str, list[str]] = {}
    key_drivers: list[str] = []
    risk_factors: list[str] = []

    # 数值基准（canonical 键，按类别分组；None = 该基准不可得）
    valuation_benchmarks: dict[str, float | None] = {}
    financial_benchmarks: dict[str, float | None] = {}
    growth_benchmarks: dict[str, float | None] = {}
    peer_universe: list[str] = []         # 实际参与推导的成分股代码（可复现性）
    peer_count: int | None = None

    # 审核与降级
    review_status: str | None = None      # approved / needs_review / rejected
    review_notes: list[str] = []
    degraded: bool = False
    degraded_reason: str = ""
    stale: bool = False                   # B2：离线产物初始 False，在线读取过期版本时置 True
```

配套工具（放 `benchmarks.py`，见 D4）：

- `BENCHMARK_CATEGORIES: dict[str, str]`——canonical 键 → 类别（valuation/financial/growth）。
- `group_benchmarks(flat)` / `flatten_benchmarks(v, f, g)`——扁平 ↔ 分组双向转换。
- `IndustryContextArtifact.benchmark_fact_values()`——把三组字典展平成 `fact_values` 形状
  （丢弃 None），在线节点/引擎注入直接复用。

**对 Phase 0 的影响**：v1 的扁平 `benchmarks` 字段被三组字典取代；`resolve_industry_context` 节点
构造 artifact 的方式同步改为分组写入（机械改动），Phase 0 的节点测试断言同步更新。由于 Phase 0
从未落盘，**不存在需要迁移的历史快照**。

### D2 单位与口径契约（B3 落地）

`normalize_industry_schema` 节点对采集到的原始记录做**强制单位转换**，规则如下（与 schema 单位、
规则公式逐一核对）：

| canonical 键 | 目标单位 | 转换（Tushare fina_indicator） |
|---|---|---|
| `industry_revenue_yoy` | PERCENT（百分点） | `tr_yoy` 原样（已是 %） |
| `industry_avg_roe` | RATIO | `roe` ÷ 100 |
| `industry_avg_debt_ratio` | RATIO | `debt_to_assets` ÷ 100 |
| `industry_avg_gross_margin` | RATIO | `grossprofit_margin` ÷ 100 |

**报告期对齐（B3 的周期部分）**：normalize 在每条记录上保留 `end_date` / `ann_date`（报告期）；
`assess_period_alignment(records)` 输出对齐状态：

- `aligned`：所有记录同一 `end_date`；
- `mostly_aligned`：单一主导期覆盖 ≥80%；
- `mixed`：多个报告期混杂。

审核节点按主计划严格执行：**`mixed` 时 `growth_benchmarks["industry_revenue_yoy"]` 置空**
（下游 `market_share_change` 回到 blocked，而不是产出口径错配的数值），并在 `review_notes` 写明
原因；`mostly_aligned` 保留数值但记录 note。估值/财务类基准不受报告期混杂影响（快照/比率类指标
口径稳定）。

### D3 工作流形态：确定性顺序流水线 + typed state（非 LangGraph）

`IndustryContextWorkflow` 是**顺序执行 6 个节点函数**的批处理流水线，节点签名统一
`(state: IndustryWorkflowState) -> IndustryWorkflowState`，每个节点可独立单测。理由：

- 离线/准离线批处理不需要流式事件、检查点或条件路由；
- 与 `market_regime` 的既有离线模式一致（`data.py` 编排 + `persistence.py` 存储）；
- 6 个节点里只有 synthesize/review 两个"可选 LLM"节点，LLM 失败可降级，不需要 agent 化。

主计划把第 3 项写成 "`IndustryResearchAgent` / `IndustryContextWorkflow`"，这里明确：**Phase 1 以
确定性流水线为主体**，"agent"形态（deepagents 工具型研究代理）留到需要开放式采集（政策/新闻/产业链）
时再引入——当前节点全部有结构化数据源，agent 化无收益。

```python
@dataclass
class IndustryWorkflowState:
    target: IndustryTarget
    raw_facts: dict[str, Any] = field(default_factory=dict)
    canonical_records: list[dict] = field(default_factory=list)
    period_alignment: PeriodAlignment | None = None
    benchmarks: IndustryBenchmarks | None = None
    qualitative: IndustryQualitative = field(default_factory=IndustryQualitative)
    review: IndustryReview = field(default_factory=IndustryReview)
    artifact: IndustryContextArtifact | None = None
    errors: list[str] = field(default_factory=list)
    degraded: bool = False
    degraded_reason: str = ""
```

### D4 节点契约（6 节点）

| 节点 | 输入（state 中） | 产出（写回 state） | 降级行为 |
|---|---|---|---|
| `collect_industry_facts` | target | `raw_facts`（identity/valuation/peers 三块，各带 source 与 error） | 任一块失败记录 error，不中断 |
| `normalize_industry_schema` | raw_facts.peers.records | `canonical_records`（单位转换 + end_date）+ `period_alignment` | 无成分股 → 空列表 |
| `derive_industry_benchmarks` | canonical_records + valuation | `benchmarks: IndustryBenchmarks` | 复用 Phase 0 纯函数；无数据 → 全 None |
| `synthesize_industry_context` | benchmarks + raw_facts.identity | `qualitative`（默认空块） | LLM 失败 → 空块 + note（v1 默认关闭 LLM） |
| `review_industry_context` | 全部上文 | `review`（status/notes/confidence）+ stale_after | 纯确定性检查，恒成功 |
| `persist_industry_profile` | artifact | 写 JSON 快照；返回存储路径 | 写失败 → error + 不产出 artifact |

**collect 的三种输入形态**（CLI 暴露两种）：

1. `symbol`：`get_industry_fact(symbol)` → identity（industry / sw_code）+ 估值快照
   （`sw_daily` 最新一行的 `industry_pe_ttm` / `industry_pb` / `trade_date`）；
2. 直接目标 `standard + industry_code(+name)`：identity 直接给定；估值经
   `get_industry_daily(sw_code, name)`（`alphabee/providers/industry.py`）取最新一行；
3. （内部）两者都缺失 → collect 报错、整个 workflow 提前结束（degraded）。

成分股与 `peer_universe`：新增底层函数 `fetch_industry_peers(sw_code, limit)`，返回
`(records, peer_codes, error)`——`peer_codes` 即实际参与推导的成分股代码，写入 artifact 的
`peer_universe`，保证快照可复现（B4：v1 仅供报告「行业对比」章节引用，不做分位引擎）。

### D5 持久化：每行业一个 JSON 快照，原子写，latest-wins

接口（`alphabee/industry/persistence.py`）：

```python
class IndustryProfileStore:
    def __init__(self, root: Path | None = None): ...   # 默认 get_data_root()/industry_profiles
    def path_for(self, classification_standard: str, industry_code: str) -> Path
    def save(self, artifact: IndustryContextArtifact) -> Path      # 原子写：临时文件 + os.replace
    def load(self, classification_standard, industry_code, *, schema_version=None, as_of_date=None) -> IndustryContextArtifact | None
    def list_profiles(self) -> list[ProfileInfo]        # standard/code/as_of_date/stale 一览
    def is_stale(self, artifact, *, now=None) -> bool
```

- 文件布局：`data/industry_profiles/{classification_standard}/{industry_code}.json`
  （`industry_code` 经 `normalize_symbol` 防路径穿越，复用 `utils/storage.py`）。
- **latest-wins**：同一 (standard, code) 重复运行覆盖旧快照；版本/血缘靠 artifact 内的
  `schema_version` / `as_of_date` / `generated_at` / `source_refs` 字段 + git diff 审计
  （主计划 1.3 明示 v1 不做多版本历史，需要时再迁 SQLite）。
- `load` 的 `schema_version` / `as_of_date` 参数对单快照做过滤匹配（不匹配返回 None），
  保持主计划 1.4 "按 standard+industry+as_of_date+schema_version 存取"的接口形态。
- 原子写：`tempfile` 同目录临时文件 → `os.replace()`，崩溃不产生半截文件。
- 文件内容即 artifact 的 `model_dump(mode="json", exclude_none=False)`（pretty-printed，
  保证可 diff、可人工 review）。

### D6 过期：按基准类别给默认 `stale_after`，有效值取最早到期

```python
STALE_AFTER_DAYS = {"valuation": 30, "financial": 90, "growth": 90, "qualitative": 30}
```

- 审核节点根据 artifact **实际存在**的类别（三组字典非空 + 定性字段非空）计算建议过期日
  `suggest_stale_after(as_of_date, present_categories)`：取各类别日期中**最早**的一个
  （最易过期的类别决定整体过期点），写入 artifact.stale_after。
- `is_stale(artifact, now)`：`stale_after` 存在且 `now > stale_after` → True；无 `stale_after`
  时按 valuation 30 天兜底（防御性默认）。
- `stale` 字段（B2）离线产物恒为 False；在线读取过期版本时（Phase 3）由
  `resolve_industry_context` 置 True。本期在 `list_profiles` / CLI `--show` 中展示 stale 状态，
  供分析师人工触发更新（主计划 1.2 的"人工触发更新"通道）。

### D7 审核：确定性检查 + 可选 LLM，三态结论

`review_industry_context` 恒运行的确定性检查（不依赖 LLM，纯函数可测）：

1. **新鲜度**：`as_of_date` 距今超过类别默认 → note + `needs_review`；
2. **成分覆盖**：`peer_count < 5` → note（成分过少，基准代表性不足）；
3. **基准覆盖**：三组字典全空 → `degraded`（collect 阶段已标）+ note；
4. **口径对齐**（B3 严格版）：`period_alignment == "mixed"` → 置空 growth 基准 + note；
   `mostly_aligned` → 保留 + note；
5. **证据支撑**：qualitative 字段非空但 `source_refs` 无对应来源 → note（防"无源定性"）。

可选 LLM 复核（`qualitative_mode="llm"` 时）：给模型看基准摘要 + 定性块 + 来源，要求给出
evidence 支持度评价，追加进 `review_notes`；LLM 失败只降级为确定性结论，不阻断。

结论三态：`approved`（无 note 或仅提示级 note）/ `needs_review`（有实质 note）/
`rejected`（collect 完全失败或口径严重错配）。**持久化不因 rejected 而跳过**——产物照写、
状态留痕，由人工决定是否修复重跑（显式留痕原则）。

`confidence` 启发式：成分覆盖 + 无 degraded + 无需要 note → 0.8 起，按缺失逐项扣减
（如无估值 → -0.1，mixed 对齐 → -0.15，peer<5 → -0.2），下限 0.3。

### D8 定性合成边界：默认关闭，LLM 通道可选

与 `DOMAIN_CONTEXT_ROADMAP.md` 的划界（主计划 A2）：`business_model_summary` /
`industry_chain` / `key_drivers` / `risk_factors` 在 v1 **默认保持空**；仅当
`qualitative_mode="llm"` 时调用 `create_chat_model("agent.industry_research")` 生成轻量摘要
（prompt 强制要求引用来源、不超过若干条），失败即回退空块。`lifecycle_stage` 属确定性启发
（按成长/估值快照简单推断），也在默认路径产出。

### D9 CLI：`python -m alphabee.industry.cli`

```
# 按标的解析行业并生成快照（推荐）
python -m alphabee.industry.cli --symbol 600519.SH
# 直接指定行业
python -m alphabee.industry.cli --standard sw_l1 --code 801120.SI --name 白酒
# 可选开启定性合成（LLM）
python -m alphabee.industry.cli --symbol 600519.SH --qualitative llm
# 存储管理
python -m alphabee.industry.cli --list
python -m alphabee.industry.cli --show --standard sw_l1 --code 801120.SI
python -m alphabee.industry.cli --symbol 600519.SH --data-dir ./data
```

不并入 `main.py`（保持主 CLI 面向个股分析；行业资产管理走专用入口，Phase 3 在线注入切换数据源
时再在 `main.py` 增加观测/触发命令）。

### D10 与在线节点的关系

Phase 1 **不改** `resolve_industry_context` 的数据来源（仍在线实时推导，Phase 3 才切换为读存储）。
但 artifact 契约升级（D1）要求在线节点构造 v2 形状，因此本期同步改造：
`resolve_industry_context` 把扁平 `benchmarks` 改为三组字典（复用 `group_benchmarks`），
`benchmark_fact_values()` 保持 `fact_values` 注入不变（注入键本来就是扁平 canonical 键）。
这样 Phase 3 切换数据源时，在线/离线两个来源产出的 artifact 形状完全一致，零契约迁移。

## 4. 模块布局

```
alphabee/industry/
  __init__.py        # 公共 API 导出（Workflow / Artifact / Store / derive / fetch…）
  benchmarks.py      # [改] 增加 BENCHMARK_CATEGORIES、group_benchmarks、flatten_benchmarks
  data.py            # [改] 单位转换委托 normalize；新增 fetch_industry_peers（返回 peer_codes）
  contracts.py       # [新] IndustryTarget / IndustryContextArtifact(v2) / IndustryQualitative /
                     #      IndustryReview / PeriodAlignment / IndustryWorkflowState
  normalize.py       # [新] normalize_industry_records（单位+canonical）、assess_period_alignment
  persistence.py     # [新] IndustryProfileStore + suggest_stale_after / is_stale
  nodes.py           # [新] 6 个节点函数（collect/normalize/derive/synthesize/review/persist）
  workflow.py        # [新] IndustryContextWorkflow（顺序流水线）
  cli.py             # [新] 命令行入口

alphabee/orchestrator/contracts.py   # [改] 再导出 IndustryContextArtifact（来自 industry.contracts）
alphabee/orchestrator/nodes/resolve_industry_context.py  # [改] 构造 v2 形状
```

## 5. 数据流示例（`--symbol 600519.SH`）

```
target = IndustryTarget(symbol="600519.SH")
 └ collect: get_industry_fact → identity{白酒, sw_l1, 801120.SI} + sw_daily→PE/PB
            fetch_industry_peers(801120.SI) → records+peer_codes
 └ normalize: tr_yoy/roe/debt_to_assets/grossprofit_margin → 12.3 / 0.152 / 0.456 / 0.321（+end_date）
            assess_period_alignment → mostly_aligned
 └ derive: IndustryBenchmarks(industry=白酒, peer_count=20, revenue_yoy=12.3,
            avg_roe=0.143, avg_debt_ratio=0.455, avg_gross_margin=0.42, pe_ttm=25.0, pb=6.0)
 └ synthesize: lifecycle_stage=成熟期（启发）；定性块默认空
 └ review: 检查 5 项 → approved；confidence=0.8；stale_after=as_of_date+30d（估值最紧）
 └ persist: data/industry_profiles/sw_l1/801120.SI.json 原子写入
```

## 6. 测试计划

### 单元级（tests/industry/）

| 文件 | 覆盖 |
|---|---|
| `test_contracts.py` | v2 artifact 序列化往返；`benchmark_fact_values()` 丢弃 None；分组/展平互逆 |
| `test_normalize.py` | 单位转换（roe 15.2→0.152、tr_yoy 12.3 不变）；`assess_period_alignment` 三态 |
| `test_benchmarks.py`（扩） | `BENCHMARK_CATEGORIES` 覆盖全部注入键；`group_benchmarks` 分类正确 |
| `test_persistence.py` | 原子写/读往返；缺失 → None；`list_profiles`；`suggest_stale_after` 取最早；`is_stale` 边界 |
| `test_workflow.py` | 全流程（monkeypatch collect）→ artifact 落盘且字段正确；collect 失败 → degraded + rejected + 无 artifact；`mixed` 对齐 → growth 置空；qualitative 默认空、llm 模式降级为空 |

### 回归（tests/orchestrator/）

- `test_resolve_industry_context.py`：断言从 `benchmarks[...]` 改为
  `valuation_benchmarks / financial_benchmarks / growth_benchmarks[...]`，`fact_values` 注入断言不变。
- 全量 `pytest tests/agents/derived_facts/` 回归：确认单位修复后相对阈值行为符合预期
  （如制造业 ROE=6% vs 行业 5% → good 的端到端场景进入 `test_industry_thresholds.py` 的 golden 用例）。

## 7. 与后续 Phase 的衔接

- **Phase 2**（字段治理）：本设计已把匹配键定为 `classification_standard + industry_code`、
  键名全部 canonical，Phase 2 只需补名字典与三处硬编码迁移，不动本契约。
- **Phase 3**（在线注入层完善）：`resolve_industry_context` 改为 `IndustryProfileStore.load()`
  + stale 标记（`is_stale` 已就绪），artifact 形状零迁移。
- **Phase 4**（引擎行业感知）：引擎消费 `find_artifact_model(...)` 读取三组基准字典，
  `benchmark_fact_values()` 直接提供扁平注入。
- **Phase 5/6**：定性块与报告字段有了明确的落点（artifact 已预留字段），届时按 ROADMAP 0.4/0.6
  分层填充。
