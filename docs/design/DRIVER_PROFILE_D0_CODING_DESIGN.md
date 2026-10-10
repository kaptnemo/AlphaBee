# DRIVER_PROFILE 前置 R-1/R-2/R-3 + D0 编码设计（详细实现稿）

> **父设计**：`docs/design/DRIVER_PROFILE_RESEARCH_DESIGN.md`（§5 契约 / §6 Tier A / §11 分期 / §14 决议）。
> 本稿只覆盖**前置 R-1/R-2/R-3 + D0**（零 LLM、可独立提交、可独立验证）；D1/D2/D3 不在本稿。
> **状态：编码设计稿（待实现）**。落地时契约改动走 `alphabee-pipeline-contract-steward`，canonical 字段走 `alphabee-schema-steward`。

---

## 0. 目标与非目标

**目标**
1. 消灭「驱动: —」：`generic_fundamental` 兜底驱动从**空**变为**从本公司结构化事实确定性推导**的标的特异变量（带 source）。
2. 修掉 G-4 / G-6（知识到不了下游的两个硬矛盾）。
3. 打通 Tier A 的信息带宽：路由消费 `sw_code` / `segments` 分部结构，新增 `anchor_strength` 三档。
4. 修掉 R-1/R-2/R-3 三个 D1 前置：web_search 误拦、驱动变量查询白名单、消息阈值可配置。
5. 为 D1 预留契约字段（`driver_hypotheses`/`research_agenda`/`novel_drivers`/`candidates`/`unverified_drivers` 等，本稿只声明、不生成）。

**非目标（本稿不做）**
- ❌ 不实现 Tier B（LLM 研究层）、不建 `agents/driver_profile/`、不做缓存/预算。
- ❌ 不接下游 explore_conflicts/verify_hypotheses/thesis/report（D2）。
- ❌ 不登记 `_ARTIFACT_MODELS` / 不包装 `with_deviation_detection`（D2，属 G-3）。
- ❌ 不新增 `OrchestratorState` 顶层字段、不新增 `ArtifactType`。
- ❌ 不改 `NODE_ORDER` / `NODE_CONTRACTS` 键集（避免触发 G-9 指纹锚——D0 设计上就不碰）。

---

## 1. 文件清单

### 改动（alphabee/）
| 文件 | 改动 |
|---|---|
| `middleware/web_search_guard.py` | R-1 词边界；R-2 驱动变量白名单 |
| `middleware/common.py` | R-3 阈值可配置 |
| `config/__init__.py` | R-3 配置字段（`agent.message_limit`） |
| `domain_context/contracts.py` | D0-a：`DriverProfile` v2 + 4 个新模型 + `ActivatedPrimitive` 补 2 字段 |
| `domain_context/context_router.py` | D0-b：`RouterInput` 扩容、`anchor_strength` 三档、sw_code 前缀、segment/financial 匹配；D0-c 兜底驱动 |
| `domain_context/driver_profile.py` | D0-a 透传 `causal_paths`/`when_to_activate`；D0-b 透传 `anchor_strength` + playbook 级字段 |
| `domain_context/schemas.py` | D0-d：`PlaybookSchema` 增 `match_sw_codes`/`match_segments`/`match_financial_structures` |
| `domain_context/domain_playbooks/hog_cycle.yaml` | D0-d：补 `match_sw_codes`（修「农林牧渔→hog_cycle」误配） |
| `orchestrator/nodes/resolve_driver_profile.py` | D0-b：从 state 组装 `sw_code`/`segments`/`financial_structure` 进 `RouterInput`；`load_primitives` 补 try/except（G-10 顺带） |
| `orchestrator/services/payload_builders.py` | D0-e：`_build_driver_profile_summary` 补全（修 G-6） |
| `agents/insights/rescue.py` | D0-e：`build_fallback_insight` 读 `driver_profile`（修 G-4） |
| `apps/cli/renderer.py` | D0-f：展示 `provenance`/`anchor_strength`/`research_agenda`/`novel_drivers`/`why_selected` |

### 配置（config.yaml / config.yaml.example）
| 文件 | 改动 |
|---|---|
| `config.yaml` | 增 `agent.message_limit`（默认 50） |
| `config.yaml.example` | 同上（与模型默认同源） |

### 测试（tests/）
| 文件 | 改动 |
|---|---|
| `tests/middleware/`（或就近 `tests/orchestrator/test_web_search_guard*.py`） | R-1/R-2 用例 |
| `tests/domain_context/test_schemas.py` | D0-d 新匹配字段默认值/forbid |
| `tests/domain_context/test_context_router.py` | D0-b anchor_strength 三档、sw_code 前缀、segment/financial 匹配；D0-c 兜底驱动 |
| `tests/domain_context/test_driver_profile.py` | D0-a v2 字段透传/roundtrip |
| `tests/orchestrator/test_resolve_driver_profile.py` | D0-b 节点组装（segments/sw_code 进 RouterInput）；G-10 降级 |
| `tests/orchestrator/test_preflight.py` / 相关 | R-3 阈值配置不回归（默认 50 行为不变） |
| `tests/agents/insights/test_rescue*.py` | D0-e G-4：Tier2 main_driver 取 primary_drivers |

---

## 2. R-1：`web_search_guard` 词边界匹配

### 2.1 现状问题（§7.7-3）
`_FORBIDDEN_PATTERNS` 第 2 项 `(pe|pb|ps|市盈率|市净率|市销率|总市值|流通市值|估值)` 是**子串**匹配：
`capex`（含 `pe`）、`pipeline`（含 `pe`）被误拦；第 4 项 `行业.*pe` 同理。

### 2.2 改动（`web_search_guard.py:33-38`）
```python
_FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("股票价格/涨跌", re.compile(r"(股价|现价|最新价|收盘价|开盘价|涨跌幅|涨停|跌停|今日.*价|当前.*价|price)", re.I)),
    # R-1：pe/pb/ps 由子串改为词边界（\b...\b），中文禁词不变
    ("市值/估值指标", re.compile(r"(\b(?:pe|pb|ps)\b|市盈率|市净率|市销率|总市值|流通市值|估值)", re.I)),
    ("财务数字", re.compile(r"(营收|净利润|毛利率|roe|roa|eps|每股收益|现金流|负债率|利润率)", re.I)),
    # R-1：行业.*pe 中的 pe 同样词边界化
    ("行业行情数据", re.compile(r"(行业.*涨跌|板块.*涨幅|行业.*\bpe\b|行业.*估值|板块.*市值)", re.I)),
]
```
> 注：`roe/roa/eps` 仍是子串（如 `steps` 含 `eps`）——R-1 决议只列 `pe|pb|ps`，roe/roa/eps 的同类问题**不在本稿**（如需，后续单独拍板）。

### 2.3 测试
`_detect_forbidden` 逐条断言：
- `"capex 周期"` → 不触发（`triggered=False`）
- `"pipeline 进展"` → 不触发
- `"AI PE 估值"` → 触发「市值/估值指标」
- `"行业 PE 估值"` → 触发「行业行情数据」
- 中文侧禁词行为**逐字不变**（回归：`"股价"`、`"市盈率"`、`"营收"` 仍触发）

---

## 3. R-2：驱动变量查询窄白名单

### 3.1 设计（三条件，全部满足才放行）
`web_search_guard` 在 pre-call 禁词拦截**之前**先做白名单判定：

```python
# 行业/商品变量词表（定性搜索白名单；不含任何财务数字语义）
_DRIVER_VARIABLE_ALLOW: list[tuple[str, re.Pattern[str]]] = [
    ("商品价格", re.compile(r"(猪价|仔猪价|猪周期|能繁母猪|生猪存栏|铜价|铝价|锂价|碳酸锂|镍价|钴价|黄金|白银|原油|天然气|动力煤|焦煤|螺纹钢|钢铁|水泥|玻璃|纸浆)", re.I)),
    ("供需/库存/产能", re.compile(r"(产能利用率|开工率|库存|去库|累库|排产|出栏|存栏|需求|供给|景气度|在手订单|新签订单)", re.I)),
    ("周期/事件", re.compile(r"(减产|扩产|投产|检修|OPEC|地缘)", re.I)),
]

# 白名单放行判定：命中行业/商品变量 且 不命中任何财务禁词
def _is_driver_variable_query(query: str) -> bool:
    if not any(pattern.search(query) for _, pattern in _DRIVER_VARIABLE_ALLOW):
        return False
    return not any(pattern.search(query) for _, pattern in _FORBIDDEN_PATTERNS)
```

在 `web_search_guard` 的 pre-call 分支改为：
```python
triggered, reason = _detect_forbidden(query)
if triggered and not _is_driver_variable_query(query):
    return ToolMessage(...)   # 仍短路
# 否则放行：post-call 免责声明 + 数值扫描照旧（数字仍被注入核验指令）
```

### 3.2 判据（显式写死，避免"全面放开"）
1. query 命中 `_DRIVER_VARIABLE_ALLOW`（**行业/商品变量**词表）；
2. query **不**命中 `_FORBIDDEN_PATTERNS`（财务/估值/股价禁词）；
3. 白名单**只放行定性**，post-call `_scan_numeric_hits` 仍执行 → 出现数字照样注入核验指令。

> 局限（如实登记）：middleware 无 symbol 上下文，无法判断"是否本公司财务"；判据退化为
> "命中行业/商品变量且不含财务禁词"。词表是**可编辑常量 + 测试**，需随行业扩展，但**不**是
> 每行业硬编码的 if-else。

### 3.3 prompt 纪律（D1 落地时同步；本稿只登记）
`DRIVER_PROFILE_RESEARCH_PROMPT` 必须写死：**搜索结果中的数字一律不得当财务数据使用**（数字只能来自 tushare/年报工具）——本稿不改 prompt（D1 建 agent 时一起写）。

### 3.4 测试
- `"生猪 2026 下半年 猪价 走势"` → 放行（不短路）
- `"铜价 能繁母猪 存栏"` → 放行
- `"宁德时代 净利润"` → 仍短路（财务禁词命中，白名单不救）
- `"capex 周期"` → 放行（R-1 已不拦；白名单与禁词均不命中 → 正常放行）

---

## 4. R-3：`check_message_limit` 阈值可配置

### 4.1 改动（`middleware/common.py`）
```python
from typing import Any
from langchain.agents.middleware import AgentState, before_model
from langchain.messages import AIMessage
from langgraph.runtime import Runtime

DEFAULT_MESSAGE_LIMIT = 50

def _message_limit() -> int:
    from alphabee.config import get_settings
    return getattr(get_settings(), "message_limit", DEFAULT_MESSAGE_LIMIT)

@before_model(can_jump_to=["end"])
def check_message_limit(state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
    if len(state["messages"]) >= _message_limit():
        return {"messages": [AIMessage("Conversation limit reached.")], "jump_to": "end"}
    return None
```
- 默认 50，行为与现状**逐字一致**（R-3 决议要求默认不变）。
- 去掉 else 分支的 `print(...)`（噪声，顺带清理；若不希望扩大 diff 可保留，属可选项）。
- `get_settings` 用**局部 import** + `getattr` 兜底，避免 middleware 在 import 期拉起配置副作用。

### 4.2 配置（`config/__init__.py`）
`Settings` 增一个扁平字段（或归入新 `agent` 段，本稿取**扁平字段**最简，避免动嵌套模型）：
```python
message_limit: int = 50
```
`config.yaml` / `config.yaml.example` 增：
```yaml
# 研究类 agent 的消息上限（≥50 条直接终止，非截断）
message_limit: 50
```
> 若后续要按 agent 区分，再升级为 `agent.message_limit` 映射；本稿保持单值 + 默认 50。

### 4.3 测试
- 默认无 `message_limit` 段 → `_message_limit()` == 50；`check_message_limit` 在 49/50 边界行为不变。
- 注入 `message_limit=10` → 10 条触发、9 条不触发。
- 6 个共用该 middleware 的 agent 工厂**不回归**（构造成功、行为不变）。

---

## 5. D0-a：`DriverProfile` v2 契约（`domain_context/contracts.py`）

### 5.1 `ActivatedPrimitive` 增 2 字段（G-7 ①）
```python
class ActivatedPrimitive(BaseModel):
    # 既有 9 字段不变 ...
    causal_paths: list[str] = Field(default_factory=list)   # 因果链（此前在快照展开时被丢弃）
    when_to_activate: list[str] = Field(default_factory=list)  # 激活条件
```

### 5.2 新增模型（§5 逐字）
```python
class DriverObservable(BaseModel):
    name: str
    source: str = ""
    cadence: str = ""

class DriverEvidence(BaseModel):
    kind: str = ""
    ref: str = ""
    quote: str = ""

class DriverHypothesis(BaseModel):
    variable: str
    role: str = "primary"          # primary / secondary / risk
    mechanism: str = ""
    company_form: str = ""
    observables: list[DriverObservable] = Field(default_factory=list)
    falsifiers: list[str] = Field(default_factory=list)
    evidence: list[DriverEvidence] = Field(default_factory=list)
    confidence: float = 0.0
    matched_primitive: str = ""

class ResearchQuestion(BaseModel):
    question: str
    why_matters: str = ""
    decisive_evidence: list[str] = Field(default_factory=list)
    preferred_sources: list[str] = Field(default_factory=list)
    priority: str = "high"          # critical / high / medium
    status: str = "open"            # open / answered
```

### 5.3 `DriverProfile` v2（只增不减，`schema_version="2"`）
在既有字段后追加（全部带默认值）：
```python
class DriverProfile(BaseModel):
    schema_version: str = "2"       # 1 → 2
    # ... 既有字段不变 ...
    provenance: str = "rule"        # rule / llm / hybrid
    anchor_strength: str = ""       # strong / weak / none
    key_conflicts: list[str] = Field(default_factory=list)
    recommended_verification_order: list[str] = Field(default_factory=list)
    report_questions: list[str] = Field(default_factory=list)
    driver_hypotheses: list[DriverHypothesis] = Field(default_factory=list)
    research_agenda: list[ResearchQuestion] = Field(default_factory=list)
    novel_drivers: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    unverified_drivers: list[str] = Field(default_factory=list)
    research_confidence: float = 0.0
    research_meta: dict = Field(default_factory=dict)
```

### 5.4 兼容性论证（不改 `coerce_driver_profile`）
`DriverProfile` 是普通 `BaseModel`（无 `extra="forbid"`），`coerce_driver_profile`
（`orchestrator/contracts.py:479`）用 `model_validate`，旧 dict 直接通过；新增字段全带默认。
`ActivatedPrimitive` 只增不减。

### 5.5 测试
- `test_coerce_driver_profile_roundtrip`（既有）继续绿。
- 新增：旧 artifact（`schema_version="1"`、无新字段）→ `model_validate` 通过、新字段为默认。
- 新增：`driver_hypotheses`/`research_agenda` 序列化 roundtrip。

---

## 6. D0-b：`RouterInput` 扩容 + `anchor_strength` + sw_code 前缀（`context_router.py`）

### 6.1 `RouterInput` 扩容（全部来自已落地产物）
```python
class RouterInput(BaseModel):
    # ... 既有字段不变 ...
    sw_code: str = ""                       # INDUSTRY_CONTEXT.sw_code（申万 L1/L2/L3 代码）
    dominant_segment: str = ""              # COMPANY_TRACK.dominant_segment
    dominant_share: float | None = None     # 主力分部占比（0~100）
    fastest_segment: str = ""               # 最快增速分部
    fastest_yoy: float | None = None        # 最快分部同比
    segment_summary: list[str] = Field(default_factory=list)  # ["云计算/服务器 42%(+58%)", ...]
    financial_structure: dict[str, float] = Field(default_factory=dict)  # gross_margin/rnd_ratio/inventory_ratio
```

### 6.2 `PlaybookSchema` 匹配扩展（D0-d，见 §7）与打分
`_score_playbook` 增三路（每路命中一次、各 +1，作为锚强度判据，**不改既有权重语义**）：
```python
# sw_code 精确前缀（申万代码本身可区分 L1/L2/L3）
if inp.sw_code and any(inp.sw_code.startswith(c) for c in pb.match_sw_codes):
    reasons.append("sw_code_match")
# 分部结构（dominant/fastest/segment_summary 子串，复用 _contains）
if any(_contains(inp.dominant_segment, s) or _contains(inp.fastest_segment, s) for s in pb.match_segments):
    reasons.append("segment_match")
# 财务结构阈值（如 {"inventory_ratio": {"gt": 0.25}}）
if all(_threshold_hit(inp.financial_structure, th) for th in pb.match_financial_structures):
    reasons.append("financial_structure_match")
```
新增纯函数 `_threshold_hit(fin: dict, spec: dict)`：`{"gt": x}`/`{"lt": x}` 比较，缺失字段视为未命中。

### 6.3 `anchor_strength` 三档（取代"命中/未命中"二元）
| 档 | 条件 | 行为 |
|---|---|---|
| `strong` | `track_label_match` **或** `sw_code_match`，**且** `segment_match/financial_structure_match` 不矛盾（D0 暂以"无 segment 声明时视为不矛盾"） | 采用，跳过 Tier B（D1） |
| `weak` | 仅 `business_model_match`，或仅 `sub_industry_match`（宽 L1 `industry`） | 采用作锚，但 D1 会进 Tier B 复核 |
| `none` | 无命中 | fallback（现状路径） |

实现：`route()` 内据 `reasons` 推导 `anchor_strength`，写入 `RouterResult`（`_build_result` 增参）。
`RouterResult` 增字段 `anchor_strength: str = ""`。

> G-1 修正：**判据不依赖 `sub_industry` 字段值**——生产环境 `sub_industry` 恒为空串；
> `weak` 分支对"宽 L1 industry 命中"用 `reasons` 里是 `sub_industry_match` 且 `inp.sub_industry==""` 来识别。

### 6.4 测试
- `sw_code` 前缀命中 → `strong`；`track_label` 命中 → `strong`。
- 仅 `industry`（L1）命中 → `weak`；仅 `business_model` 命中 → `weak`。
- 无命中 → `none` + fallback。
- `农林牧渔` 不再命中 hog_cycle（除非 sw_code 精确前缀命中）——G-1 回归钉。

---

## 7. D0-c：`generic_fundamental` 结构性兜底驱动

### 7.1 改动点（`context_router.py::_build_result` 的 fallback 分支）
当回退 `generic_fundamental` 时，`primary_drivers` 改为**从 `RouterInput` 确定性推导**：
```python
drivers: list[str] = []
if inp.dominant_segment and inp.dominant_share is not None:
    drivers.append(f"主力分部 {inp.dominant_segment} 收入占比 {inp.dominant_share:.0f}%")
if inp.fastest_segment and inp.fastest_yoy is not None:
    drivers.append(f"{inp.fastest_segment} 同比 {inp.fastest_yoy:.0f}%（快于主力）")
gm = inp.financial_structure.get("gross_margin")
bench = inp.financial_structure.get("peer_gross_margin")
if gm is not None and bench is not None:
    drivers.append(f"毛利率 {gm:.1f}%（行业基准 {bench:.1f}%，差 {gm-bench:+.1f}pp）")
if not drivers:  # 无任何结构事实 → 回退旧行为（空驱动，避免编造）
    drivers = list(playbook.primary_drivers)
```
- **每条的 source 内嵌在字符串里**（`segment:`/`peer:` 口径），D0 不引入新 source 结构。
- 无结构事实时回退旧行为（空），**不编造**（反模式红线）。

### 7.2 测试
- 有 `dominant_segment/dominant_share` → `primary_drivers` 含"主力分部 …"。
- 有 `fastest_segment/fastest_yoy` → 含"同比 …（快于主力）"。
- 有 `gross_margin` + `peer_gross_margin` → 含"毛利率 …（行业基准 …）"。
- 全空 → `primary_drivers == []`（与现状一致，不编造）。

---

## 8. D0-d：`PlaybookSchema` 新匹配字段 + YAML

### 8.1 `schemas.py`
```python
class PlaybookSchema(DomainSchemaBase):
    # ... 既有字段不变 ...
    match_sw_codes: list[str] = Field(default_factory=list)        # 申万代码前缀（L1/L2/L3 均可）
    match_segments: list[str] = Field(default_factory=list)        # 分部名（dominant/fastest）
    match_financial_structures: dict[str, dict[str, float]] = Field(default_factory=dict)  # {"inventory_ratio": {"gt": 0.25}}
```
`extra="forbid"`：旧 YAML 无这三字段 → 默认空（不破坏加载）。

### 8.2 `hog_cycle.yaml` 补 `match_sw_codes`
```yaml
match_sw_codes:
  - "801010.SI"   # 农林牧渔（L1）——待核实：以 tushare index_classify 实际代码为准
  - "801017.SI"   # 养殖业（L2）——待核实
```
> ⚠️ 实施时**必须用 `index_classify` 实查**（数据源契约纪律），不得凭猜测填码；
> `match_sub_industries` 的「农林牧渔」保留，仅不再作为 **strong** 判据（改走 sw_code）。

### 8.3 测试
- `test_schemas.py`：三新字段默认空；`extra="forbid"` 对未知字段仍报错。
- `test_loader.py`：现有 YAML 加载不回归。

---

## 9. D0-e：下游两个硬矛盾（G-6 / G-4）

### 9.1 `payload_builders._build_driver_profile_summary` 补全（G-6）
在现有 `activated_primitives` 项内补 `key_variables` / `causal_paths` / `disconfirming_signals` /
`preferred_sources`；顶层补 `provenance` / `anchor_strength` / `why_selected` /
`driver_hypotheses`（截断）/ `research_agenda`（截断）/ `unverified_drivers` / `novel_drivers`：
```python
"activated_primitives": [{
    "id": ap.id,
    "key_variables": ap.key_variables,
    "causal_paths": ap.causal_paths,
    "disconfirming_signals": ap.disconfirming_signals,
    "preferred_sources": ap.preferred_sources,
    "priority_questions": ap.priority_questions,
    "report_angles": ap.report_angles,
} for ap in profile.activated_primitives],
"provenance": profile.provenance,
"anchor_strength": profile.anchor_strength,
"why_selected": profile.why_selected,
"driver_hypotheses": [{"variable": h.variable, "role": h.role} for h in profile.driver_hypotheses][:8],
"research_agenda": [{"question": q.question, "priority": q.priority} for q in profile.research_agenda][:6],
"unverified_drivers": profile.unverified_drivers,
"novel_drivers": profile.novel_drivers,
```
> 效果：`agents/insights/prompts.py:31` 要的 `key_variables` 现在真的在 payload 里（修 G-6）。

### 9.2 `rescue.build_fallback_insight` 读 `driver_profile`（G-4）
在 `build_fallback_insight` 顶部加：
```python
driver_profile: dict[str, Any] = context.get("driver_profile") or {}
```
新增纯函数：
```python
def _pick_main_driver_from_profile(dp: dict[str, Any]) -> str:
    prim = (dp.get("primary_drivers") or [])
    return _coerce_text(prim[0]) if prim else ""
```
`main_driver` 计算改为：
```python
main_driver = _pick_main_driver_from_profile(driver_profile) or _pick_main_driver(key_derived, signals)
```
> 效果：Tier 2 降级路径下 `main_driver` 与 `primary_drivers` 一致（修 G-4），
> 验收 5 可直接断言（D0 即生效）。

### 9.3 测试
- `_build_driver_profile_summary`：`activated_primitives[i]` 含 `key_variables`/`causal_paths`；顶层含 `provenance`。
- `build_fallback_insight`：context 有 `driver_profile.primary_drivers=["主力分部 …"]` → `main_driver` 取之；无 → 回退旧逻辑。

---

## 10. D0-f：renderer 展示（`apps/cli/renderer.py`）

`resolve_driver_profile` 分支增：
- `框架` 行追加 `anchor_strength`（strong/weak/none）与 `provenance`（rule 时可不显示或灰显）；
- 若有 `research_agenda` → 追加「研究议程: question(priority)」若干；
- 若有 `novel_drivers` / `why_selected` → 追加展示（G-8：`why_selected` 至今无消费方）。

测试：`apps/cli` 渲染不回归（纯显示，覆盖空值分支）。

---

## 11. D0-g：golden 夹具保真修复（§14.2 #9）

- `tests/orchestrator/test_resolve_driver_profile.py:82` 与 `tests/domain_context/test_context_router.py:36`：
  把显式注入的 `sub_industry="采掘服务"` 改为**生产形状 `sub_industry=""`**；
- 补「仅 `track_label` 命中」用例（金诚信靠 `track_label_match` 命中 `mining_services`）；
- 保留牧原 = `hog_cycle`、金诚信 = `mining_services` 且 `fallback=False` 不变（验收 1）。

---

## 12. D0-h：测试与回归

- 基线（`tests/domain_context` 26 例 + `tests/orchestrator/test_resolve_driver_profile.py` +
  `tests/orchestrator/test_node_contracts.py` 49 例）全绿。
- `tests/orchestrator` 需 `HOME` 指向可写目录（否则 tushare `set_token` 写 `~/tk.csv` 中断 collection）——沿用既有 CI 约定。
- **本稿不改 `NODE_CONTRACTS` 键集 / `NODE_ORDER` / detectors** → 不触发 G-9 指纹锚（`1857784a5cb634e3`）与 `_EXPECTED_LADDERS`。若误触，属范围外改动，回滚。

---

## 13. 提交切分（建议顺序，每段独立可测）

1. `fix(middleware): R-1 web_search_guard 词边界 + R-2 驱动变量窄白名单`（web_search_guard + tests）
2. `feat(agent): R-3 check_message_limit 阈值可配置（默认 50）`（common.py + config + config.yaml(.example) + tests）
3. `feat(domain_context): D0 契约 v2 + RouterInput/anchor_strength + generic 结构性兜底`（contracts/schemas/context_router/driver_profile + 2 yaml + tests）
4. `feat(orchestrator): D0 节点组装 + payload 补全 + Tier2 接 driver_profile + renderer`（resolve_driver_profile/payload_builders/rescue/renderer + tests）
5. `test(domain_context): D0 golden 夹具保真修复`

> 每个提交带「实现契约」说明；锚更新若发生（本稿预期不发生）在提交信息里显式登记。

---

## 14. 风险与回滚

| 风险 | 缓解 | 回滚 |
|---|---|---|
| `anchor_strength` 让某公司从 strong 变 weak/none 而改变路由 | 判据纯函数 + 单测钉住 golden（牧原/金诚信）；weak 仍采用、仅标记，D0 行为等价 | 撤销 `_score_playbook` 扩展 |
| generic 结构性兜底驱动误报 | 只转述结构事实、无事实回退空（不编造）；验收 2 断言 100% 非空 | 撤销 D0-c |
| R-2 白名单"放开"导致财务数字被 web_search 带入 | 白名单只放行"不含财务禁词"的行业变量查询；post-call 数值扫描照旧 | 删 `_DRIVER_VARIABLE_ALLOW` 分支 |
| R-3 配置缺段 | `getattr(settings, "message_limit", 50)` 兜底默认 | 删 config 字段 |
| v2 契约旧 artifact 反序列化 | 只增不减、带默认；`coerce_driver_profile` roundtrip 测试 | 无（向后兼容） |

---

## 15. 一句话总结
本稿把 D0 + 前置拆成 5 个可独立提交的增量：三个 middleware 前置（R-1/R-2/R-3）+ 契约 v2
（D0-a/d）+ 路由增强（D0-b/c）+ 下游两个硬矛盾修复（D0-e）+ 渲染与夹具（D0-f/g）。
全部零 LLM、向后兼容、不碰 D2 的契约/检测器锚；交付后立即消灭「驱动: —」并使
`key_variables`/`causal_paths` 首次有消费者。
