# 规则-市场自适应与 AI 渗透 Gap 收口实施设计

> 目标：把「全项目 LLM/规则参与度评估」中发现的 gap 转化为可落地、可分阶段验收的实施设计。
> 关联文档：
> - `docs/roadmap/MARKET_REGIME_ROADMAP.md`（Phase 5 回测与模型升级、Phase 3 LLM 解释层）
> - `docs/roadmap/DOMAIN_CONTEXT_ROADMAP.md`（P2 软评分/趋势）
> - `docs/midterm/MIDTERM_DECISION_MODEL.md`（§6.3 阈值"结构性示意，应回测"）
> - `docs/guide/DEVELOPMENT.md`（蒸馏闭环自动化列为未完成事项）
> - `docs/design/INSIGHT_DEGRADATION_DESIGN.md`（降级设计范式，本文档沿用其"诚实降级"纪律）
> 状态：📐 **设计中（部分已实施）**。评估结论依据：2026-08 全仓盘点（19 处 LLM 调用点 / 约 90+ 条规则 / 零学习闭环）。
> 已实施（2026-08）：**结构化输出迁移第一波（json_object 容器约束）** + `tools/fundamentals.py` 的 `parse_json` 治理修复，见 §4.7b / §4.7a。

---

## 1. 背景与结论回顾

全项目盘点得出三个结构性结论：

1. **分工健康但错位**：定量骨架 100% 确定性（21 衍生 + 19 勾稽 + 22 信号 + 8 维度），LLM 承担叙事/验证/综合；但 19 处 LLM 调用点中 8 处是"机械抽取/格式转换"，4 处开放推理中有 3 处被 flag 默认关闭（`--enhance` / `--llm-review`，`apps/cli/args.py:36-48`）。
2. **规则刚性**：约 80% 阈值为跨行业绝对数（`PEG>3`、`debt_ratio>0.65`、`interest_coverage≥5` 等）；自适应仅存在于三处——anomaly 的公司自身历史基线、market_regime 的 10 年滚动分位、thesis 的事后乘数（硬编码关键词名单）。
3. **零学习闭环**：`forward_returns.py`（`market_regime/forward_returns.py:57,156`）只在测试中被调用；midterm 常量自认"应回测"但仓库无回测执行器；`task_records` 蒸馏只产文本建议、不回写规则、仅 CLI 手动触发（`task_records/distiller.py:46-123`）。

本设计把上述 gap 收敛为 **7 个设计项（D1-D7）+ 2 个可选延伸（D8-D9）**，分四个阶段实施。

---

## 2. Gap 清单总表

| ID | Gap | 现状锚点 | 设计项 | 优先级 |
|---|---|---|---|---|
| G1 | 无回测执行器，规则常量从未被数据校准 | `market_regime/forward_returns.py`（仅测试调用）；`midterm/*.py` 常量注释"应回测" | D1 | P0 |
| G2 | market_regime 与主链平行，signal/thesis 不消费 regime | `ArtifactType.MARKET_REGIME` 已注册（`core/schemas.py:79`）、`coerce_market_regime` 已存在（`contracts.py:378`）但**无生产节点** | D2 | P0 |
| G3 | ~80% 信号/衍生阈值为绝对数 | `signal/rules/valuation_risk.yaml`（peg>3/2/1）、`debt_risk.yaml`；仅 `debt_ratio`/`roe_level` 两条衍生规则有 peer→industry→绝对回退链 | D3 | P1 |
| G4 | 蒸馏闭环单向：LLM 建议 → 文本 → 丢弃 | `task_records/distiller.py`（纯文本）；`TaskAnalyzer` 统计已备（`analyzer.py:132-251`） | D4 | P1 |
| G5 | 无规则失效监测 | `TaskAnalyzer.signal_trigger_rates()` 存在但无漂移对比 | D5 | P1 |
| G6 | 无"市场/政策变化"感知，规则失效靠人工发现 | 无对应模块 | D6 | P2 |
| G7 | LLM 默认策略不灵活 + 4 处治理裂缝 | `args.py:36-48` 硬编码默认；`markdown_table_handler.py:379-394` 绕过工厂；`fundamentals.py:231` 严格 `json.loads`；`peer_validate.py` 只验代码存在不验名称匹配 | D7 | P0/P2 |
| G8 | 信号只有硬档位，无置信度/边际 | `signal/registry.py:107-123`（命中即停） | D8（可选） | P2 |
| G9 | 研究层单次综合，无对抗/元规划 | `agents/insights/` 单次调用 | D9（愿景） | P3 |

---

## 3. 设计原则（贯穿全部设计项）

1. **分层规则**：不变量层（勾稽关系、迁移合法性、诚实性契约）保持硬编码；参数层（阈值/权重）必须可回测、可影子、可人工放行；语境层（行业分位、regime 条件）必须数据驱动。
2. **LLM 提建议、规则做权威、人工做 gate**：LLM 只产出结构化候选（规则 diff、审查 ticket），**任何规则变更必须经过影子期 + 人工 promote**，不存在自动改规则的代码路径。
3. **影子先行**：新规则/新阈值先以 shadow 身份并行求值、只记录不生效，用真实运行数据对比后再放行。
4. **诚实降级**：regime/行业基准缺失时显式 `missing_fact` 或回退绝对阈值并留痕，绝不静默；沿用 `INSIGHT_DEGRADATION_DESIGN` 的降级纪律。
5. **最小契约变更**：优先复用现有机制——回退链表达式列表（`derived_facts/registry.py` 顺延语义）、`fact_values` 注入、已注册的 `ArtifactType`/typed contract；新增产物一律走 artifacts 列表，**不在 `OrchestratorState` 上加节点产物字段**（遵循 `CLAUDE.md` orchestrator artifact contract）。

---

## 4. 分项设计

### D1：回测与验证执行器（P0，地基）

**现状锚点**：`market_regime/forward_returns.py` 已实现 `compute_forward_returns()`（6 个月前向收益/回撤，防前视）+ `search_similar()`（同 regime 相似历史），但仅在 `tests/market_regime/test_regime_classifier.py` 被调用；`persistence.py` 已有周序列落盘（`load_score_history` / `load_regime_history`，`persistence.py:118,192`）。

**设计**：

1. 新增 `alphabee/backtest/__init__.py` + `alphabee/backtest/regime_validation.py`（不新建轮子，纯编排现有模块）：

```text
load_score_history()          # persistence.py:118（周频 MarketScore 序列）
→ compute_forward_returns()   # forward_returns.py:57（防前视已内置）
→ validate()                  # 新增：三个验证器
```

2. 三个验证器（全部确定性纯函数，同一输入同一输出）：
   - `ic_of_score()`：`total_score` 与 6M 前向收益的 **Spearman 秩相关（IC）**，按年输出 IC 序列（判断评分是否随市场环境漂移）。
   - `band_statistics()`：按评分档（`>70 / 50-70 / <50`）分组统计平均前向收益、平均最大回撤、正收益概率——验证 `position.yaml` 档位切分是否仍有区分度。
   - `phase_statistics()`：按六阶段（`regime_classifier.py`）统计各阶段前向收益/回撤差异 + 迁移合法性占比（`transition_valid/suspicious`）。
3. 输出：`data/market_regime/validation/report_<YYYYMMDD>.json` + 同目录 Markdown 摘要（模板渲染，无 LLM）。
4. CLI：`main.py` 新增 `--validate-regime`（`apps/cli/args.py` + `tasks.py` 加子命令，沿用 `--task-stats` 模式）。
5. **midterm 预留**：定义 `BacktestHarness` Protocol（`validate(features: DataFrame, forward_returns: DataFrame) -> ValidationReport`），regime 是第一个实现；midterm 常量校准（`midterm/bayes.py:37-65`、`classifier.py:40-46`、`position.py` 仓位带）在 `MIDTERM_FACTOR_DATA_DESIGN` 因子历史数据落地后作为第二个实现接入，**本次不做**。

**验收标准**：
- [ ] `--validate-regime` 在有 ≥24 周历史数据时产出报告（不足时显式报"样本不足"退出码非 0）；
- [ ] 单测：合成历史（构造已知前向收益）验证 IC/分组统计数值正确；防前视断言（回测只用当日及以前特征，沿用 `forward_returns.py` 现有约束）；
- [ ] 报告落盘路径与文档一致，CI 可复跑。

**改动文件**：新增 `alphabee/backtest/`；`apps/cli/args.py`、`tasks.py`；`docs/guide/USAGE.md`。

---

### D2：market_regime 接入主链（P0 注入展示，P1 信号条件化）

**现状锚点**：`RegimeSnapshot`（`market_regime/models.py:113`）、`ArtifactType.MARKET_REGIME`（`core/schemas.py:79`）、`coerce_market_regime`（`contracts.py:378`）全部就绪；`fact_values` 是 `dict[str, float]`（`state.py:73`），`safe_eval_formula` 可引用任意键——**regime 可以作为"事实"注入，零引擎改动**。

**设计（Phase 0：注入 + 展示）**：

1. 新增节点 `resolve_market_regime`（`alphabee/orchestrator/nodes/resolve_market_regime.py`）：
   - 图序：`collect_raw_facts → resolve_market_regime → resolve_industry_context`（`agent.py:326-328` 插入一条边）；
   - 读 `load_regime_history()` 最新一期；若缺失或 `date` 距今 >7 天 → 触发一次完整采集+评分+分类（复用 `collectors/market_regime/` + `score_engine` + `classify_regime`），失败则记 `Issue(category="market_regime_unavailable")` 并**不注入、不产出 artifact**（下游走回退）；
   - 产出 `market_regime` artifact（`RegimeSnapshot` payload，`producer_step=resolve_market_regime`），复用现有 coerce 契约；
   - 注入 fact_values 数值键（`phase` 是字符串放不进 fact_values，只放数值）：
     `regime_total_score` / `regime_valuation_score` / `regime_trend_score` / `regime_liquidity_score` / `regime_risk_delta` / `regime_phase_risk`（六阶段 → 风险代理数值 0-1，映射表新增 `market_regime/rules/phase_risk.yaml`）。
2. 消费点 1（report，Phase 0 内完成）：`payload_builders.build_report_generation_payload()`（`services/payload_builders.py:284`）追加 `market_regime` 摘要段（phase/total_score/position_low-high/main_drivers/risks，**含 stale 标注**）；`REPORT_GENERATOR_PROMPT`（`orchestrator/prompts.py`）在 `scenario_analysis` 章节要求"结合市场状态语境讨论三情景前提"——**不新增 report section**，避免 `gates.py:185-198` 的 `expected_sections` 与 `artifact_coverage` 分母联动改动（契约最小化）。
3. 消费点 2（recorder）：`task_records/recorder.py` 的 `capture()` 新增可选字段 `regime: RegimeRecord | None`（`TaskRecord` additive 字段，默认 `None`，向后兼容旧记录）。
4. 契约登记：`schemas/INDEX.yaml` 的 market_regime 域登记 6 个 `regime_*` 字段，`field_consumers.consumed_by_signal` 登记（Phase 1 信号引用后），避免 dead-end。

**设计（Phase 1：信号条件化，只改 3 条）**：
- `valuation_risk`：`high` 条件加 `and regime_total_score >= 50`（熊市/震荡区估值风险更致命）；对应新增 `regime_conditional` 可选 YAML 键，`signal/registry.py` 在条件求值前把 `regime_*` 缺失时**跳过该分支**（视为未命中，而非 invalid）。
- `crowding_risk`：`regime_phase_risk >= 0.7`（高位分歧期拥挤度信号升级）。
- `trend_break_risk`：`regime_trend_score < 40` 时 medium 条件放宽。
- thesis 层（可选，同 Phase 1）：`agents/thesis/engine.py` 新增 `_apply_regime_context()`，仅对 `valuation_fit` 维度做 `context_notes` 注入（不改分数），与现有 `_apply_company_context`（`engine.py:585-645`）并列——**Phase 1 只加注释不加乘数**，乘数留待 D1 回测验证后再说。

**验收标准**：
- [ ] 单测：`resolve_market_regime` 注入/缺数据降级/过期重算三条路径；`regime_conditional` 分支在 `regime_*` 缺失时结果与当前版本完全一致（向后兼容）；
- [ ] 集成：报告 prompt payload 含 regime 摘要且 `scenario_analysis` 提及市场状态（LLM 断言或模板断言二选一）；
- [ ] 契约：`INDEX.yaml` 无 dead-end 字段（每个 `regime_*` 都有登记消费者）；
- [ ] gates 不变：`compute_report_metrics` 分母不变，既有 gate 测试全部通过。

**改动文件**：新增 `nodes/resolve_market_regime.py`；`agent.py`；`services/payload_builders.py`；`orchestrator/prompts.py`；`task_records/{models,recorder}.py`；`schemas/INDEX.yaml`；`market_regime/rules/phase_risk.yaml`（新增）；Phase 1 追加 `signal/registry.py` + 3 个规则 YAML + `thesis/engine.py`。

---

### D3：信号阈值相对化（P1）

**现状锚点**：回退链模式已验证可行——`derived_facts/rules/debt_ratio.yaml` 与 `roe_level.yaml` 已用"表达式列表顺延语义"（peer_avg → industry_avg → 绝对阈值，`registry.py` 列表求值）；但 `signal/registry.py:62-73` 的 `trigger_rules` 只支持单条件字符串。

**设计**：
1. `signal/registry.py` 扩展：`trigger_rules` 的 level 值支持 `str` 或 `list[str]`（列表语义与 derived_facts 一致：按序求值，首 True 命中，引用缺失字段抛异常则顺延下一表达式）；`evaluate()`（`registry.py:107-123`）相应改循环。**向后兼容**：所有现有 YAML 单条件写法不变。
2. 首批改造 5 条（其输入字段已有 peer/industry 事实来源）：`debt_risk`（debt_ratio/current_ratio）、`capital_efficiency_risk`（roe）、`moat_erosion_risk`（gross_margin_trend）、`valuation_risk`（peg 无 peer 基准 → 保留绝对阈值 + D2 的 regime 条件）、`growth_quality_risk`（revenue_growth）。每条 YAML 写三列式回退链，与 `debt_ratio.yaml` 同构。
3. 缺失事实登记：peer/industry 尚不覆盖的输入字段，在 `INDEX.yaml` `field_gaps` 登记（如 `peg` 的行业分位），由 `resolve_industry_context` 后续补齐——**缺什么登记什么，不造数**。

**验收标准**：
- [ ] 单测：列表顺延语义（首级缺失→二级命中；全缺失→绝对阈值命中；全缺失且绝对阈值未命中→none）；
- [ ] 单测：旧单字符串条件行为不变（回归）；
- [ ] 影子对比（与 D4 联动）：改造前后 5 条信号在历史 TaskRecord 上的命中分布对比报告。

**改动文件**：`signal/registry.py`；5 个规则 YAML；`schemas/INDEX.yaml`（field_gaps 补充）。

---

### D4：蒸馏闭环结构化 + 影子模式 + 人工 promote（P1）

**现状锚点**：`task_records/distiller.py` 4 个 prompt 全部产自由文本 Markdown；`TaskStore`/`TaskAnalyzer` 统计已备；`apps/cli/tasks.py:78-82` 只有 `--distill` 打印文本。

**设计**：
1. 新增 `alphabee/task_records/candidates.py`：
   - `RuleCandidate` Pydantic：`id` / `kind`（`new_signal | threshold_adjust | industry_calibration`）/ `target_file` / `rule_id` / `yaml_snippet`（完整候选规则 YAML 或阈值 diff）/ `source_stats`（来自 TaskAnalyzer 的触发率、z 分布等）/ `rationale` / `confidence` / `status`（`proposed → shadowed → approved_promote | rejected`）。
   - `CandidateStore`：JSONL 落盘 `data/rule_candidates/`，含状态机迁移函数（非法迁移拒绝）。
2. `distiller.py` 新增 `generate_candidates()`：新 prompt `DISTILL_CANDIDATES_PROMPT`（要求输出 `RuleCandidate` 数组 JSON；`target_file` 只能是现有规则目录内的路径；`yaml_snippet` 必须能被 `safe_eval_formula` 语义校验——**候选生成时就做公式静态校验**，非法候选直接 `rejected`）。
3. 影子模式：`run_analysis_engines`（`nodes/analyze.py`）在 `settings.pipeline.shadow_rules` 开启时加载 `rules_shadow/`（与 `signal/registry.py` 同构加载），与生产规则并行求值；结果写入 state 的 artifacts（`SHADOW_SIGNAL_RESULTS` 新 ArtifactType，登记 role_group=ANALYSIS），并进入 `TaskRecord.shadow_results`（additive 字段）。
4. 人工 promote：CLI 三命令：
   - `--rule-review`：列出候选 + 影子期对比（触发率差异、命中分布）+ 关联漂移告警（D5）；
   - `--rule-review shadow <id>`：候选进入 `rules_shadow/`；
   - `--rule-review promote <id>`：**前置条件硬校验**——影子样本 ≥20 次运行且触发率偏差在 ±30% 内，否则拒绝并提示原因；通过后把 `yaml_snippet` 复制/合并到正式 `rules/` 目录（diff 形式落盘 `data/rule_candidates/history/`）。**代码只提供工具，promote 永远由人在 CLI 显式执行。**
5. `TaskAnalyzer` 新增 `shadow_vs_production()`：按 rule_id 对比生产/影子触发率。

**验收标准**：
- [ ] 单测：候选公式非法 → 生成即 rejected；状态机非法迁移拒绝；promote 前置校验（样本不足/偏差超标被拒）；
- [ ] 集成：`--distill --write-candidates` 产出合法 JSONL；影子模式开启后 TaskRecord 含 `shadow_results` 且不影响生产信号/报告；
- [ ] 安全断言：代码库中**不存在**任何自动写 `rules/*.yaml` 的路径（grep 断言进测试）。

**改动文件**：新增 `task_records/candidates.py`、`DISTILL_CANDIDATES_PROMPT`；`distiller.py`；`nodes/analyze.py`；`core/schemas.py`（ArtifactType）；`task_records/{models,recorder,analyzer}.py`；`apps/cli/{args,tasks}.py`；`config.yaml`（`pipeline.shadow_rules`）。

---

### D5：规则漂移监测（P1）

**现状锚点**：`TaskAnalyzer.signal_trigger_rates()`（`analyzer.py:132`）已算静态触发率。

**设计**：
- `analyzer.py` 新增 `trigger_rate_drift(window: int = 10)`：按时间序把运行记录分成最近 N 次 vs 前 N 次两个窗口，逐 signal 计算触发率相对变化；漂移判定：`|Δrate| / max(前窗rate, 0.05) > 0.5 且 两窗合计样本 ≥ 20` → `drifted`。
- 输出接入 `--task-stats`（表格）与 `--rule-review`（漂移规则置顶，作为重校候选的上下文）。

**验收标准**：
- [ ] 单测：构造两窗口触发率序列，验证漂移阈值边界与样本不足不误报；
- [ ] CLI 集成：`--task-stats` 输出含漂移列。

**改动文件**：`task_records/analyzer.py`；`apps/cli/tasks.py`。

---

### D6：LLM 变化感知器（P2，离线）

**现状锚点**：`tools/news.py`（新闻/公告数据）+ `web_search`（带 `web_search_guard`）已具备数据能力；无"规则失效"感知。

**设计**：
- 新增离线 agent `alphabee/agents/rule_watch/`（`create_deep_agent`，组件 `agent.rule_watch`，挂 `web_search_guard` + `check_message_limit`）：
  - 输入：现有规则清单（signal/anomaly/derived 的 id + 阈值摘要，程序生成）+ 时间窗口内新闻/公告（Tushare 公告 + 政策新闻检索）；
  - 输出：`RuleReviewTicket` 数组（Pydantic）：`rule_id`（可选，允许主题级）、`change_type`（`policy | accounting_standard | industry_rule | market_structure`）、`summary`、`evidence_urls`、`suggested_action`（`recalibrate | add_rule | retire_rule | none`）、`confidence`；
  - **硬约束**（prompt）：不接触行情数字；不产出规则 YAML（那归 D4 蒸馏）；只产出"哪些规则可能因外部变化失效"的 ticket。
- CLI：`--scan-rule-changes [--since <days>]`，ticket 落盘 `data/rule_review_tickets/`；`--rule-review` 展示 ticket 作为校准决策上下文。
- **不自动改任何规则**：ticket 只能被人工消化后转成 D4 候选。

**验收标准**：
- [ ] 单测：`RuleReviewTicket` schema 校验 + 输出落盘；web_search_guard 挂载断言；
- [ ] 冒烟：对 1 个已知政策事件（如会计准则修订新闻）能产出关联 ticket（人工验收，不进 CI）。

**改动文件**：新增 `agents/rule_watch/`；`apps/cli/{args,tasks}.py`；`task_records` 或独立 store（ticket 存 `data/rule_review_tickets/`）。

---

### D7：LLM 默认策略 + 治理修复（P0 修裂缝，P2 修默认策略）

**7a. 治理裂缝修复（P0，独立小改，可与 D1/D2 并行）**：

| 裂缝 | 现状锚点 | 修复 |
|---|---|---|
| 表格整理 LLM 绕过工厂 + 硬编码模型 | `loader/markdown_table_handler.py:379-394`（裸 `OpenAI()` + `deepseek-v4-flash`） | 改走 `create_chat_model("agent.table_format", model=settings.llm.table_model)`；`config.yaml` 增加 `llm.table_model: "${LLM_TABLE_MODEL:deepseek-v4-flash}"`；恢复 token 观测 |
| fundamentals 摘要严格 `json.loads` 单点故障 | `tools/fundamentals.py:231`、`tools/industry_fundamentals.py:404-447` | ✅ **已实施**：改用 `parse_json`（`utils/pipeline.py`）+ `json_mode=True`（2026-08）；"解析失败降级为仅结构化数据"仍可作后续增强 |
| 对标组 name↔code 配对幻觉 | `company_track/peer_extract.py`（LLM 同时产 name+code）、`peer_validate.py:17-99`（只验代码存在） | `peer_validate` 增加名称一致性校验：用 stock_basic 名称表比对"该代码的公司名是否与 LLM 给的 name 匹配"，不匹配 → 丢弃该候选 + warning；匹配不上名称表 → 保守丢弃 |

**7b. LLM 默认策略配置化（P2，依赖 7a 完成后评估成本）**：
- `config.yaml` 新增 `pipeline:` 节：`enhance_default: "${PIPELINE_ENHANCE_DEFAULT:false}"`、`llm_review_default: "${PIPELINE_LLM_REVIEW_DEFAULT:false}"`；
- `collectors.py` 初始化 state 时以 `settings.pipeline.*_default` 为缺省（CLI flag 显式覆盖）——**默认值保持 false，只把"默认在哪里定义"从代码移到配置**，让生产环境按部署策略翻转；
- 远期（D9 落地后）评估把 `llm_review_default` 翻 true，前提是报告"LLM gate 失败回退确定性 gate"路径（`gates.py:434-440`）已有足够运行证据。

**7c. 结构化输出迁移（json_object 容器约束）——✅ 第一波已实施（2026-08）**：

端点能力冒烟结论（deepseek-v4-pro @ api.deepseek.com）：
`json_object` ✅ / `json_schema(strict)` ❌ / 强制 tool_choice ❌（思考模式限制）。因此不用 `with_structured_output(json_schema/function_calling)`，只用 **`bind(response_format={"type":"json_object"})` 容器约束**：API 层保证输出是合法 JSON（无散文/栅栏），字段级结构仍由 `json_instruction` + `parse_json` + Pydantic 校验 + 既有降级链保证——**降级链全部保留，只减漂移不改契约**。

已实施清单：
- `utils/llm.py`：`create_structured_model()`（内部走 `create_chat_model` 保持测试 monkeypatch 兼容）+ `tracked_chat_completion(json_mode=True)` + 端点能力注释；`settings.llm.structured_json` 总开关（`config.yaml` / `config.yaml.example`）。
- 迁移调用点（7 处）：`reporter.py`（agent.report）、`gates.py`（harness.evaluator）、`thesis/enhancer.py`、`thesis/reviewer.py`、`tools/fundamentals.py`、`tools/industry_fundamentals.py`、`workflow/framework_monitor.py`；后两者同时完成"严格 `json.loads` → `parse_json`"治理修复（7a 表格第 2 行）。
- `utils/prompts.py:229-231` 过时注释已更新为端点能力事实。

未迁移（后续波次）：
- **deepagents 站点**（explore_conflicts / verify_hypotheses / synthesize_insights）：`create_deep_agent` 的 `response_format` 会走 ProviderStrategy(json_schema)/ToolStrategy(强制 tool)，本端点均 400；且思考模式限制未验证与工具循环共存。待端点支持 json_schema 后迁移，届时 `json_instruction` 可简化。
- `loader/markdown_table_handler.py`（7a 表格第 1 行，flash 模型旁路治理）与 `company_track` 三处、`industry/nodes.py`：属独立治理项，按各自设计执行。

验证：`pytest -m "not integration"` 1043 passed；真实端点端到端冒烟（create_structured_model + json_instruction + parse_json、开关关闭退化、json_mode）全部通过。

**验收标准**：
- [ ] 7a 单测：表格 handler 走工厂（mock 断言组件 tag）；fundamentals 非法 JSON 降级不抛；peer 校验拒绝名称不匹配候选；
- [ ] 7b 单测：config 默认 → state 缺省值；CLI 覆盖 config。

---

### D8：软判决信号（P2，可选）

- `SignalAnalysisArtifact.results` 每条增加 `margin`（距命中阈值的边际距离，用于报告区分"勉强命中"与"深度命中"）+ `confidence`。
- 契约传播（按 pipeline-contract-steward 纪律逐点核对）：`agents/signal/engine.py`（生产侧）→ `contracts.py`（typed contract）→ `payload_builders._build_key_signals` → `REPORT_GENERATOR_PROMPT`（signal_analysis 章节要求展示弱信号）→ `recorder._extract_signals` → `TaskRecord.SignalResult`（additive）。
- **前置条件**：D3 回退链上线后才有"边际"语义（阈值不再是单点）。

### D9：对抗式研究 + 元规划（P3，愿景，不在本设计实施范围）

- 元规划节点：分析前由 LLM 基于 domain playbook + company track 生成本次研究问题清单，动态分配 verify_hypotheses 预算；
- 对抗式 insight：bull/bear 双 agent 辩论 → 确定性结算；
- market_regime 相似历史（`search_similar`）结果接入报告作为"历史类比"附录（附 `limitation_note`）。

---

## 5. 契约变更清单（steward 纪律核对）

| 变更 | 生产者 | 消费者（逐一核对） | 备注 |
|---|---|---|---|
| 新节点 `resolve_market_regime` | `nodes/resolve_market_regime.py` | signal 条件（fact_values）、payload_builders（报告摘要）、recorder（TaskRecord.regime）、INDEX.yaml 登记 | `ArtifactType.MARKET_REGIME` 已注册，无需改 enum |
| 新 fact 键 `regime_*` ×6 | 同上 | `run_analysis_engines`（自动可见）、D2 三条信号、`thesis/engine.py`（P1 注释注入） | 数值型，符合 fact_values 契约 |
| 新 ArtifactType `SHADOW_SIGNAL_RESULTS` | `nodes/analyze.py`（影子模式） | recorder、`--rule-review` | role_group=ANALYSIS 注册 |
| `TaskRecord` additive 字段（`regime` / `shadow_results`） | recorder | analyzer、CLI 展示 | 默认 None/[]，旧记录兼容 |
| `SignalRule.trigger_rules` 支持列表 | `signal/registry.py` | 全部信号规则（向后兼容）、D3 改造 YAML | 单测回归 |
| `RuleCandidate` / `RuleReviewTicket` 存储 | `task_records/candidates.py`、`agents/rule_watch/` | CLI `--rule-review` / `--scan-rule-changes` | 新增数据目录 |
| report prompt 增加 regime 语境 | `orchestrator/prompts.py` | `reporter.py`（生成侧） | **不新增 section，gates 不动** |

**明确不做**：不在 `OrchestratorState` 上加 `market_regime`/`shadow_results` 等节点产物字段（只加 `regime_enabled`/`shadow_rules` 控制位，或直接读 settings）；不改 `finalize_message` 顶层 payload 结构（regime 经 report 与 artifacts 已有出口）。

---

## 6. 分阶段实施计划

### Phase 0（地基，本迭代）：D1 + D2(注入展示) + D7a
| 任务 | 验收 | 测试 |
|---|---|---|
| `alphabee/backtest/regime_validation.py` + `--validate-regime` | 产出 IC/分组/阶段报告 | 合成数据单测 + 防前视断言 |
| `resolve_market_regime` 节点 + 注入 + 报告语境 + recorder | regime artifact 出现且报告提及；缺失时降级留痕 | 节点三条路径单测 + 集成 |
| 7a 三处治理修复 | 裂缝关闭 | 各自单测 |

**Phase 0 完成判据**：`pytest` 全绿；`--validate-regime` 可跑；`INDEX.yaml` 无 dead-end 字段；报告在 regime 可用时包含市场状态语境。

### Phase 1（规则自适应）：D2(信号条件化) + D3 + D4 + D5
**完成判据**：5 条信号回退链 + 3 条 regime 条件化上线且影子对比报告存档；`--rule-review` 三命令可用；漂移列进 `--task-stats`；promote 硬校验生效。

### Phase 2（AI 渗透）：D6 + D8 + D7b
**完成判据**：`--scan-rule-changes` 产出可用的规则失效 ticket；信号带边际/置信度且全链消费；LLM 默认策略配置化。

### Phase 3（愿景，另行立项）：D9

---

## 7. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 回退链/regime 条件化导致过拟合 | 保留绝对阈值末级回退；上线必须附影子对比（D4）；regime 条件先只改 3 条 |
| regime 数据缺失/过期导致信号误变 | `regime_*` 缺失 → 条件分支跳过（等价旧行为）；过期 → stale 标注 + Issue |
| 影子模式污染生产 | 影子与生产规则分目录加载、分 artifact 存储、不进 thesis/report；`shadow_rules` 默认关闭 |
| promote 被误用 | 硬前置校验（样本数 + 偏差）+ 历史 diff 落盘 + 无自动写规则代码路径（测试断言） |
| 报告 prompt 改动导致 gate 波动 | 不新增 section、不改 `expected_sections` 分母；改动后跑全量 gate 测试 |
| LLM 调用量上升（D7b 翻转默认） | 默认保持 false，仅配置化；翻转决策前置成本评估 |

---

## 8. 与现有文档的关系

- `MARKET_REGIME_ROADMAP.md` Phase 5（评分有效性回测、HMM/GMM 升级）→ 本设计 D1 是其执行器；Phase 3（LLM 解释层）→ 保留为其后续阶段，与本设计 D6/D9 不重叠（D6 面向"规则失效"，Phase 3 面向"评分解释"）。
- `MIDTERM_DECISION_MODEL.md` §6.3"阈值应回测" → D1 的 `BacktestHarness` 是预留入口。
- `DOMAIN_CONTEXT_ROADMAP.md` P2 软评分 → 与 D3 相对化并行推进，合流点为"行业分位事实"。
- `DEVELOPMENT.md` 蒸馏闭环自动化 → 被 D4 取代其"候选 YAML diff + 一键回测"设想。
- 本文档任何 Artifact/节点契约变更的落地，必须按 `alphabee-pipeline-contract-steward` 逐点核对下游消费者（§5 清单即初版 consumer map）。
