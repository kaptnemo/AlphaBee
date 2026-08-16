# 公司赛道与对标组路线图（穿透申万标签）

> 问题：申万行业分类是给**数据统计和指数编制**用的，不是给**深度个股分析**用的。
> 用申万行业均值去给个股做行业相对判断，口径太粗糙——本路线图给出从「行业标签」
> 升级到「业务线解构 + 对标组基准」的实施方案，与既有数值基准层（industry-context）、
> 定性框架层（DOMAIN_CONTEXT_ROADMAP）构成三角分工。

## 1. 问题诊断：申万分类的四大硬伤（以工业富联 601138 为解剖样本）

| 硬伤维度 | 具体表现（工业富联） | 对当前管道的影响 |
| :--- | :--- | :--- |
| **1. 掩盖真正的增长引擎** | 申万二级丢进「通信设备」，但通信设备仅占营收约 1/3 且增速平缓；占约 2/3 且高增长的**云计算/AI 服务器**被分类无视 | 用「通信设备」行业营收增速去套它 → 严重低估成长性；`market_share_change` 等行业相对判断失真 |
| **2. 混淆商业模式** | 「通信设备」里既有品牌商（中兴通讯）也有代工厂（工业富联）；品牌商赚技术溢价，代工厂赚规模制造费，毛利率/研发逻辑/客户粘性完全不同 | 成分股中位数把两种商业模式混在一起 → ROE/负债率/毛利率基准失真 |
| **3. 无视产业链位置** | 同属「电子制造」，立讯精密在消费电子链（受手机出货量影响），工业富联在 AI 算力链（受英伟达/微软资本开支影响） | 行业 PE/PB 中位数对比对象错误 |
| **4. 更新严重滞后** | 分类静态，业务动态：工业富联 5 年前「通信+云」，现在「AI 服务器第一」，分类系统多年不调 | 行业标签与真实业务漂移，且漂移不可观测 |

**方法论转向（三步走）**：
1. **收入解构取代行业标签**：拆各业务线营收占比、增速、毛利率 → 真实赛道 = 占比最大 + 增速最快的业务线；
2. **商业模式定位取代粗分**：品牌/解决方案商、ODM/OEM 代工商、核心零部件商、软硬件集成商——赚的钱模式不同，分析维度不同；
3. **竞争格局与产业链映射取代行业整体估值**：对标组 = 公司真正所处产业链环节的**直接竞对**（工业富联 → 广达/纬创/英业达/华勤技术），而非申万全行业。

## 2. 设计原则与文档划界

1. **可计算优先**：本路线图只做「能落成数值/结构化标签」的部分——营收拆解数值、对标组基准、商业模式标签。定性叙事（产业链传导逻辑、驱动变量 playbook）归 `DOMAIN_CONTEXT_ROADMAP.md`，不重复建设。
2. **申万基线保留，公司赛道是"修正/覆盖"而非替代**：SW 行业均值仍是缺失数据时的回退基线；有公司赛道数据时，规则优先消费 `peer_*` 字段（自然构成 `peer → industry → absolute` 三级回退链，见 §6）。
3. **复用既有机制，零引擎改动优先**：
   - 缺失字段回退链（`registry.py` 表达式列表）：相对表达式缺失 → 抛异常 → 顺延下一级，天然三级回退；
   - artifact 契约约定：新产物走 `ArtifactType` + `find_artifact_model(...)`，不进 `OrchestratorState` 顶层；
   - 降级契约：缺失/过期 → 显式 issue 留痕（`company_track_missing` MEDIUM），不静默；
   - 外部字段只在 adapter/采集层：东方财富/同花顺/Tushare 列名不出现在下游。
4. **新鲜度与版本**：分项数据是半年度/年度口径，`as_of_date` 用**报告期**而非今天；对标组是**可版本化、可人工编辑**的资产。

### 2.1 与 `industry-context-injection-plan.md` 的关系（基线 vs 覆盖）

两者是**同一个「语境注入栈」的上下两层**，不是替代关系：

```
语境注入栈（可计算数值层）
├─ industry-context-injection-plan：申万行业基线
│   └─ 行业中位数基准（industry_*）+ 阈值机制 + 注入通道 + 离线行业知识工作流
└─ COMPANY_TRACK_ROADMAP（本文档）：公司级覆盖
    └─ 业务线解构 + 商业模式标签 + 对标组基准（peer_*）
定性叙事层：DOMAIN_CONTEXT_ROADMAP（primitives/playbooks）
```

**依赖方向（只依赖已完成部分）**：本路线图**依赖**行业计划的 Phase 0-1 机制——`fact_values` 注入通道、表达式列表回退链（`registry.py`）、降级契约（issue/stale 留痕）、artifact 契约约定、`alphabee/industry/{data,benchmarks,normalize}.py` 纯函数（`derive_benchmarks` 直接复用于对标组）。这些**已全部落地**。

**不依赖其剩余阶段**：Phase 2-6 未开始，且与本文档的排期关系为：
- Phase 2（字段治理/多来源 mapping）↔ 本文档 Phase A：**公共前置，并行实施**（`peer_*` 字段与 `biz_segment_*` 字段走同一套 schema 治理）；
- Phase 3（在线读行业存储）↔ 本文档 Phase D 注入：**各自独立**（行业存 JSON 快照、对标组存 `data/peer_groups/`，在线节点各读各的）；
- Phase 4（`industry_thresholds` 逐行业绝对带）↔ **可再缓**：相对基准已覆盖多数行业，`peer_*` 也不需要它，仅结构性行业（银行/地产）有增量价值；
- Phase 5/6（冲突/观点/报告层行业感知）↔ 本文档 Phase F：**合并实施**（同一批 prompt/报告 payload 改造点，同时支持 `industry` 与 `company_track` 字段，避免重复改两遍）。

## 3. 现状盘点（2026-08 与代码核对）

| 已有 | 位置 | 缺口 |
|---|---|---|
| 主营业务构成工具（分项收入/成本/利润，多报告期） | `agents/facts/tools/operation_fact.py`（Tushare `fina_mainbz`，已接入 FactCollectorAgent） | 无**分项占比 / 分项增速**（`fina_mainbz` 本身没有这两列） |
| operation 域 canonical 字段 | `schemas/operation.yaml`（biz_segment_name/revenue/cost/profit + 计算字段 biz_gross_margin） | 缺 `biz_segment_revenue_share`、`biz_segment_revenue_yoy` |
| Tushare operation adapter | `adapters/tushare/operation_mapping.yaml` | 无 akshare 侧（东财 `stock_zygc_em` 有占比/毛利率/增速，正好补齐） |
| 成分股取数与基准推导 | `alphabee/industry/{data,benchmarks,normalize}.py` | 取数是"按指数代码"，**不能按任意代码列表**取（对标组用不了） |
| 行业基准注入 | `orchestrator/nodes/resolve_industry_context.py` + `INDUSTRY_CONTEXT` artifact | 无 COMPANY_TRACK artifact、无 `peer_*` fact_values 键 |

## 4. 总体架构：CompanyTrack 层

```
离线（准离线，可周期/事件触发）
  collect_business_segments ── 东财主营构成 stock_zygc_em + Tushare fina_mainbz 交叉
  → normalize_segments      ── 单位/口径统一，canonical 分项记录（含占比、增速、报告期）
  → derive_track_label      ── 真实赛道 = 占比最大 + 增速最快的业务线（规则 + LLM 复核）
  → classify_business_model ── 商业模式 archetype 标签（四类分类器）
  → build_peer_group        ── 真对手清单（研报/业绩会 LLM 抽取 + 商业模式同类 + 人工白名单）
  → derive_peer_benchmarks  ── 对标组财务/估值基准（复用 derive_benchmarks，输入换成代码列表）
  → review_track            ── 新鲜度/覆盖/口径检查 + confidence + stale_after
  → persist_track           ── data/peer_groups/{symbol}.json + COMPANY_TRACK artifact 落盘

在线（个股分析，新增 resolve_company_track 节点，插在 resolve_industry_context 之后）
  1. 读最近非过期 CompanyTrack（无 → 回退申万基线 + company_track_missing issue）
  2. 完整 CompanyTrackArtifact 写 artifacts（ArtifactType.COMPANY_TRACK）
  3. 数值基准注入 fact_values：peer_* 键（覆盖优先级最高的相对判断）
```

### 4.1 数据模型草案

```python
class SegmentSnapshot(BaseModel):
    report_date: str                      # 报告期 YYYYMMDD
    segment_name: str                     # 业务分项（如"云计算/服务器"）
    category: str = ""                    # 按产品 / 按行业 / 按地区
    revenue: float | None = None          # 分项营收（元）
    revenue_share: float | None = None    # 收入占比（%，东财口径）
    revenue_yoy: float | None = None      # 分项同比增速（%，东财口径）
    gross_margin: float | None = None     # 分项毛利率（%）
    is_calculated: bool = False           # 是否由 fina_mainbz 推导（无 share/yoy）

class CompanyTrackArtifact(BaseModel):
    schema_version: str = "1"
    symbol: str = ""
    as_of_date: str = ""                  # 报告期
    generated_at: str = ""
    stale_after: str | None = None
    source_refs: list[str] = []

    segments: list[SegmentSnapshot] = []
    dominant_segment: str | None = None   # 占比最大业务线
    fastest_segment: str | None = None    # 增速最快业务线
    track_label: str = ""                 # 真实赛道标签（如"AI 算力基础设施 ODM"）
    override_basis: str = ""              # 标签依据（占比/增速数据 + LLM 复核记录）

    business_model: str = ""              # brand/odm/component/integrator/other
    business_model_evidence: str = ""

    peer_group: list[str] = []            # 对标组代码（可含境外：广达等）
    peer_group_source: str = ""           # 研报/业绩会/人工
    peer_benchmarks: dict[str, float | None] = {}   # canonical 键（peer_*）

    review_status: str | None = None
    review_notes: list[str] = []
    degraded: bool = False
    degraded_reason: str = ""
    stale: bool = False
```

### 4.2 新增 canonical 字段（schemas）

`operation.yaml` 补（东财 `stock_zygc_em` 来源，unit PERCENT）：

```yaml
biz_segment_revenue_share:  # 分项收入占比（%）
biz_segment_revenue_yoy:    # 分项收入同比增速（%）
```

`industry.yaml` 补对标组基准段（与 `industry_*` 同族，前缀 `peer_`，公司级）：

```yaml
peer_avg_roe:            # 对标组 ROE 中位数（RATIO）
peer_avg_debt_ratio:     # 对标组负债率中位数（RATIO）
peer_avg_gross_margin:   # 对标组毛利率中位数（RATIO）
peer_revenue_yoy:        # 对标组营收增速中位数（PERCENT，百分点）
peer_median_pe_ttm:      # 对标组 PE(TTM) 中位数（RATIO）
peer_median_pb:          # 对标组 PB 中位数（RATIO）
```

## 5. 分阶段实施

### Phase A：业务线数据基础（先补数据，一切的前提）

- [ ] A1 新增 akshare adapter：`adapters/akshare/operation_mapping.yaml`（`stock_zygc_em` → canonical：报告期/分类类型/主营构成/主营收入/收入比例/毛利率/同比增长率/成本/利润）；
- [ ] A2 `schemas/operation.yaml` 补 `biz_segment_revenue_share` / `biz_segment_revenue_yoy`；
- [ ] A3 新增 `alphabee/company_track/data.py`：`fetch_business_segments(symbol)` —— 东财主营构成（share/yoy/毛利率齐全）为主，Tushare `fina_mainbz` 兜底（无 share/yoy 时标 `is_calculated`，占比由 revenue 求和推导）；
- [ ] A4 `normalize.py`（公司赛道版）：报告期口径对齐（`latest_period` 唯一性检查）、单位统一（东财百分比已是 %，fina_mainbz 金额转占比需显式标注）、过滤"其他业务"低占比噪音（可选阈值）；
- [ ] A5 单测：adapter 列映射、单位/口径、多报告期选择、空数据降级。

### Phase B：真实赛道标签推导（收入解构 → 标签）

- [ ] B1 规则层 `derive_track_label(segments)`：真实赛道 = 收入占比 top1 且增速非负的业务线；占比与增速冲突时取**"占比 × 增速"加权得分最高**者（避免高增速低占比噪音）；
- [ ] B2 可选 LLM 复核（`agent.track`）：给规则输出 + 分项明细，输出结构化 `track_label` + `override_basis`（引用具体分项数据）；LLM 失败回退纯规则；
- [ ] B3 **override 机制**：`track_label` 与申万行业并存于 artifact（SW 是基线字段，track 是修正字段）；下游引用 track 时必须在报告注明"公司赛道标签（数据截至 X 报告期）"；
- [ ] B4 新鲜度：标签随**年报期**刷新（`as_of_date = 最新报告期`），跨期变化记录 `review_notes`（业务漂移可观测——直击硬伤 4）。

### Phase C：对标组构建（真对手清单，可版本化资产）

- [ ] C1 数据源：`peer_group` 来源优先级 = 券商研报/业绩会纪要 LLM 抽取（管理层点名的对标公司）> 商业模式同类（同 archetype + 分项结构相似度）> 人工维护白名单；
- [ ] C2 LLM 抽取（`agent.peer_group`）：输入 = 研报/业绩会文本片段 + 分项构成，输出 JSON 对标组（代码/名称/理由），需 `source_refs` 记录出处；失败/置信低 → 空对标组（降级，不编造）；
- [ ] C3 持久化：`data/peer_groups/{symbol}.json`（原子写，latest-wins，人工可编辑——分析师确认后覆盖 LLM 结果）；
- [ ] C4 校验：对标组代码合法化（A 股走 tushare，境外代码（广达 2382.TW 等）需标注 `exchange` 并单列，v1 对标组**优先支持 A 股**，境外仅存名单不进基准计算——避免跨市场口径错配）。

### Phase D：对标组基准（数值落地，规则消费）

- [ ] D1 `alphabee/industry/data.py` 泛化取数：新增 `fetch_peer_financials_for_codes(codes, limit)`（对显式代码列表跑 fina_indicator + daily_basic，复用现有 normalize/derive 纯函数）——**唯一引擎无关的机械改动**；
- [ ] D2 `derive_peer_benchmarks(codes)`：`derive_benchmarks` 直接复用，输出 `peer_*` canonical 键（中位数语义与 industry 完全一致）；
- [ ] D3 在线注入：`resolve_company_track` 把 `peer_*` 写进 `fact_values`（None 不注入——缺失即回退）；
- [ ] D4 规则改造（示例，复用表达式列表回退链，**零引擎改动**）：

```yaml
# roe_level.yaml（相对对标组优先 → 相对行业 → 绝对）
thresholds:
  excellent:
    - "value >= peer_avg_roe * 1.5"
    - "value >= industry_avg_roe * 1.5"
    - "value >= 0.15"
```

- [ ] D5 回退语义明确：`peer_*` 缺失 → 顺延 `industry_*` → 绝对阈值；「无对标组」= 与今天行为完全一致（向后兼容）。

### Phase E：商业模式定位（四类 archetype 标签）

- [ ] E1 archetype 定义：`brand`（品牌/解决方案，关注研发费率/渠道）、`odm`（代工，关注产能利用率/良率/大客户集中度）、`component`（核心零部件，关注产品迭代/技术壁垒）、`integrator`（软硬件集成）、`other`；
- [ ] E2 分类器：规则启发（毛利率带 + 研发费率带 + 客户集中度）为主，LLM 复核为辅，输出 `business_model` + `business_model_evidence`；
- [ ] E3 消费：`business_model` 进入 `ThesisIndustryContext` 扩展（run_thesis / review_thesis 按 archetype 切换审查口径——如 ODM 不看品牌溢价、component 看研发投入）。

### Phase F：消费端打通

- [ ] F1 `ArtifactType.COMPANY_TRACK` 注册（`core/schemas.py`）+ `find_artifact_model` 消费；
- [ ] F2 在线图新增 `resolve_company_track` 节点（在 `resolve_industry_context` 之后），降级契约：无 track → `company_track_missing`（MEDIUM）issue + 回退申万基线；
- [ ] F3 `synthesize_insights` / `run_thesis`：payload 注入 track_label / business_model / peer 基准摘要（行业主线升级为公司赛道主线）；
- [ ] F4 `explore_conflicts` / `verify_hypotheses`：偏离判断优先参照 `peer_*` 基准（如"毛利率低于对标组而非行业"）；
- [ ] F5 报告层：`ReportGenerationPayload` 增 `company_track` 字段（赛道标签 + 对标组基准对比章节）；`review_report` gate 增加检查（有 track 必须含对标组对比、无 track 不得虚构）；
- [ ] F6 `task_records/recorder.py` 读取 track 摘要用于观测（对标组缺失率、标签漂移统计）。

## 6. 三级回退链（与既有机制无缝衔接）

```
表达式求值（registry.py 列表语义，阈值/信号引擎通用）：
  value >= peer_avg_roe * 1.5        ← 对标组相对（最精准）
  → value >= industry_avg_roe * 1.5  ← 申万行业相对（基线）
  → value >= 0.15                    ← 绝对阈值（兜底）
字段缺失 → 表达式抛异常 → 跳过 → 顺延下一级；相对表达式必须排在绝对之前。
```

## 7. 降级与口径约束

- **无对标组**：不产 `peer_*`，不发 COMPANY_TRACK artifact 的基准段，发 `peer_group_missing`（LOW）——管道行为与现状完全一致；
- **报告期错配**（分项数据跨期混杂）：`as_of_date` 取最新报告期，`review_notes` 记录跨期混杂；增速字段无法对齐时置空（与 B3 同策略，避免口径错配数值）；
- **境外对标**：v1 只进名单不进基准（跨市场财务口径不可直接中位数）；A 股对标组正常计算；
- **标签 override 必须留痕**：`override_basis` + `source_refs`，LLM 产出需人工复核标记（与 qualitative 块同机制）。

## 8. 测试计划

### 单元级
1. `normalize_segments`：东财列映射、占比/增速透传、fina_mainbz 兜底推导（`is_calculated`）、报告期选择；
2. `derive_track_label`：占比主导 / 增速主导 / 加权冲突三分支、全空输入 → None；
3. `classify_business_model`：四类 archetype 边界样例（工业富联→odm、中兴→brand、光迅→component）；
4. `derive_peer_benchmarks`：显式代码列表取数 → 中位数语义与 industry 一致；负 PE 过滤复用；
5. 规则回退链：`peer_*` 缺失 → `industry_*` → 绝对（现有 `test_industry_thresholds.py` 扩展）。

### 节点级（`resolve_company_track`）
6. 有 track：artifact + `peer_*` 注入 fact_values；无 track：`company_track_missing` issue + 管道继续；
7. 过期 track：`stale=True` + issue，置信度下调；报告 prompt 收到过期分支。

### 端到端（golden，工业富联样本）
8. 工业富联：track_label=AI 算力基础设施 ODM、对标组含华勤技术、`peer_avg_debt_ratio` 明显低于「通信设备」行业均值 → 相对判断改走对标组；
9. 无主营构成数据的公司：整条链路回退申万基线，行为与现状一致。

## 9. 预期收益（工业富联样例对照）

| 维度 | 申万基线（现状） | 对标组（改造后） |
|---|---|---|
| 成长判断 | 通信设备行业营收增速（平缓）→ 低估 | AI 算力链对标组增速（高增）→ 匹配真实赛道 |
| 盈利定位 | 通信设备混入品牌商 → ROE/毛利中位数失真 | AI 服务器 ODM 环节（华勤/广达…）→ 可比 |
| 估值对比 | 行业 PE/PB 中位数（对象错误） | 对标组 PE/PB 中位数（真对手） |
| 报告定位 | "公司属于通信设备行业" | "AI 算力基础设施 ODM，对标华勤/广达/纬创" |

## 10. 优先级与依赖

```
Phase A（数据基础）→ Phase D（基准落地，最快见效：peer_* 注入即触发规则链）
                ↘ Phase B（标签）→ Phase C（对标组）→ Phase E（商业模式）→ Phase F（消费端）
```

建议实施顺序：**A → D → B → C → E → F**（先让"对标组基准"这一最可计算、收益最直接的部分落地，标签与商业模式作为结构化增强跟进；Phase F 的消费端与既有 Phase 5/6（行业语境注入）合并实施，避免重复改 prompt）。
