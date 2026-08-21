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
- 行业 Phase 2（字段治理/多来源 mapping）↔ 本文档 Phase A：**公共前置**——两者均已 ✅ 落地
  （`biz_segment_*` / `peer_*` 字段走同一套 schema 治理与 adapter mapping，Phase A 详见 §5）；
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

### Phase A：业务线数据基础（✅ 已落地，2026-08）

> **实施说明（实测修正，与初版设计的 3 处差异）**：
> ① EM `stock_zygc_em` **无"同比增长率"列** → 分项增速由 normalize **跨期同口径推导**
> （最新报告期 vs 去年同报告期同名分项，`derive_segment_yoy`）；② EM 的"收入比例/毛利率"
> 是 **0-1 比例**（各类别内部合计 = 1.0，实测茅台酒 0.8569 ≈ 85.7%）→ normalize ×100 转
> PERCENT（canonical 单位）；③ Tushare `fina_mainbz` **不推导占比**——该接口产品/地区混列且
> 无分类类型标记，求和推导会口径错配（实测兆易创新"集成电路产品"被算成 ~1%），宁缺毋错，
> 占比仅在 EM 源可得（缺失时 Phase B 用 revenue 兜底）；另对 fina_mainbz 按
> `(报告期, 分项)` 去重、保留 update_flag 最新修订。

- [x] A1 ✅ 新增 akshare adapter：`adapters/akshare/operation_mapping.yaml`（`stock_zygc_em`
      → canonical：report_date / 分类类型 / 主营构成 / 主营收入 / 收入比例 / 主营成本 /
      主营利润 / 毛利率；成本/利润比例暂不映射）
- [x] A2 ✅ `schemas/operation.yaml` 补 `biz_segment_revenue_share` / `biz_segment_revenue_yoy` /
      `biz_segment_category`（含两源 source_mappings 与口径 notes）
- [x] A3 ✅ 新增 `alphabee/company_track/`（contracts + data + normalize）：`fetch_business_segments(symbol)`
      —— 东财主营构成为主，Tushare `fina_mainbz` 兜底（修订行去重；`SegmentCollection.source` 标记
      实际来源，双源失败显式 error 留痕）
- [x] A4 ✅ `normalize.py`：单位统一（EM 0-1 比例 ×100 转 PERCENT）、跨期 yoy 推导（is_calculated
      标记）、噪音过滤（`min_share` 低占比剔除 + `drop_other` 剔除"其他"分项）、报告期对齐
      （`latest_report_period` / `assess_period_consistency`）
- [x] A5 ✅ 单测：`tests/company_track/`（adapter 列映射、EM/tushare 归一化、跨期 yoy、噪音过滤、
      双源降级、去重、符号转换）——17 个用例全绿；实测 603986：存储芯片 71.3% / 微控制器 20.8%、
      茅台 85.7% 口径正确

### Phase B：真实赛道标签推导（✅ 已落地，2026-08）

> **实施说明**：`alphabee/company_track/label.py` + `track.py` + `contracts.CompanyTrackArtifact`。
> 分项数据按**分类类型优选**（按产品分类 → 按行业分类）取业务线；tushare 兜底无占比时按
> 类别内收入近似排序并告警。漂移检测跨年报期比较，**全期统一优选分类**（避免早期只有
> "按地区"行时把地区当主线）；实测兆易创新正确识别 2015→2016 "存储芯片销售收入 → 存储芯片"
> 主线变化。

- [x] B1 ✅ 规则层 `derive_track_label(segments)`：真实赛道 = 收入占比 top1 且增速非负的业务线；
      占比与增速冲突时取**「占比 × 增速」加权得分最高**者（负增速 ×0.5 衰减，极端负增速钳制
      0.05，避免高增速低占比噪音；占比最大但增速为负且加权落败时切换标签并告警）
- [x] B2 ✅ 可选 LLM 复核（`agent.track`）：`synthesize_track_label` 给规则输出 + 业务线明细，
      产出 `track_label` / `override_basis`；LLM 失败/关闭回退纯规则（`track_method` 留痕 rule/llm）
- [x] B3 ✅ **override 机制**：`CompanyTrackArtifact` 内 `track_label`（公司赛道，修正字段）与
      `sw_industry`/`sw_code`（申万基线，调用方注入）并存；`review_notes` 首条固定注明
      「公司赛道标签基于 X 报告期数据」——下游引用必须携带该口径
- [x] B4 ✅ 新鲜度：`as_of_date = 最新报告期`、`stale_after = 报告期 + 90 天`；`detect_track_drift`
      跨年报期主线变化写入 `review_notes`（业务漂移可观测——直击硬伤 4）
- [x] 单测：`tests/company_track/test_label.py`（11）+ `test_track.py`（4）全绿；实测 603986：
      `track_label=存储芯片`、dominant/fastest 一致、漂移检测仅报真实主线变化

### Phase C：对标组构建（✅ 已落地，2026-08）

> **实施说明**：`alphabee/company_track/peer_extract.py`（C2，`agent.peer_group`）、
> `peer_validate.py`（C4，代码规范化 + A 股 tushare 校验 + 境外拆分）、
> `peer_group_build.py`（C1 优先级汇总端到端）、`peer_group_store.py` 扩展
> （`international` / `reason_map`，Phase D 的存储升级）。「商业模式同类」来源（同
> archetype + 分项相似度）依赖 Phase E archetype 与全市场分项索引，暂未实现——v1
> 来源优先级为：人工候选 > 研报/业绩会 LLM 抽取 > 空（不编造）。

- [x] C1 ✅ 来源优先级落地：调用方直接给候选（人工白名单/结构化）> 研报/业绩会 LLM 抽取
      > 空对标组（`is_empty` 留痕，绝不编造）；「商业模式同类」待 E 后按需补充
- [x] C2 ✅ LLM 抽取（`agent.peer_group`）：输入研报/业绩会片段 + 业务线构成 → JSON 对标组
      （name/code/exchange/reason/source）；无文本/输出非数组/异常 → 空列表 + `meta.note`
      留痕（降级不编造）
- [x] C3 ✅ 持久化：`data/peer_groups/{symbol}.json`（原子写、latest-wins、人工可编辑覆盖；
      Phase D 的 PeerGroupStore 扩展 international / reason_map，旧文件向后兼容）
- [x] C4 ✅ 校验：`normalize_peer_code`（6 位前缀推断 SH/SZ/BJ，境外保留后缀）、A 股经
      Tushare `stock_basic` 存在性校验（best-effort，失败按格式放行并告警）、境外进
      `international`（仅名单不进基准——避免跨市场口径错配）、无法识别交易所的候选剔除
- [x] 单测：`tests/company_track/test_peer_group_build.py`（9 个）；实测 601138（工业富联）：
      A 股华勤技术进基准、境外广达/英业达进名单、假代码 999999.SH 被 tushare 校验剔除并告警

### Phase D：对标组基准（✅ 已落地，2026-08）

> **实施说明**：`alphabee/industry/data.py::fetch_peer_financials_for_codes`（取数循环与
> `fetch_industry_peers` 共享 `_fetch_rows_for_codes`，唯一差异是成分来源换成显式代码列表）、
> `alphabee/company_track/peer.py::derive_peer_benchmarks`（复用 normalize + derive_benchmarks，
> 中位数语义与行业完全一致）、`company_track/peer_group_store.py`（对标组 JSON 存储，Phase C3
> 的 LLM 抽取在此之上扩展）、`orchestrator/nodes/resolve_company_track.py`（已接入主图
> `resolve_industry_context → resolve_company_track → run_analysis_engines`）。
> **机制语义说明（D5）**：表达式列表回退链给出「**同一档位内 peer 优先** + 字段缺失顺延
> industry → 绝对」；peer 与 industry 结论**跨档位冲突**时（如 peer 判 aggressive 而 industry
> 判 moderate）按档位扫描序 conservative → moderate → aggressive 解析——这是零引擎改动机制的
> 边界，Phase F 消费端如需 peer 绝对权威再按规则处理。

- [x] D1 ✅ `alphabee/industry/data.py` 泛化取数：`fetch_peer_financials_for_codes(codes, limit)`
      （fina_indicator + daily_basic，与指数成分取数共享同一循环/归一化链路）
- [x] D2 ✅ `derive_peer_benchmarks(codes)` → `peer_*` canonical 键（peer_avg_roe /
      peer_avg_debt_ratio / peer_avg_gross_margin / peer_revenue_yoy / peer_median_pe_ttm /
      peer_median_pb；None 不注入——缺失即回退），已补 `schemas/industry.yaml` 6 个字段
- [x] D3 ✅ 在线注入：`resolve_company_track` 节点读 `data/peer_groups/{symbol}.json` →
      `derive_peer_benchmarks` → `peer_*` 写 `fact_values` + `COMPANY_TRACK` artifact
      （`ArtifactType.COMPANY_TRACK` 已注册）；无对标组 → `company_track_missing`（MEDIUM）
      issue + step SKIPPED（回退申万基线，显式留痕）；计算失败 → `peer_group_benchmarks_missing`
      + degraded artifact
- [x] D4 ✅ 规则改造：`roe_level` / `debt_ratio` 加入 peer 优先表达式（每个档位
      peer → industry → 绝对），**零引擎改动**
- [x] D5 ✅ 回退语义：`peer_*` 缺失 → 顺延 `industry_*` → 绝对阈值；「无对标组」行为与
      Phase 0/1 完全一致（向后兼容，`test_peer_thresholds.py` 全覆盖）
- [x] 单测：`tests/company_track/test_peer.py`（5）+ `test_peer_group_store.py`（5）+
      `tests/agents/derived_facts/test_peer_thresholds.py`（9）+ `tests/orchestrator/test_resolve_company_track.py`（4）；
      实测 603986 对标组（5 只存储芯片设计 peers）：ROE 3.9% / 毛利率 44.7% / 负债率 6.9% /
      PE(TTM) 125× / PB 8.5× / 营收增速 192%

### Phase E：商业模式定位（✅ 已落地，2026-08）

> **实施说明**：`alphabee/company_track/business_model.py`（E1/E2）+ `CompanyContext.business_model`
> （E3，thesis models）+ `build_company_context` 自动分类 + `reviewer._layer1_check` Rule 5
> archetype 视角。v1 分类带为规则常量（可调）；LLM 复核组件 `agent.business_model` 可选。

- [x] E1 ✅ archetype 定义：`brand`（品牌/解决方案，关注研发费率/渠道）、`odm`（代工，关注
      产能利用率/良率/大客户集中度）、`component`（核心零部件，关注产品迭代/技术壁垒）、
      `integrator`（软硬件集成，关注生态绑定/交付）、`other`（指标不足不猜测）
- [x] E2 ✅ 分类器：规则启发（毛利率带 + 研发费率带 + 大客户集中度佐证）为主
      （odm：毛利<20% 且研发<8%；component：≥40% 且 ≥12%；brand：≥40% 且 <12%；
      integrator：20-40% 且 ≥10%；带外 → other 需人工确认），LLM 复核（`agent.business_model`）
      失败回退规则，输出 `business_model` + `business_model_evidence`
- [x] E3 ✅ 消费：`CompanyContext.business_model` 由 `build_company_context` 从财务快照自动
      分类（% → RATIO）；`reviewer._layer1_check` Rule 5 按 archetype 切换审查口径
      （ODM 盈利弱化不按品牌商毛利标准衡量、component 盈利弱化提示研发战略投入）
- [x] 单测：`tests/company_track/test_business_model.py`（11）+ `test_e3_consumption.py`（5）

### Phase F：消费端打通（✅ 已落地，2026-08）

> **实施说明**：在线节点 `resolve_company_track` 升级为**完整赛道注入**（组装
> CompanyTrackArtifact：业务线分项 + 赛道标签 + 商业模式 + 对标组基准 + 漂移），降级
> 分级为 `company_track_missing`（无业务线，MEDIUM）/ `peer_group_missing`（无对标组，LOW）/
> `peer_group_benchmarks_missing`（对标组计算失败，MEDIUM）/ `company_track_stale`（过期，MEDIUM，
> 走既有 issue 披露检查）。消费端：报告层新增 `ReportCompanyTrackPayload` + `ReportSections.company_track`
> 章节（确定性报告 + LLM prompt 指令 + stale 提示分支）；论点层 ThesisIndustryContext 扩展
> track_label/business_model/peer 摘要；冲突/验证上下文注入对标组摘要；recorder 落 track 摘要。

- [x] F1 ✅ `ArtifactType.COMPANY_TRACK` 注册（Phase D 已做）+ `find_artifact_model` 消费
- [x] F2 ✅ `resolve_company_track` 已接入主图（Phase D）；本阶段升级为完整赛道组装 + 降级分级
      （无业务线 → MEDIUM 回退申万基线；有赛道无对标组 → LOW；对标组失败 → MEDIUM degraded；
      过期 → `company_track_stale` MEDIUM 进披露检查）
- [x] F3 ✅ `run_thesis`：`ThesisIndustryContext` 扩展 `business_model` / `track_label` /
      `peer_group` / `peer_benchmarks`，从 COMPANY_TRACK artifact 注入（行业主线升级为公司赛道主线）
- [x] F4 ✅ `explore_conflicts` / `verify_hypotheses`：`build_verify_context` 与冲突探索 prompt
      注入 `company_track` 摘要（track_label / peer_benchmarks），指令「偏离判断优先参照对标组
      基准而非申万行业均值」
- [x] F5 ✅ 报告层：`ReportGenerationPayload.company_track`（`ReportCompanyTrackPayload`）、
      `ReportSections.company_track` 章节（确定性报告渲染赛道/对标组基准 + stale 提示；
      LLM prompt 指令要求含对标组对比，stale 时显式写出过期提示）
- [x] F6 ✅ `task_records`：TaskRecord 新增 `company_track_label` / `company_business_model` /
      `peer_group`，recorder 读取 COMPANY_TRACK artifact 落观测
- [x] 单测：`tests/orchestrator/test_resolve_company_track.py`（重写，6 个降级分级用例）+
      `tests/orchestrator/test_company_track_report.py`（4 个报告层消费用例）

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

建议实施顺序：**A → D → B → C → E → F**（先让"对标组基准"这一最可计算、收益最直接的部分落地，标签与商业模式作为结构化增强跟进；Phase F 的消费端与既有 Phase 5/6（行业语境注入）合并实施，避免重复改 prompt）。**当前进度（2026-08）**：Phase A（业务线数据基础）、Phase B（真实赛道标签）、Phase C（对标组构建：LLM 抽取 + A股/境外校验拆分）、Phase D（对标组基准，`peer_*` 三级回退链已激活）、Phase E（商业模式定位 + reviewer archetype 视角）均已 ✅ 落地。**路线图 A–F 全部实施完成（2026-08）**。
