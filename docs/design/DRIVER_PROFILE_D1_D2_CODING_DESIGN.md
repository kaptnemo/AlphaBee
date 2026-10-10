# DRIVER_PROFILE D1（模型研究层）+ D2（下游接线）编码设计（详细实现稿）

> **父设计**：`docs/design/DRIVER_PROFILE_RESEARCH_DESIGN.md`（§7 Tier B / §8 Tier C / §9 下游接线 / §11 分期 / §12 验收 / §14 决议）。
> **前序**：`docs/design/DRIVER_PROFILE_D0_CODING_DESIGN.md`（R-1/R-2/R-3 + D0，已实现）。
> 本稿覆盖 **D1（Tier B 模型研究层）与 D2（下游接线）**；D3（闭环与度量）不在本稿。
> **状态：编码设计稿（待实现）**。落地时契约传播走 `alphabee-pipeline-contract-steward`。

---

## 0. 目标与非目标

**目标**
1. D1：在稀疏区域（无专用框架/弱锚/多主线/身份漂移）用 LLM 生成**标的特异的驱动假设 + 可执行研究议程**，经 Tier C 确定性收敛后落 `DRIVER_PROFILE` v2。
2. D2：让 `driver_hypotheses` / `research_agenda` / `key_conflicts` / `recommended_verification_order` 真正被下游消费——**绝不重复 §1.2/§1.4 的 dead-end**。
3. 修掉 G-3 / G-5 / G-9 / G-10 四个契约/接线缺口。
4. 兑现 §14.2 决议：Tier B **flag 门控起步**；`candidates` **只落字段**（博弈留 P1）。

**非目标（本稿不做）**
- ❌ 不让 LLM 取代 Tier A 规则路由主线、不让 LLM 写 YAML 写库（§2 反模式）。
- ❌ 不做 `narrative_transition`/EventOverlay（roadmap P1/P2）。
- ❌ 不新增 `OrchestratorState` 顶层字段 / 新 `ArtifactType`。
- ❌ 不新增节点（Tier B 在 `resolve_driver_profile` 内扩展，规避 `NODE_ORDER`/`test_contracts_match_graph_builder_registration` 连带改动）。

---

# 第一部分：D1 模型研究层

## D1.1 文件清单

### 新增（alphabee/agents/driver_profile/）
| 文件 | 内容 |
|---|---|
| `agent.py` | `driver_profile_research_agent_factory()`（create_deep_agent + 工具 + 中间件） |
| `prompts.py` | `DRIVER_PROFILE_RESEARCH_PROMPT`（三段式，含证据纪律） |
| `output.py` | `DriverResearchOutput`（LLM 结构化输出 schema，`json_schema_extra.example`） |

### 新增（alphabee/domain_context/）
| 文件 | 内容 |
|---|---|
| `research.py` | `should_research`（触发）/ `build_research_input`（输入包）/ `run_driver_research`（调用+缓存+超时+重试）/ `converge`（Tier C）/ `apply_degradation` 复用 |

### 改动
| 文件 | 改动 |
|---|---|
| `orchestrator/nodes/resolve_driver_profile.py` | flag 门控 + 调 `research.run_driver_research` + Tier C 收敛 + 降级留痕 |
| `config/__init__.py` | `domain_context.research` 段 |
| `config.yaml` / `config.yaml.example` | `domain_context.research` 段 |

### 测试
| 文件 | 改动 |
|---|---|
| `tests/domain_context/test_research.py` | 触发条件 / Tier C 收敛 / 降级阶梯 / 缓存 |
| `tests/orchestrator/test_resolve_driver_profile.py` | flag 关=no-op；flag 开=FakeAgent 注入路径；降级留痕 |
| `tests/agents/driver_profile/`（集成，`@pytest.mark.integration`） | 真实 LLM 冒烟 |

---

## D1.2 数据契约：`DriverResearchOutput`（`agents/driver_profile/output.py`）

LLM 原始输出（Tier C 输入），与 `DriverProfile` v2 字段对齐但更"可被 LLM 稳定输出"：

```python
class ResearchEvidence(BaseModel):
    kind: str = ""            # annual_report / segment / tushare / industry_benchmark / web
    ref: str = ""             # 精确定位（章节名 / api+params / URL / 字段名）
    quote: str = ""           # 原文片段（限长）

class ResearchHypothesis(BaseModel):
    variable: str
    role: str = "primary"     # primary / secondary / risk
    mechanism: str = ""
    company_form: str = ""
    observables: list[str] = Field(default_factory=list)   # 收敛层可简化：字符串列表
    falsifiers: list[str] = Field(default_factory=list)
    evidence: list[ResearchEvidence] = Field(default_factory=list)
    confidence: float = 0.0

class ResearchAgendaItem(BaseModel):
    question: str
    why_matters: str = ""
    decisive_evidence: list[str] = Field(default_factory=list)
    preferred_sources: list[str] = Field(default_factory=list)
    priority: str = "high"

class DriverResearchOutput(BaseModel):
    profit_formula: str = ""                              # 盈利公式（钱从哪来）
    driver_hypotheses: list[ResearchHypothesis] = Field(default_factory=list)
    research_agenda: list[ResearchAgendaItem] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)   # 备选 playbook id（多主线）
    novel_drivers: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)  # 无法验证的猜测（不进主线）
```
`model_config = ConfigDict(extra="ignore")`（LLM 多给字段不炸；缺字段走默认+校验）。

> 收敛后映射进 `DriverProfile.driver_hypotheses`（用 D0 已定义的 `DriverHypothesis`/`ResearchQuestion`），
> 本 schema 只用于 LLM 输出与 `parse_json` 中间态，两者字段一一对应。

---

## D1.3 Agent 构造（`agents/driver_profile/agent.py`）

照抄 `agents/verify_hypotheses/agent.py:19-59`（仓库唯一形态完整的取证 agent），**必须挂 `_ToolExclusionMiddleware`**（§7.7-1：`create_deep_agent` 的 `tools=` 是增量的，含 `execute`+fs）：

```python
def driver_profile_research_agent_factory() -> CompiledStateGraph[Any, Any, Any, Any]:
    from alphabee.tools.financial_report import query_financial_report   # 重依赖：延迟导入
    backend = FilesystemBackend(root_dir=str(PROJECT_ROOT), virtual_mode=True)
    system_prompt = DRIVER_PROFILE_RESEARCH_PROMPT + "\n\n" + json_instruction(DriverResearchOutput)
    return create_deep_agent(
        model=create_chat_model("agent.driver_profile"),
        system_prompt=system_prompt,
        tools=[query_financial_report, query_tushare, web_search, get_industry_fundamentals],
        middleware=[
            check_message_limit,      # R-3 已可配置
            web_search_guard,         # R-1/R-2 已修
            ToolRetryMiddleware(),
            _ToolExclusionMiddleware(excluded=frozenset(
                {"ls", "glob", "grep", "read_file", "write_file", "edit_file", "execute"})),
        ],
        backend=backend,
        skills=["alphabee/skills/tushare", "alphabee/skills/eastmoney"],
        # 不设 response_format=（端点不支持 json_schema）
    )
```
工具表（§7.3）：`query_financial_report`（一手，prompt 强制第一步用）/ `query_tushare` / `web_search`（挂 guard）/
`get_industry_fundamentals`（**本稿真正挂进 tools=**，修 §7.7-4 的"拦截后推荐工具未接线"）。

> `get_stock_news_summary` 当前未注册到任何 agent，本稿**不引入**（保持工具面最小），留 D3 再评估。

---

## D1.4 Prompt（`agents/driver_profile/prompts.py`）

三段式（§7.4 骨架展开为最终文案），**必须含**：
1. 证据纪律（先 `query_financial_report` 读 MD&A 写盈利公式；分部结构与盈利公式自洽；数字只能来自 tushare/年报，**搜索数字不得当财务数据**——R-2 prompt 纪律在此落地）；
2. 标的特异要求（❌「驱动：毛利率」/ ✅「高频高速 CCL 收入占比 × 铜箔-树脂价差」+ 公司分部数值）；
3. `research_agenda` 落点（每条写清"什么证据能定论 + 去哪拿"，不可执行=无效）。

输出字段见 `DriverResearchOutput`，经 `json_instruction` 注入 schema 示例。

---

## D1.5 触发条件（`domain_context/research.py::should_research`）

```python
def should_research(inp: RouterInput, anchor_strength: str, dominant_share: float | None,
                    track_drift: bool) -> tuple[bool, str]:
    if anchor_strength == "none" and inp.has_identity_signals():   return True, "no_anchor"
    if anchor_strength == "weak":                                   return True, "weak_anchor"
    if dominant_share is not None and dominant_share < 0.50:        return True, "multi_segment"
    if track_drift:                                                 return True, "identity_drift"
    return False, ""
```
外加：缓存命中跳过（D1.8）、`domain_context.research.enabled` 开关（flag 门控，§14.2 决议 1）。

---

## D1.6 输入包（`build_research_input`）

按 §7.2 组装：公司身份 / **`COMPANY_TRACK.segments` 多期分部结构**（最关键）/
`fact_values` 关键项 + `peer_*` 行业基准 / Tier A `weak` 命中的 `key_variables`+`causal_paths`+`disconfirming_signals`
（**首次真正被消费**）/ `COMPANY_TRACK.review_notes`（漂移线索）/
`company_track.peer_report.fetch_local_report_fragments(symbol)`（MD&A，上限 12000 字符）。

---

## D1.7 Tier C 确定性收敛（`converge`）

按 §8 C1–C6：
| 步骤 | 规则 | 失败行为 |
|---|---|---|
| C1 schema | `DriverResearchOutput.model_validate` | 单条丢弃；全丢 → 降级链 |
| C2 证据可溯 | 每条假设 ≥1 `evidence` 且 `ref` 非空 | 移 `unverified_drivers`，**不得进 main_driver** |
| C3 事实交叉校验 | `observables` 与 `segments`/`fact_values` 无明显矛盾 | 降权（conf≤0.3）+ issue（MEDIUM） |
| C4 原语对齐 | `variable` 对齐 `key_variables` → `matched_primitive`，继承 `causal_paths`/`disconfirming_signals` | 对不上 → `novel_drivers` |
| C5 结构约束 | ≥1 primary 且 primary ≤3；条数 ≤ `max_hypotheses` | 按 confidence 截断 |
| C6 派生 | `primary_drivers`/`secondary_drivers` ← `driver_hypotheses`（老消费方零改动受益） |

---

## D1.8 预算 / 缓存 / 超时 / 重试

配置（`domain_context.research`，§7.6）：
```yaml
domain_context:
  research:
    enabled: false            # flag 门控起步（§14.2 决议 1），验证后默认开
    max_hypotheses: 7
    max_agenda_items: 6
    max_tool_calls: 20
    wall_timeout_s: 180
    cache_ttl_days: 90
    cache_dir: "data/driver_profiles"
```

- **缓存**：key = `symbol + 最新报告期 + track_label + playbook_version`（§14.2 决议 5）；命中跳过 Tier B（`research_meta.cached=true`）。与 `PeerGroupStore` 同构。
- **超时/超限**：`asyncio.wait_for(wall_timeout_s)` + `max_tool_calls` 超限强制收口，`research_meta["truncated"]=true` + issue（LOW）。
- **显式 `recursion_limit`**：照 `tools/financial_report.py:505` 用 `recursion_limit=40`，进配置。
- **重试一次**：附解析错误 re-ask 一次（本节点自行实现，计入预算）——`ToolRetryMiddleware` 只管工具，不管 LLM。
- **提取函数**：自带只取 `text` 块的提取（不复用 `_extract_final_text` 宽口径，规避 `{"type":"thinking"}` 污染，§7.7-6）。

---

## D1.9 降级阶梯（照 `synthesize_insights`，不照 `explore_conflicts`）

| 档 | 动作 | LLM |
|---|---|---|
| Tier 0 | `parse_json` + `model_validate` 成功 | — |
| Tier 1 | 宽松救援：丢弃非法单条假设、保留合法条目 | 否 |
| Tier 2 | Tier A 结果 + D0 结构性兜底驱动（`provenance="rule"`） | 否 |
| Tier 3 | 最小骨架（playbook + 空驱动 + `degraded_reason`） | 否 |

降级元数据只走 `apply_degradation(base, tier, reason, threshold=1)`（`orchestrator/services/degradation.py`），
节点另补 `fallback_tier`；Tier≥1 产 issue（`category="driver_profile_research_degraded"`，MEDIUM）。

---

## D1.10 节点接线（`resolve_driver_profile`）

在 D0 的"路由 → 组装"之后插入：
```python
enabled = _research_switches(state).get("enabled", False)      # flag 门控（§14.2 决议 1）
should, reason = should_research(...) if enabled else (False, "disabled")
if should:
    try:
        research_out = await run_driver_research(symbol, build_research_input(...))  # 缓存/超时/重试
        profile = converge(profile, research_out)               # Tier C
    except Exception as exc:                                     # 失败 → 降级链，不抛穿
        profile = apply_degradation(profile, tier, reason, ...)
```
flag 关时**严格 no-op**：不产 LLM 调用、`provenance="rule"`、行为与 D0 逐字段一致（测试钉住，参照 `test_assumption_registry.py` 的 no-op 范式）。

---

## D1.11 测试

- 范式 A：`FakeAgent.ainvoke` 返回 `{"messages":[obj(content=json)]}` + patch agent 工厂；节点内延迟 import 工厂必须 patch **源模块**属性（参照 `test_conflict_lifecycle.py:107-136`）。
- 范式 B：patch 节点内 seam（参照 `test_conflict_lifecycle.py:82-101`）。
- 范式 C：降级阶梯逐档单测（参照 `test_insight_degradation.py:42-61`）。
- `should_research` 五分支各一；`converge` C1–C6 各一（含"无证据 → unverified 不进 main_driver"）；缓存命中跳过。
- 集成（`@pytest.mark.integration`）：真 LLM 冒烟，断言 `driver_hypotheses` 非空且证据可溯。

---

# 第二部分：D2 下游接线

## D2.1 文件清单

| 文件 | 改动 |
|---|---|
| `orchestrator/contracts.py` | `ReportGenerationPayload` 增 `driver_profile` 字段（G-5） |
| `orchestrator/services/payload_builders.py` | `build_report_generation_payload` 填 `driver_profile`（消费 D0 已补全的 summary） |
| `agents/explore_conflicts/prompts.py`（+ `nodes/conflicts.py` 输入组装） | 消费 `research_agenda` 的 open questions 生成"框架层冲突"；消费 `key_conflicts`；`candidates` 非空 → 框架竞争假设 |
| `agents/verify_hypotheses/`（prompt + `nodes/verification.py`） | 按 `priority` 决定验证顺序；消费 `recommended_verification_order` |
| `agents/thesis/`（`engine.py` 输入 / `prompts.py`） | `main_driver` 取 `driver_hypotheses[role=primary].variable`；`central_tension` 围绕 `research_agenda[priority=critical]` |
| `orchestrator/detectors.py` | `_ARTIFACT_MODELS` 登记 `DRIVER_PROFILE`/`INDUSTRY_CONTEXT`/`COMPANY_TRACK`；新增检测器 `driver_profile_evidence_present` |
| `orchestrator/agent.py` | `with_deviation_detection("resolve_driver_profile", ...)` 包装 |
| `orchestrator/node_contracts.py` | `resolve_driver_profile.detectors` 扩为 `("artifact_schema_valid","driver_profile_evidence_present")`；`postconditions` 补"驱动非空且可溯"；修正 G-10 两条 `preconditions` 文本 |
| `apps/cli/renderer.py` / `web/src/lib/constants.ts` | 展示 `research_agenda` 摘要 / `novel_drivers`（D0-f 已做 CLI 侧，本稿补 Web 常量） |

## D2.2 下游消费语义（§9 表逐条落地）

| 下游 | 语义 |
|---|---|
| `explore_conflicts` | **首选**以 `research_agenda` open questions 生成假设（"框架层冲突"）；消费 `key_conflicts`；`candidates` 非空 → 框架竞争假设 A/B/C（`FrameworkCompetition` 复用 `ConflictAnalysisResult`，不新建模型） |
| `verify_hypotheses` | 按 `priority` 决定验证顺序；消费 `recommended_verification_order`（**字段零消费 → 首次接线**） |
| `run_thesis` / `generate_report` | `ReportGenerationPayload.driver_profile`；`main_driver` 取 `driver_hypotheses[role=primary].variable`；`central_tension` 围绕 `research_agenda[priority=critical]`；报告可见 `report_questions` |
| `synthesize_insights` | prompt 原则 9 增：引用 `driver_hypotheses` 必保留其 `evidence`；`unverified_drivers` 不得写成结论 |

## D2.3 偏离控制接线（G-3）

1. `detectors._ARTIFACT_MODELS` 登记 `ArtifactType.DRIVER_PROFILE`（及 `INDUSTRY_CONTEXT`/`COMPANY_TRACK`），使 `artifact_schema_valid` 对三者不再是空操作。
2. `agent.py:472` 用 `with_deviation_detection("resolve_driver_profile", ...)` 包装（与其余节点一致）。
3. 新增检测器 `driver_profile_evidence_present`：`fallback=false 或 provenance!=rule` 时 `driver_hypotheses` 非空且证据可溯。
4. `node_contracts.py`：`detectors` 扩项 + `postconditions` 补"驱动非空且可溯"；修正 G-10 `preconditions`（读 `run.context["symbol"]`；`load_primitives` 补 try/except）。

## D2.4 测试锚更新（G-9，预期改动、非回归，须在提交信息显式登记）

1. `tests/tracking/test_scheduler.py::test_f4_does_not_touch_node_contracts`：
   - `sorted(NODE_CONTRACTS)` 键集**不变**（本稿不增删节点），但指纹 `1857784a5cb634e3` **会变**（detectors/postconditions 文本不改键集，但 `_EXPECTED_DETECTORS`/`_EXPECTED_LADDERS` 比对会失败）→ 重算指纹并更新。
   - 注：键集不变则指纹**本可不变**；但若 `_EXPECTED_DETECTORS`（`test_node_contracts.py:113`）按 detector 列表逐字比对，则须同步。
2. `tests/orchestrator/test_node_contracts.py`：`_EXPECTED_DETECTORS` 加 `driver_profile_evidence_present`；`_EXPECTED_LADDERS` 不变。
3. 三处变更在提交说明中逐条登记为"实现契约变更"。

## D2.5 验收（§12 全量，D2 交付时须满足）
1. 回归全绿（含 3 处锚更新的破例登记）。
2. 覆盖率：`primary_drivers` 非空 100%；`fallback=true 且 driver_hypotheses 为空` = 0。
3. 证据可溯率 100%；`unverified_drivers` 进 `main_driver`/`central_tension` 次数 = 0（可断言）。
4. 标的特异率：同 playbook 下 `primary_drivers` 完全相同比例 < 50%。
5. 下游消费率：`research_agenda` open questions 被 `explore_conflicts` 引用 ≥ 80%；`key_variables`/`causal_paths` 进 insight prompt = 100%；Tier 2 降级下 `main_driver` 与 `primary_drivers` 一致（G-4）。
7. 成本上界：p95 工具调用 ≤ `max_tool_calls`、p95 耗时 ≤ `wall_timeout_s`；缓存命中 ≥ 60%；未因 `check_message_limit` 提前终止。
8. 偏离留痕：四类失败（研究失败/截断/证据不足/事实矛盾）均产 issue（断言对象是 issue/artifact 载荷，非 step 状态，§7.7-7）。
9. 契约有效性：`artifact_schema_valid` 对 `DRIVER_PROFILE` 非空操作（已登记 + 已包装）。

---

## D2.6 提交切分（建议）

1. `feat(domain_context): D1 模型研究层（agent + 输入包 + Tier C + 缓存预算 + flag 门控）`
2. `feat(orchestrator): D1 节点接线 + 降级留痕`
3. `feat(insights/conflicts/verification): D2 下游接线（agenda 驱动研究 + 验证顺序）`
4. `feat(report): D2 ReportGenerationPayload 增 driver_profile（G-5）`
5. `feat(deviation): D2 检测器登记 + 包装 + 契约修正（G-3/G-10）+ 测试锚更新（G-9）`
6. `feat(cli/web): D2 渲染 research_agenda/novel_drivers`

> D1 与 D2 **必须成对交付**（§14.2 决议 7）；每个提交带「实现契约」说明，锚更新显式登记。

---

## 风险与回滚

| 风险 | 缓解 | 回滚 |
|---|---|---|
| LLM 编造驱动变量 | C2 证据强制可溯 + `unverified_drivers` 隔离 + C3 交叉校验 | 关 `enabled` 回退 D0 |
| 成本失控 | 触发收窄 + 缓存 + 工具/时长硬上界 | 关 `enabled` / 调预算 |
| web_search 误拦/数字带入 | R-1/R-2 已修 + prompt 禁数字 | 回退 middleware 改动 |
| agent 持有 shell/fs | `_ToolExclusionMiddleware` | — |
| artifact 再次 dead-end | D2 与 D1 同期 + 验收 5 消费率硬指标 | — |
| 契约锚变更误判为回归 | 提交信息显式登记 + 三处锚同步更新 | — |

---

## 一句话总结
D1 把"研究"从查表补成 LLM 在稀疏区域生成**标的特异驱动假设 + 可执行研究议程**（flag 门控、有预算缓存、Tier C 收敛留痕）；D2 把这份产物**真正接进冲突探索/假设验证/论点/报告**，并修掉 G-3/G-5/G-9/G-10。两者成对交付，验收 §12 的覆盖率/可溯率/消费率是硬门。
