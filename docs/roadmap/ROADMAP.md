# AlphaBee Analysis Agent Roadmap

## 背景判断

AlphaBee 当前已经具备较完整的“事实采集 → 衍生指标 → 风险信号 → 异常检测 → 冲突探索 → 论点汇总 → 报告生成”流水线，但核心短板是：系统能发现大量指标和风险，却还没有形成真正像公司财务分析师一样的主观点。

当前输出容易表现为：

- 指标很多，但主次不清
- 风险信号很多，但没有中心矛盾
- 结论是维度打分，而不是可争辩的投资论点
- 报告像数据堆砌，而不是观点驱动的研究备忘录

后续建设目标应从“财务指标检测系统”升级为“有洞见的公司财务分析 Agent”。

---

## 实现状态跟踪（2026-08 与当前代码对齐）

> 本节是对下文各 Phase 的**最新落地状态**盘点，用于和当前 `alphabee/orchestrator/` 代码保持同步。
> 状态标记：✅ 已实现　🟡 部分实现　⬜ 未实现

| 条目 | 状态 | 现状说明（代码位置） |
|---|---|---|
| 0.1 anomaly 进入 signal/thesis | ✅ | `nodes/analyze.py` 将 AnomalyEngine 输出投影回 `fact_values`，`anomaly_cluster_risk` / `cross_validation_break` 等异常信号规则可命中 |
| 0.2 ThesisEngine 显式消费 anomaly/conflict/verification/context | ✅ | `nodes/thesis.py` 全量传入；`agents/thesis/engine.py` 的 `run()` 已接收 `anomaly_report / conflict_analysis / verification_results / company_context / insight` |
| 0.3 canonical field / signal rule 一致性 | 🟡 | 无系统性 schema 校验（工程侧 E3 未落地）；`services/gap_recorder.py` 已把 blocked/missing_fact 信号记录进失败库 |
| 0.4 Insight schema 脆弱性 | ✅ | 枚举归一化 + 四级降级阶梯（`agents/insights/rescue.py`：严格解析 → 宽松救援 → 确定性兜底 → 最小骨架），任何失败模式下 insight artifact 必然存在；降级标记随 artifact 落库（degraded / fallback_tier / degradation_reason），报告 prompt 有对应降级分支（见 `tests/orchestrator/test_insight_degradation.py`） |
| 0.5 待验证/已验证冲突分层 | ✅ | `explore_conflicts` 不再把 provisional 冲突升格为 issue；`verify_hypotheses` 作为结算层：verified/partial 高严重度冲突升格为 `verified_conflict` issue、rejected 沉淀为 decision、状态显式回写 `conflicts_result`；`review_thesis` 只保留 thesis_conflict（见 `tests/orchestrator/test_conflict_lifecycle.py`） |
| 0.6 用户输出与调试输出分层 | ⬜ | `main.py` `_render_final_report()` 仍把全部 issues（含 parse_error / rewrite 信息）打印到“🐞 系统问题”段 |
| Phase 1 InsightAgent 稳定观点骨架 | 🟡 | 已接入主图（`nodes/insights.py` + `agents/insights/`），报告 prompt 以 `insight.core_view` 为主线（`prompts.py`）；`what_would_change_my_mind → falsification_conditions` 已贯通；parse fail 已有四级降级（0.4）；`materiality_rank` 未显式驱动报告排序 |
| Phase 1.5 探索/验证/结算分层 | 🟡 | 结算层已随 0.5 落地（provisional 不升格、verified/partial 升格为 issue、rejected 沉淀 decision、状态回写 `conflicts_result`）；剩余：验证预算 / 最短排除路径 / 未探索区域记录、evidence refs 硬约束 |
| Phase 2 BusinessModelContext | 🟡 | `services/company_context.py` 已有 industry / sub_industry / market_cap_category / lifecycle_stage / business_model_summary；无 BusinessModelClassifier、无 playbooks/primitives（见 `docs/roadmap/DOMAIN_CONTEXT_ROADMAP.md`） |
| Phase 3 Claim-Evidence Graph | ⬜ | 未实现；`gates.py` 已有 `evidence_coverage / grounding_score` 检查，但上游 Decision 普遍未填 `based_on / evidence_refs` |
| Phase 4 ExpectationFitAgent | ⬜ | 未实现 |
| Phase 5 报告备忘录化 | 🟡 | 报告已重构为“观点驱动”（`REPORT_GENERATOR_PROMPT`：insight 主线 + 12 章节 + 三情景 + 可证伪条件），LLM 空输出有确定性降级报告（`reporter.py` `build_deterministic_report`）；“系统问题”段仍在 CLI 暴露 |
| 偏离控制框架（DEVIATION_CONTROL_FRAMEWORK） | ✅ | 设计见 `docs/design/DEVIATION_CONTROL_FRAMEWORK.md`。**已提交**：F0 偏离分类法 + 跨 run 偏离账本（`261070b`/`fb3bced`）、F1 节点契约 + 6 个后置检测器（`49d027f`）、F1c 假设登记簿生产者 + 报告 gate 消费者（`15213cb`）、F1 结转与 F1c 认证更正（`458f975`/`42a8526`）。**已提交**：F2 恢复阶梯协议化 + 降级传导阻尼 + `DeviationSettings` 五段配置（`2186eb9` 8 路径）与其收尾 `insights.py` 降级写入统一 + 开关登记（`9573d09` 2 路径），经第三方独立认证（提交树 8/8 + 2/2 与认证值逐字相符）。**已提交**：F3 放大标注 + 加权边审计（`681a216` 12 路径：`INSIGHT_CONFIDENCE_WEIGHTS` 显式化并收口 medium=0.92 + `audit_amplification` + `review_thesis` 发射点 + category 登记 + R2-8 切换）与其结转文案（`893b3b8` 2 路径，登记实际发射点并更正 `attach_amplification_audit` 文案）。**已提交**：F4 宏观环自动调度（`d65164f` 7 路径 2581 行：`alphabee/tracking/` 触发判定纯函数 + 一次性 reconcile 调度 + CLI + §9.4 红线 `require_human_confirm` 恒 `False` + §9.2 逐条反证强制入账），经第三方独立认证（提交树 7/7 与冻结锚逐字相符，verdict=pass 零 findings）。**已提交**：F5 度量层（`e9fe18c` 6 路径：`orchestrator/services/telemetry.py` 的 §11.1 八项指标 + run 尾部 sink 接线 + `deviation_metrics` 表 + CLI `--deviations` 只读时间线视图）与其结转（`13d1b0d` 4 路径：落地 §14.6 的 `budget.d_max`，使 §11.1「预算消耗」在生产路径可算）。**F0–F5 六期全部完成**：上述每个提交均经第三方独立认证（提交树与认证树逐字相符：F0 10/10、F1 11/11、F1c 4/4、F2 8/8 + 2/2、F3 12/12 + 2/2、F4 7/7、F5 6/6、F5 结转 4/4），且每期先 review（verdict=pass）后提交。§15 验收 6「状态行同步」已履行；设计有要求而 v1 未落地的点全部具名登记于下方「偏离控制框架顺延项登记」 |
| 研究连续体（RESEARCH_CONTINUUM） | ✅ | 设计见 `docs/design/RESEARCH_CONTINUUM_DESIGN.md`（Temporal Long-Horizon）。**P1–P6 六期全部提交（未 push）**：P1 W3 财报原文窗口→证据抽取（`e42aab2`）、P2 D2-B2 入口前置校验+告警只读视图+E402 豁免（`635887b`）、P3 D1-A2 tracking 偏离入账本（`b692e84`）、P4 D3-C2 research_status 派生视图（`984b9bb`）、P5 W5 ThesisVersion 读写+反漂移（`e78f0c3`）、P6 §8 L2 ResearchEngine 协议+适配器（`6410cc5`），另本收官登记提交（ROADMAP 状态行+顺延项+工程实践登记第 9–12 条）。每期先独立复审（t5/t8/t11/t14/t17 verdict=pass，自建探针+变异杀死+门禁双跑）后经 captain 提交门（显式路径 add+逐路径哈希核对+提交树认证+复冻）入库。**收官验收 t19 达标**（R19-1 口径裁定）：§11 六条 = 1 部分达标（同 §3 W1 设计预期：tracking 路径已闭合、普通 `--midterm` 不落帧）/2–6 达标；§15.10 映射 9/9；端到端真实 CLI 实测 P3/P4/P5 同帧共存（`risk_alert`+账本 2 行+版本文件 50 行）。v1 未落地点全部具名登记于「偏离控制框架顺延项登记」（P3-L1/L2、P6-L1、RC-1…RC-6）；全量门禁离线口径与理由登记于工程实践登记第 10 条 |

“当前关键问题”中的 #2（anomaly/conflict 进入 thesis）、#3（Report Generator 被限制为格式化器）、#4（Reviewer 维度覆盖落后）、#7（冲突状态边界）已解决：
- `nodes/thesis.py` 全量传入 anomaly/conflict/verification/context，`engine.py` 已显式消费（0.2）。
- `prompts.py` 的 `REPORT_GENERATOR_PROMPT` 已改为“有观点、有论证、可证伪”的忠实裁决模式，不再要求“只做格式化”。
- `agents/thesis/reviewer.py` 的 `ThesisReviewer` 遍历全部 8 个维度生成 `dimension_verdicts`，审查逻辑已随 `dimensions/` 目录（8 个 YAML）同步扩展。
- `explore_conflicts` / `verify_hypotheses` / `review_thesis` 已按 provisional / settled 分层（0.5）。

### 行为变更登记

> 依据 `docs/design/DEVIATION_CONTROL_FRAMEWORK.md` §8.2 规则 3：「权重调整属于行为变更，必须在 ROADMAP 登记」。

| 日期 | 变更 | 依据 | 影响与回归面 |
|---|---|---|---|
| 2026-09-17 | `insight → thesis` 加权边的 medium 档乘数 **0.95 → 0.92**（同时提为模块常量 `alphabee/agents/thesis/engine.py::INSIGHT_CONFIDENCE_WEIGHTS`） | §14.4-A 与契约文案 `node_contracts.py:370` 两处早已登记 0.92，代码 0.95 为离群值 | medium 档 insight 对维度 confidence 的乘数略降（high/low 不变）；`min(factor, 0.85)` 的 F2 降级阻尼与"一档封顶"不受影响；受影响的既有断言见 `tests/orchestrator/test_degradation_damping.py` 与 F3 主测试的常量一致性用例 |
| 2026-09-18 | `deviation.budget.d_max` 由**不存在**变为默认 `{analysis: 60, tracking: 20}`（类型 `dict[str,int] \| int \| None`） | §14.6 / §10.2 明文规定该键，而 F2 期 `DeviationBudgetSettings` 从未落地它 ⇒ §11.1「预算消耗 = `D_cum / D_max`」在生产路径**恒 `None`**（F2「配置 vs §14.6」偏差，F5 期发现） | **只读指标面**：`budget_consumption` 由恒 `None` 变为可算（analysis `10/60 = 0.1667`、tracking `10/20 = 0.5`、`run.context["d_max"]` 仍优先）；**无执行语义**（`on_exceed` 未接线，见顺延项 F5-D3）；缺失/非正/不可解析/未知 `task_kind` 仍为 `None`（绝不回退 0）；缺 `deviation` 段的 `config.yaml` 仍可 import（默认值保证，有专测 + 变异 N2 坐实）；`tests/orchestrator/test_telemetry.py` 收集数 65 → 82，全量门禁 607 passed |
| 2026-09-22 | 研究连续体 P1（W3）：新增 `report_window` 段，默认 `enabled: true` —— `nodes/midterm.py::_window_texts()` 由"仅已验证冲突 explanation"变为"**本地已解析财报叙事章节原文** + 已验证冲突 explanation"，且"没有窗口"**不再静默** | `docs/design/RESEARCH_CONTINUUM_DESIGN.md` §15.1-F 明文要求该登记；§15.1-A 核实窗口过去只含冲突解释 ⇒ 定性证据抽取（Stage A/B）在主链从未真正生效 | **序口径（相对 §15.1-B 字面措辞的显式偏差，captain 定案，已在模块 docstring 逐字披露）**：主序 = **报告期**（解析 `report_period` 的（年, 期内序），年内序 = 第一季度 < 半年度/第二季度 < 第三季度 < 年度报告），`created_at` **仅作 tie-break**；`as_of` 过滤要求**报告期期末日与入库时刻都不晚于 `as_of`**（F5 前视消除：`created_at` 晚于 `as_of` 者即便报告期已到也不入选 ⇒ 历史回放不再把 "as_of 时点尚未入库" 的财报接进窗口；实时 run `as_of`=今天时 `created_at` 恒 ≤ 今天，故 21/22 家结论不变，有 `test_as_of_excludes_not_yet_ingested_manifest` 坐实）。偏差理由（命令枚举 `find reports -name .report_manifest.json | wc -l` = 142 份 / 22 家（限定作用域；未限定的 `find . -name` 会把 gitignored `tmp/**` 下自建合成 manifest 一并计入））：`created_at` 是**解析入库时刻**（同批 ingest 按目录序递增），按 `created_at` 倒序选出的"最新一期"与报告期语义最新者在 **21/22 家**不一致；极端样本 `reports/工业富联(601138)`：`created_at` 最新 = **2023 年年度报告**，而报告期最新 = **2026 年半年度报告**（恰是 12 份里 `created_at` 最旧的一份）⇒ 按字面读法窗口会接进**两年前的陈旧年报**，§11 验收 3 的语义「**期间出现的**新财报被接进窗口」在真实数据上落空（而 §15.1-G 的字面断言仍会假绿）。修正后真实数据 **21/22 家**取到窗口（唯 `300274` 仅解析到 2026 年一季度报告，无白名单叙事章节）。**章节匹配与文本边界（两条已文档化约定）**：① 白名单是**子串**匹配 ⇒ 真实标题 `## 十、 重大风险提示` 命中 `risk`（预期）；② 文本边界 = **子树**（F8 口径变更）：命中章节的文本延伸到**下一个同级或更高级标题**为止（父章节**包含**其全部子标题段落），父子都命中时**子树只计一次**；`chars == len(text)` 恒成立且 `max_chars`/`max_sections` 仍是**硬上限**（超预算的子树被截断到剩余预算）。**变更理由（可复现）**：叶边界下父章节常因自身正文为空被丢弃 + 白名单只匹配标题 ⇒ 命中多为 `## （二） 非主营业务导致利润重大变化的说明` ＋ `□适用 √不适用` 这类占位行；枚举命令 `find reports -name .report_manifest.json | wc -l` = **142 份 / 22 家**，按 `as_of=2026-12-31` 逐家 `select_report_window` 累加 `chars`：**V1 叶边界 88,164 字符（限定作用域 `find reports -name .report_manifest.json | wc -l`=142 份/22 家，命令见上；全库全文 0.48%）→ V2 子树边界（未加分配规则）251,016 字符 → V3 等额分配后 174,732 字符**（captain 引用的 V1=102,461 / V2=251,073 与本次实测口径有差，V1 差因枚举作用域（本次仅 `reports/` 下 22 家、重放旧叶边界算法）与统计包含范围不同，V2 差 57 字符为重建写法差异；22 家结论一致），薄窗口（<1000 字符）**5/22 → 1/22**（唯 `300274` 无白名单叙事章节），`601138` **253 → 12,000（truncated=True）**。**为什么不是「切得更浅 / 再剔噪声子树」**：实测「子树边界」与「子树再剔噪声子树」**逐字符相同**（减 0 字符）⇒ 噪声来自标题级白名单本身，故不新增排除规则/阈值。**代价（显式登记）**：① 子树会带入章节内部的数值表格（由白名单 + 预算双重约束兜住）；② **组间覆盖塌缩**：`mda` 组先取且单棵子树即可吃满预算 ⇒ 实测 **20/22 家**窗口只剩一棵被截断的 `mda` 子树（`business`/`risk` 因预算耗尽不再入选），叶边界下多数家可同时含 `business`+`risk`；**旧"顺序吃满"下的塌缩（历史）**：实测 20~21/22 家只剩 1 组、`risk` 0/22；**已由裁定 C 的等额分配 + 余额顺延修复**（`risk` 10/22、≤1 个组 **11/22**（含 1 家无窗口的 `300274`）；以有窗口 21 家为分母 = **10/21**）；本行**未**新增 `boundary`/配额类配置项（"不加开关"与"分配规则已改"不冲突）；③ **组归属与去重 = 顶层声明制（F8 裁定 A）**：自上而下遍历标题树，命中节点**认领整棵子树**且**不再向内层认领**，子树文本归属**认领者所在的组**（子树内的其他关键词不产生第二次认领）⇒ 一条文本只入窗一次、不重复计 `chars`；`max_sections` 计**被认领的章节数**（一棵巨大子树仍算 1 段，裁定 B）。**预算分配 = 按"有内容的命中组"等额切分 + 余额顺延（F8 裁定 C，覆盖早前"顺序吃满"口径）**：每组份额 `max_chars // 命中组数`（整除余数同样顺延），组内按文档序取章节、每段截到该组额度，未用完的余额顺延给下一组；**未引入任何魔法常量**（无 800 字符保底、无"每段 ≤60%"，遵守 §15.0 C-5），总消耗恒 ≤ `max_chars`。**修正了什么（22 家实测，`as_of=2026-12-31`）**：旧"顺序吃满"下 `mda` 首棵子树一票吃满 12,000 ⇒ `risk` 组 **0/22 家**有内容、**只剩 ≤1 个组 21/22 家**、`max_sections=12` 形同虚设；等额分配后 → **`risk` 恢复到 10/22 家**、**只剩 ≤1 个组降到 11/22 家**；**组别构成**：`mda` 164,000 字符（93.9%）/ `business` 8,590（4.9%）/ `risk` 2,142（1.2%），合计 **174,732 字符**；**取舍（如实登记）**：① 大子树被截到**其组份额**（单段不再是完整子树），全库总量由 251,016 降为 174,732（用总量换组覆盖）；② **残余**：11/22 家的风险标题（如 `十、 公司面临的风险和应对措施`）**落在被认领的 `mda` 子树内**，按顶层声明制归属 `mda`、不再单独认领（另 1/22 家 `300274` 报告内无 risk 白名单标题）⇒ 这些家的风险文本受 `mda` 份额截断影响；要改归属须动顶层声明制，属**另一次口径变更**，由 captain 决定；③ 判别性对照：把等额分配改回"先到先得吃满" ⇒ `test_budget_split_equally_across_hit_groups` / `test_unused_group_share_carries_forward_to_next_group` / `test_real_sample_601138_thickness_regression` **必红**；去掉余额顺延 ⇒ 顺延用例**必红**；⑤ **两条不变量已加钉子并实测判别力**：① 总字符 ≤ `max_chars` —— `test_total_chars_never_exceeds_max_chars_invariant`（`max_chars∈{10,100,999,4000,12000,50000}` 参数化）+ `test_total_chars_invariant_holds_for_whole_local_corpus`（真实全库 22 家 × 默认 12000）；② `risk` 组贡献 > 0 —— `test_real_sample_risk_group_nonempty_under_equal_split`（601138，另断言标题为 `十、 重大风险提示`）。**实测判别力（两个变异分别验证）**：忠实复现旧 FCFS（`room = max_chars - used`，单一全局剩余预算）⇒ risk 钉子**变红**、total_chars 钉子**仍绿**（后者守的是"上限性质"本身）；把每组份额膨胀为 `share = max_chars`（每组合格配额各自等于总预算）⇒ total_chars 钉子**变红**。④ 未新增配置项（无 `boundary` 开关），行为仍由 `report_window.enabled` 统一门控；④ 判别性对照实验：把 `_subtree_span` 改回叶边界（`return index + 1`）后，4 条 F8 钉子（`test_section_text_boundary_is_subtree` / `test_subtree_boundary_stops_at_same_or_higher_level_heading` / `test_empty_body_parent_keeps_its_subtree` / `test_real_sample_601138_thickness_regression`）**全部转红**（1 passed 的 `test_truly_empty_section_is_skipped` 与边界无关）。**窗口内容面**：本地存在该标的已解析财报时 `window_texts` 由 1 条冲突解释变为「原文（≤`max_chars` 字符 / ≤`max_sections` 段，章节白名单 mda→business→risk）+ 冲突解释」，Stage A/B 首次真正生效（§11 验收 3 由 ⬜ → ✅；注：`601138` 最新年报的父章节「第三节 管理层讨论与分析」自身正文为空，故该标的窗口为 5 段 / 253 字符，属白名单+边界约定的预期结果）；**可观测面**：无报告 ⇒ 新增 `report_window_unavailable`（F4 文案修正：按真实上位条件二分支 —— `window_texts is None` 时「本次仅数值类证据参与方向判定」，已验证冲突解释非空时「本次定性判定仅依赖已验证冲突解释」，不再无条件断言"仅数值类证据"）（D1/MEDIUM/`scope=data`/`recovery_action=numeric_only`）、预算截断 ⇒ 新增 `report_window_truncated`（D1/LOW），两 category 已登记 `CLASS_BY_CATEGORY`；⇒ `no_local_report`（无本地报告的新标的）与 `no_matching_section`（只解析到季报的标的）**每次 run 各 +1 条 MEDIUM 偏离**，会抬高 §11 检测率/偏离预算消耗（**设计口径如此，未新增判定与阈值**，如后续要降到 LOW 属另一次行为变更）；**零回归保证（F1 修复后可复算）**：`report_window.enabled=false` ⇒ **开关同时门控「窗口内容」与「D1 记账」**，`_window_texts()` 逐字回到 `_conflict_explanations(artifacts) or None` **且不读盘**（`select_report_window` 的 `enabled=False` 分支位于任何读配置/读盘之前），亦不新增任何 issue；坐实用例 `test_switch_off_window_texts_equal_legacy_and_no_side_effects` **不 stub** `select_report_window`，并以 "`reports_root` 调用即断言失败" 作反 stub 哨兵 + 真实同 code 报告根 ⇒ 对 M5 型（开关未透传）变异敏感；**复算命令与期望值（均不 stub `select_report_window`）**：`pytest tests/orchestrator/test_report_window.py -k switch_off -q` → 期望 `1 passed`（内容逐字等价）；`-k disabled_selection_reads_nothing -q` → 期望 `1 passed`（不读配置）；`-k report_window_enabled -q` → 期望 `2 passed`（配置不可读 ⇒ 回落 False）；`-k as_of_excludes_not_yet_ingested_manifest -q` → 期望 `1 passed`（F5 前视消除）；上述任一处期望不成立即复现 M5 型变异；缺 `report_window` 段的旧 `config.yaml` 仍可 import（`Settings` 默认值保证；该旧配置下窗口**照常生效**，即默认行为变更）；配置**不可读/非预期**时开关回落 `False` ⇒ 经同一透传路径逐字回到旧实现且不新增任何 issue（有专测坐实 "`enabled=false` 不读配置、不读盘"）；窗口选择纯规则/无网络/无 LLM/单次 7–38ms（1.4MB 全文）；门禁：`tests/orchestrator/test_report_window.py` **58 用例全绿**（顶层函数 55 / `test_*` 52 / 收集数 58）、三目录 **667 passed**（基线 607）、`tests/midterm` 375 passed 零回归、`ruff check` + 固定版 0.15.0 `format --check` 干净、`mypy alphabee` 259 files 零问题、契约不变量 16 / `90bd2190b86f1186` / 6 / `validate_contracts()==[]`；测试面加固（captain 于同日补入本提交）：`_EXPECTED_CLASS_BY_CATEGORY` 补 2 个新 category、`tests/orchestrator/test_midterm_node.py` 的 2 例窗口断言改为"无本地窗口"替身，消除对 `reports/` 磁盘状态的依赖；**文案偏差登记（相对 §15.1-C 规格片段）**：① `report_window_unavailable` 文案按真实上位条件**二分支**（`window_texts is None` ⇒「本次仅数值类证据参与方向判定。」；否则 ⇒「本次定性判定仅依赖已验证冲突解释。」）—— §15.1-C 原文为无条件的"…，本次仅数值类证据…"；② `report_window_truncated` 文案改为「财报原文窗口被组份额截断（`{chars}` 字符）。」—— §15.1-C 原文为"被预算截断"，而裁定 C 下截断由组份额（非总预算）引起（实测 601138 = 4,101 < 12,000）。**设计文档为权威规格，本轮不改**，两处偏差在此登记。**影响面登记（裁定 (a)：维持逐 run 记账）**：等额分配 + 子树边界下 `truncated` 几乎恒真 —— **有窗口 21 家全部截断（21/21；含 1 家无窗口时 21/22）** ⇒ 每次窗口化 run 新增一条 `report_window_truncated`（D1/LOW）；属本口径直接后果、非缺陷；要降频须改判据（另需批准）。**本行描述的实际提交面 = 9 条路径（认证快照 `tmp/certified/p1_prereview.sha256.txt`，逐路径完整 sha256）**：member 侧 7 条（`alphabee/orchestrator/services/report_window.py`(新增)/`alphabee/orchestrator/nodes/midterm.py`/`alphabee/orchestrator/services/deviation.py`/`alphabee/config/__init__.py`/`config.yaml.example`/`tests/orchestrator/test_report_window.py`(新增)/`docs/roadmap/ROADMAP.md`）+ captain 侧 2 条测试文件（`tests/orchestrator/test_deviation_service.py`：补 `_EXPECTED_CLASS_BY_CATEGORY` 2 个新 category；`tests/orchestrator/test_midterm_node.py`：2 例窗口断言改「无本地窗口」替身）—— 两者均为**本提交面**的一部分，不剥离（剥离会使提交树上的覆盖守卫用例转红）。 |
| 2026-09-22 | 研究连续体 P2（D2-B2）：新增 `deviation.tracking` 段（`block_stale_runs` **新增阻断行为**；`stale_after_days=7` / `tv_distance=0.3` / `evidence_rate=0.5` **显式登记既有默认，值不变**；`max_alerts_shown=20` 只服务只读视图）；新增 `orchestrator/services/preflight.py`（`PreflightVerdict` + `check_preflight`，纯规则/无网络/无 LLM/永不抛）；`collectors.collect_raw_facts` 命中即**恒**记 `stale_state_run`（D5/MEDIUM/`scope=planning`/`recovery_action=proceeded_without_reconcile`，`detected_at_step=collect_raw_facts`）；CLI 新增 `--allow-stale` / `--track-alerts` | `docs/design/RESEARCH_CONTINUUM_DESIGN.md` §14.3 D2（B2 定案）与 §15.2 / §15.7 明文要求本段登记；§11 验收 2 口径已修订为「**不可能静默地**带过期状态继续」；§14.3 D2 依据核实 `data/tracking/alerts/*.jsonl` **全仓零消费者**（dead-end，必须修） | ① **CLI 交互入口**：该标的最新帧 `stale_after` 到期，或告警末行存在未消费触发（`triggers`/`exit_reasons`/`monitor_reasons` 非空）⇒ `SystemExit(3)`（"未执行"退出码，非错误码 1），`--allow-stale` 显式放行；② **记账与阻断解耦**：放行**不**取消记账 —— 记账在 `collect_raw_facts` 侧**恒发生**（§15.2 设计决策 3），故 `--allow-stale` 之后 state/账本仍留痕；命中时 `collect_raw_facts` 的 Step 由 `SUCCEEDED` 变 `PARTIAL`（既有 `_finalize_step` 语义：有 issue 有产物）；③ `--track-alerts [SYMBOL]` 是**只读视图**（不触发 run、不写库、不改编排；省略 SYMBOL 列出全部标的），修复 alerts dead-end；④ **阈值读取面变化**：`thresholds_from_settings()` 由"段不存在 ⇒ `TriggerThresholds()`（`tv_distance=None`、`monitor_kwargs()` 返回 `{}`）"变为**显式读出本段**（`{"tv_threshold": 0.3}`）；因 `monitor_triggers` 的默认值**就是** `midterm.diff_consumers._TV_TRIGGER`，**有效判定逐字不变**（对拍用例 `test_tracking_thresholds_explicit_registration_keeps_monitor_verdict` 在 4 档 `tv ∈ {0.0,0.29,0.3,0.4}` 上断言"不传参"与"显式传本段值"产出相等 `MonitorTrigger`）；**唯一可观测面差异**：`detect_triggers` 的 `PRICE_MOVE` payload `threshold` 由 `null` → `0.3`（既有用例 2 处同步更新：`tests/tracking/test_triggers.py::test_kind_price_move_from_belief_drift_comes_from_monitor_triggers` 与 `test_thresholds_default_section_registers_existing_defaults` —— 后者原名 `..._section_absent`，其"段不存在"前提已不成立）；⑤ **新增 category**：`stale_state_run` 已登记 `CLASS_BY_CATEGORY`（D5），覆盖守卫冻结期望表同步 1 行（否则 `CLASS_BY_CATEGORY == _EXPECTED_CLASS_BY_CATEGORY` 的既有严格等式转红）；⑥ **零回归面**：本机 `data/midterm/state/` **不存在**（无持久化帧）⇒ `checked=False` ⇒ 既不产 issue 也不阻断；**无帧（首次研究）恒放行**（§15.2 设计决策 2，即便告警文件已有未消费触发也不阻断）；告警末行损坏/非对象行只跳过、不抛；`block_stale_runs=false` ⇒ 只记录不阻断；回滚 = `block_stale_runs=false` + 移除 CLI gate 调用（视图独立可留，§15.2-E）；⑦ **规格措辞偏差登记（相对 §15.2-B 片段）**：片段写作 `check_preflight(symbol, block_enabled=False)` 后判 `if verdict.blocking:`；实现口径为 `blocking = checked∧(stale∨pending)∧block_enabled`（`block_enabled=False` ⇒ 恒 `False`），故记账取"命中"判据（`checked and (stale or pending)`）—— 否则 §15.2 设计决策 3 的"记录恒发生"落空；另：`latest_frame_id` 按 `midterm.persistence._artifact_id` 同规则（`symbol:as_of_date`）重建（持久化层不把 id 放进模型）；`days_since` = **帧龄**（`today − as_of_date`，与 `stale_after` 非同一口径，不做非负夹取）；**设计文档为权威规格，本轮不改**；⑧ **可复算口径**：`pytest tests/orchestrator/test_preflight.py -q` → **61 passed**（顶层函数 59 / `test_*` 47 / 收集数 61）；`pytest tests/apps -q` → **12 passed**（顶层函数 18 / `test_*` 12 / 收集数 12）；三目录 `tests/orchestrator tests/tracking tests/core` → **729 passed**（基线 607；P1 落点 667）；`tests/midterm` → **375 passed** 零回归；契约不变量 16 / `90bd2190b86f1186` / 6 / `validate_contracts()==[]`；⑨ **本行描述的实际提交面 = inScope 9 条路径 + 越界披露 4 条（测试面 3 + 构建配置 1）**：`tests/orchestrator/test_deviation_service.py`（覆盖守卫期望表 1 行）、`tests/tracking/test_triggers.py`（2 例按上文④改判）、`tests/apps/{__init__,test_cli_preflight}.py`（新增 CLI 入口级用例）—— 三者均为**本期 verify 命令能否通过**的直接前提（覆盖守卫的严格等式、三目录门禁、`pytest tests/apps`），不剥离；第 4 条 = `pyproject.toml`（`[tool.ruff.lint.per-file-ignores]` 追加 `"alphabee/apps/web/__init__.py" = ["E402"]`，见下文⑩，经 captain 裁定 (A) 授权）；**认证快照 `tmp/certified/p2_prereview.sha256.txt`**（逐路径完整 sha256，**14 条**路径在冻结 444 后经 3×9s 零写入采样一致；路径清单由命令枚举 `git status --porcelain` 展开，非手写）；⑩ **门禁既有红点（非本期引入，本期已修复）**：`ruff check alphabee tests` 原先因 `alphabee/apps/web/__init__.py:25` 的 **E402**（`load_dotenv()` 先于 import）报 1 个 error —— 该文件随 `8e825b5` 入库、`git show HEAD:alphabee/apps/web/__init__.py` 同样报错，证其属既有、非本期引入；经 captain 裁定 (A) 授权，本期在 `pyproject.toml` 的 `[tool.ruff.lint.per-file-ignores]` 追加 `"alphabee/apps/web/__init__.py" = ["E402"]`（比照既有 `main.py = ["E402"]` 行，同理由：`load_dotenv()` 必须先于依赖环境变量的 import）⇒ 修复后 `ruff check alphabee tests` → **All checks passed!**（exit 0）；`alphabee/apps/web/__init__.py` **本身未改**（其前置加载是文件注释明示的有意行为，改 import 顺序会改变 .env 加载时序）；固定版 `format --check` 另报 2 个**既有**未格式化文件（`tests/midterm/test_factors.py`、`tests/midterm/test_insight_evidence_adapter.py`，本期未动，故全仓 `format --check` 仍非全绿，本期改动路径则为 11 files already formatted）；修复**前**的等价证据已留档：`ruff check alphabee tests --exclude alphabee/apps/web/__init__.py` 与 `--per-file-ignores "alphabee/apps/web/__init__.py:E402"` 两种形式当时均 → **All checks passed** |
| 2026-09-22 | 研究连续体 P3（D1-A2）：新增 `tracking/ledger.py`（`TRACKING_RUN_PREFIX="track"` + `tracking_run_id` 前缀约定（`track:<symbol>:<as_of>`，不加表列，17 列表结构不动）+ `tracking_issues` 5 行投影（`deviation_class` **显式给定**，不依赖 category 惰性回退）+ `record_tracking_deviations` fail-open 返回条数）；`tracking/scheduler.py::run_once` 在 `_write_alerts` 后、`return` 前 `if persist:` 写账本；`TrackingReport` 只 append `deviations_recorded: int = 0`（向后兼容）；5 个新 category 登记 `CLASS_BY_CATEGORY`；`services/telemetry.py` 的 `latest_run_id()` 跳过 `track:` 帧、时间线对跟踪帧整块行首加 `[track] ` | `docs/design/RESEARCH_CONTINUUM_DESIGN.md` §14.3 D1（跟踪路径可观测）与 §15.3（D1-A2）明文要求 | ① **账本副作用（新）**：`persist=True` 时每帧 0–5 行（投影命中数）写 `deviation_events`，复用既有指纹去重/upsert（同标的连续同类帧 ⇒ `occurrence_count` 递增、`run_id` 刷新）；`persist=False` **零写入**（`deviations_recorded==0`，专测坐实，与帧落盘同一开关）；DB 不可用（monkeypatch `record_event` 抛异常）返回 0 不抛（fail-open）；② **5 个新 category**（`tracking_exit_signal`/D3/HIGH/`escalate`、`tracking_drift_trigger`/D4/MEDIUM/`deep_research_due`、`tracking_contradiction_forced`/D3/MEDIUM/`forced_accounting`、`tracking_frame_degraded`/D2/MEDIUM/`degraded_frame`、`tracking_frame_skipped`/D5/LOW/`diff_skipped`）已入 `CLASS_BY_CATEGORY`，覆盖守卫冻结期望表同步 +5 行（**越界披露 1 条**：剥离会使既有严格等式转红，与 P1/P2 同类处置）；③ **`latest_run_id()` 语义变更**：只返回非 `track:` 前缀 run（跟踪帧不抢占"最近一次分析 run"；查看跟踪帧需显式传 `run_id`），`--deviations` 时间线对 `track:` 帧整块行首加 `[track] `（telemetry **不 import tracking**，依赖方向不反向）；④ 门禁：`tests/tracking` **119 passed**（新 `test_ledger.py` 40 例：顶层函数 40 / `test_*` 32 / 收集数 40）、三目录 **774 passed**（基线 607，P2 729 ⇒ +45 = 新增 40 + 覆盖守卫 parametrize +5）、`test_telemetry.py` 82 passed 零回归、`ruff check` 全绿、固定版 0.15.0 `format --check` 本期 7 路径全绿、`tests/midterm` 375、契约不变量 16/`90bd2190b86f1186`/6/`[]`、变异 **8/8 KILLED**（M1 首轮存活已换对照式用例重杀；破报告用例揭出 except 分支诊断日志重抛真缺陷，已用 `_safe_str` 修掉）；⑤ **实际提交面 = 8 路径**（t7 inScope 6 + 越界披露 1（`tests/orchestrator/test_deviation_service.py` 覆盖守卫）+ captain 侧 1（本行 ROADMAP 登记，t7 的 docs/ outOfScope）），认证快照 `tmp/certified/p3_prereview.sha256.txt`；⑥ **两条顺延项（captain 裁定 2026-09-22，详见顺延项表 P3-L1 / P3-L2）**：reconcile 异常早退分支**不额外入账**（最小实现）；**不**与 `deviation.ledger.enabled` 联动（§15.3-B 开关 = `persist`） |
| 2026-09-22 | 研究连续体 P4（D3-C2）：新增 `tracking/status.py`（`ResearchStatus` 五态（**无 ACTIVE**）+ `research_status(*, exit_reasons, monitor_reasons, triggers, stale)` 纯投影 + 四个**同源前缀常量**）；`TrackingReport` 只 append `research_status: str = ""`；`run_once` 在 `_write_alerts` **之前**写入（降级/异常早退保持 `""`，不猜）；`tracking/__init__.py` 导出；`apps/cli/main.py` 的 `--track-alerts` 渲染 `状态=<派生状态字>`（与 `认知状态=Sx` 并列，旧帧回落认知状态，仍纯读） | `docs/design/RESEARCH_CONTINUUM_DESIGN.md` §14.3 D3（C2 派生视图）与 §15.4 明文要求；§5「缺口」随本期转为「**可选增强已交付（派生视图）**」 | ① **五态优先级表**（`INVALIDATED`→`RISK_ALERT`→`THESIS_CHANGED`→`NEEDS_RESEARCH`→`WAITING`，序即优先级、互斥，同命中取序小者）逐字复用 §15.4-A 表；**同源驱动（关键要求）**：用真实 `check_exit` / `monitor_triggers` / `detect_triggers` / `detect_fact_triggers` 输出逐字比对前缀常量（`"退出条件触发"` / `"状态降级"` / `"tv_distance="` 等提为模块常量 + 同源断言用例）；**无第二份阈值**：边界同源坐实（`tv == _TV_TRIGGER` 不触发、`+0.001` 才触发 ⇒ 判据完全由既有常量决定）；② **无 config / 无 IO**：模块只 import `{__future__, enum, collections.abc, alphabee.tracking.triggers}`（不 import `get_settings`，§15.4-C「无阈值引入」）；③ **两条保守投影（captain 裁定 2026-09-22：维持，已在模块 docstring 逐字披露）**：只由 `evidence_arrival_rate=` 引起的 monitor reason ⇒ 状态字维持 `waiting`（优先级表第 3 序只认 `tv_distance=` 前缀，扩围属改规格、未偷加判据）；只由人工确认闸产生的 `MANUAL` 触发 ⇒ 同样不改状态字（该信息已由 `blocked_actions`/`escalation_tier` 携带）；④ 门禁：`tests/tracking/test_status.py` **42 passed**（顶层函数 34 / `test_*` 28 / 收集数 42）、`tests/tracking` **161 passed**（P3 119 + 本期 42）、三目录 **816 passed**（607→667→729→774→816 零回归）、`tests/apps` 12（main.py 改动对越界面零回归）、`tests/midterm` 375、`ruff check` 全绿、固定版 0.15.0 `format --check` 5/5、契约不变量 16/`90bd2190b86f1186`/6/`[]`、变异 **7/7 KILLED**（M1 优先级塌陷/M2 前缀改子串/M3 类别集清空/M4 接线恒写空串/M5 赋值挪到落盘后（落盘帧缺字段）/M6 忽略 stale/M7 CLI 丢派生字）；端到端第二帧财报刷新 ⇒ 落盘帧 `needs_research` 实测成立；⑤ **实际提交面 = 6 路径**（t10 inScope 5（`tracking/status.py`(新)/`tracking/scheduler.py`/`tracking/__init__.py`/`apps/cli/main.py`/`tests/tracking/test_status.py`(新)，**本期无越界披露**）+ captain 侧 1（本行 ROADMAP 登记，t10 的 docs/ outOfScope）），认证快照 `tmp/certified/p4_prereview.sha256.txt`；回滚 = 删除 `run_once` 赋值与 CLI 展示行 |
| 2026-09-22 | 研究连续体 P5（W5）：新增 `midterm/versions.py`（`DEFAULT_VERSION_DIR = data/midterm/thesis_versions`、`_version_id = "{as_of_date}#{version}"`、`append_version` 按 id 幂等、`load_versions` 升序 + 坏行跳过、`latest_version`、`register_thesis_if_changed` + 三 reason 常量）；`tracking/scheduler.py::_reconcile_frame` 在落帧之后、**仅 `persist=True`** 时登记版本（接线侧双保险 except）；`_Frame` 只 append 两字段（带默认值）；`TrackingReport` 只 append `thesis_version: int = 0` / `thesis_version_reason: str = ""`（向后兼容已钉）；**不改 `midterm/models.py`**（`ThesisVersion` 无 symbol 字段，标的由文件名承载，不动契约面） | `docs/design/RESEARCH_CONTINUUM_DESIGN.md` §3（W5）与 §15.5 明文要求；§11 验收 5（W5 部分）随本期转 ✅ | ① **反漂移语义（核心）**：首版 `version=1`/`buy_rationale=[thesis]`/`invalidation=list(invalidation)`；后续版本 `version=prev+1` 但 **`buy_rationale` 与 `invalidation` 均继承首版**（原始买入理由与证伪条件不可被行情/叙事重写）；三版连变后首版理由不变；未变 ⇒ 不追加（行数不变）；同 id 重复 append 幂等（仍 1 行）；历史行 `buy_rationale` 为空时回落首版 thesis；② **接线位置差异（规格明文，captain 裁定 2026-09-22：维持按规格，不统一）**：§15.5-B 把登记放在 `_reconcile_frame` **内核内** ⇒ `reconcile(persist=True)`（公开"纯推进内核"入口）也会登记版本，与 P3（§15.3-B 放 `run_once` 上层、账本写库不进内核）不同——两者均按各自规格片段执行，未"顺手对齐"（对齐会偏离 §15.5-B 明文）；已在代码注释与用例 docstring 逐字登记；③ **容错口径**：`persistence._read_rows` 的坏行跳过口径在本模块复制 3 行实现（不 import 同层私有名），两处分别有用例钉住，已在模块 docstring 披露；④ 门禁：`tests/midterm/test_versions.py` **24 passed**（顶层函数 30 / `test_*` 24 / 收集数 24）、`tests/midterm` **399 passed**（P4 落点 375 + 24 零回归）、三目录 **816 passed**、`tests/tracking + tests/apps` 173 passed（scheduler 改动越界面零回归）、`ruff check` 全绿、固定版 0.15.0 `format --check` 3/3、契约不变量 16/`90bd2190b86f1186`/6/`[]`、变异 **8/8 KILLED**（M1 反漂移失效/M2 invalidation 不继承/M3 未变也追加/M4 版本号恒 1/M5 拆 persist 门/M6 去接线 fail-open/M7 不转字段/M8 幂等失效）；⑤ **实际提交面 = 4 路径**（t13 inScope 3（`midterm/versions.py`(新)/`tracking/scheduler.py`/`tests/midterm/test_versions.py`(新)，**无越界披露**；`tracking/__init__.py` 虽在 inScope 但 P5 无需再导出故未改）+ captain 侧 1（本行 ROADMAP 登记，t13 的 docs/ outOfScope）），认证快照 `tmp/certified/p5_prereview.sha256.txt`；回滚 = 不调用 `register_thesis_if_changed`（新目录可安全保留或删除） |
| 2026-09-22 | 研究连续体 P6（§8）：新增 `tracking/engine.py`（`ResearchContext` 7 字段 / `ResearchOutput` 6 字段 / `ResearchEngine` Protocol，**零 orchestrator 依赖**）、`tracking/engines/__init__.py`、`tracking/engines/pipeline_engine.py`（`PipelineEngine` + `to_output` 映射，**方法体内延迟 import**、fail-open）；`tracking/__init__.py` 导出协议三件套（**刻意不含适配器**）；`collectors.py` 的 **§15.6-C 最小修法**：已有 `run` ⇒ 复用并合并 context（保留 id/goal/status/started_at，只在解析出标的时覆盖 symbol），无传入 run ⇒ 原分支语义逐字不变 | `docs/design/RESEARCH_CONTINUUM_DESIGN.md` §8 与 §15.6 明文要求；§11 验收 8（L2 协议部分）随本期转 ✅ | ① **延迟 import（本门最硬的可证伪钉子）**：子进程实测（3 模块参数化）——`HOME` 指向空 tmp 后 import → `orchestrator.collectors` / `orchestrator.agent` / `tushare` 均不在 `sys.modules` 且 `$HOME/tk.csv` 未被创建；AST：模块级无 `alphabee.orchestrator`、方法体内有 ⇒ 变异 M1（提到模块级）两类用例同时转红；② **注入边界**：只注 `symbol`/`as_of`/`thesis`（作 `thesis_prior`）/`prior_confidence` 四键；**不注入** `evidence_ids`/`open_questions`（主图当前无消费者，注入即 dead-end；M3 注入 ⇒ 转红）；`collectors.py` 复用/合并分支保证注入不被覆盖（M4 run 覆盖/M5 symbol 覆盖 ⇒ 转红，回归测试已加）；③ **v1 明确非目标（防范围膨胀）**：不重构主图为插件系统、不改 `agent.py` 图结构、不改 `NODE_ORDER`（实测恒 16）、不接入外部引擎（MiroThinker/MiroFlow/Tongyi）——只交付协议 + 现有实现适配器 + stub 引擎测试作为替换接缝；④ 门禁：`tests/tracking/test_engine.py` **24 passed**（顶层函数 26 / `test_*` 21 / 收集数 24）、`tests/tracking` **185 passed**（P5 161 + 24）、三目录 **840 passed**（607→667→729→774→816→840 零回归）、`tests/apps` 12、`tests/midterm` 399（collectors 改动越界面零回归）、`ruff check` 全绿、固定版 0.15.0 `format --check` 6/6、契约不变量 16/`90bd2190b86f1186`/6/`[]`/NODE_ORDER 16（未变）、变异 **8/8 KILLED**（M1 延迟 import/M2 丢 thesis_prior/M3 注入 evidence_ids/M4 run 覆盖/M5 symbol 覆盖/M6 不判降级/M7 summary 取错键/M8 引擎异常不 fail-open）；⑤ **实际提交面 = 7 路径**（t16 inScope 6（`tracking/engine.py`(新)/`tracking/engines/__init__.py`(新)/`tracking/engines/pipeline_engine.py`(新)/`tracking/__init__.py`/`orchestrator/collectors.py`/`tests/tracking/test_engine.py`(新)，**无越界披露**）+ captain 侧 1（本行 ROADMAP 登记，t16 的 docs/ outOfScope）），认证快照 `tmp/certified/p6_prereview.sha256.txt`；回滚 = 删除 `engines/` 子包（协议模块无副作用可留）；⑥ **具名顺延项（见顺延项表 P6-L1）**：`evidence_ids`/`open_questions` 形状已定、主图消费者待后续期按 steward 流程接线（§15.6-C 明确非目标） |
| 2026-09-23 | tracking 调度：thesis 版本目录由「恒定 `data/midterm/thesis_versions`」改为「`state_dir` 非 None ⇒ `<state_dir>/thesis_versions`；`state_dir=None` ⇒ `data/midterm/thesis_versions`（逐字不变）」（RC-5 收口） | ROADMAP 顺延项表 RC-5（P5 遗留：`--state-dir` 不隔离版本目录 + 单测非密闭性）；§8.2 规则 3 行为变更登记 | 影响面：**缺省路径与行为零变化**（`data_dir=None` 分支逐字不变，回归用例 `test_data_dir_none_resolves_to_verbatim_default_path` 钉住）；显式 `--state-dir` 用户的新行为 = 版本随状态目录落位（原写默认目录）——属 RC-5 预期修复；回滚 = 删除 `_reconcile_frame` 的 `data_dir` 透传（隔离用例 `test_reconcile_writes_thesis_versions_under_state_dir` 即转红） |
| 2026-10-04 | 交叉核对修复 P0-1：`alphabee/orchestrator/services/company_context.py::_detect_market_cap` 市值换算由 `/1e8` 改为 `/1e4`（`MarketFacts.market_cap` 存储单位是**万元**（`models.py:254` 注释 + `market_fact.py` 直取 tushare daily_basic 原值、渲染端 `/10000` 显亿元），万元→亿元 = `/1e4`；500/100 亿元阈值语义不变） | 用户外部数据交叉核对：Tushare 实测 002916 深南电路市值 2541 亿元被判 `small`（原代码把万元当元处理，阈值 100 亿元等效成 100 万亿元） | 影响面：**全部 A 股的 `market_cap_category` 将由恒 small 变为真实分档**（large ≥500 亿 / mid ≥100 亿 / small 其余），属 bug 修复；回归钉 = `tests/orchestrator/test_company_context.py::test_market_cap_category_regression_pegs`（002916=2541 亿→large、300037=700 亿→large、603979=368 亿→mid，构造 `MarketFacts` 输入、不依赖外部数据）；回滚 = 把 `/1e4` 改回 `/1e8` ⇒ 三条断言必红（变异实验 M1 实测 **3/3 KILLED**） |

> **F5 行为变更复核（2026-09-18）**：除上表新增行外，F5 期未引入其它权重/阈值默认值 —— `_budget_limit`（= 工单里写的 `_resolve_d_max`，**F5 既有命名**，`git show e9fe18c:alphabee/orchestrator/services/telemetry.py:365`）只做读取，无写路径；CLI `--deviations` 是只读视图（§14.8 PR8：回滚方式 = 不调用即可）。`config.yaml.example` 与代码默认值经**全段叶子程序化比对**（15 个叶子 0 不一致），且旧 example（无 `d_max`）仍可构造并回落默认。

> **F4 行为变更复核（2026-09-18）**：F4 期**未引入任何新的权重/阈值默认值** —— `DeviationSettings` 至今没有 `tracking` 段，`tracking/triggers.py::thresholds_from_settings()` 是**运行时** fail-open 读取（段缺失/异常 → `None` → 全部回落 midterm 既有默认；tv 档**不传参**给 `monitor_triggers`，用其自身默认常量），故本期无需新增登记行。将来若给 `deviation.tracking` 段写入默认值，须回到本表登记。**（该条件已于 2026-09-22 由研究连续体 P2 满足：本表 P2 行即该段的登记行，`thresholds_from_settings()` 自此显式读出 `deviation.tracking`。）**

### 偏离控制框架顺延项登记

> 依据 `docs/design/DEVIATION_CONTROL_FRAMEWORK.md` §8.2／§14.8：设计有要求而 v1 未实现的点必须**具名登记**（写明「当前不可达／未实现」的依据与将来的收口条件），不得静默略过。

| 期 | 顺延项 | 当前未实现／不可达的依据 | 收口条件 |
|---|---|---|---|
| F3 | `review_report` 侧的加权边审计发射点 | F3 的实际发射点唯一 = `review_thesis`（已在 `orchestrator/node_contracts.py` 的 `requirement` 内显式登记「设计面 auditor vs 实现面实际发射点」）；`review_report` 侧为零接线的具名顺延 | 报告层接入放大审计时补齐 |
| F4 | **F4-L1 残余**：反证强制入账的「某侧只被**部分**引用」情形 | 已按**逐条**判定落地（`tracking/scheduler.py:321-339`：未被任何归因引用的每条证据各自补一条显式记账，`forced_sides` 同侧去重），并以 `test_partial_coverage_is_accounted_per_event` 钉住（变异非真空：退回旧「按侧 `any(...)` 守卫」该用例即报红）；但该情形在真实 midterm 路径上**当下不可达** —— `midterm/diff.py` 把 `new_evidence` 的全部 id 写进同一条归因 | midterm 改为按条归因后，该情形自动成为端到端可达路径（本模块无需改动，钉子已在位） |
| F4 | **F4-L2**：`exit_conditions` 求值器（§9.3） | v1 不新增第二份判定，只复用 midterm `diff_consumers.check_exit` 的既有投影；`exit_conditions_met` 触发经 `MANUAL` 类别上报（payload 自报 `source`） | midterm 暴露 exit_conditions 求值器后接入 |
| F4 | **F4-L3**：触发类型映射的代理标签 + 常驻循环 | `FINANCIAL_REPORT`/`ANNOUNCEMENT` 走 `*_proxy` 探测（payload 自报 `source`，不冒充确定来源）；`--loop` 是 §14.5-A 的**具名非目标**，CLI 显式拒绝（exit 2），不是待办缺陷 | 真实数据源接入后替换 proxy 探测；常驻循环另立一期 |
| F5 | **F5-D1** `recovery_half_life` 的 **resolved 端** | 现口径 = 「本 run 末位已执行节点序 − 检测端节点序」的**中位数**（模块 docstring 显式披露，用例钉 `{14,6,5} → 6.0`）—— 因为它测的是「检测 → run 末端」而非真正的「检测 → 解决」；根因：§14.1-C 的账本 17 列**已冻结、无 `resolved_at_step` 列**，而 `resolved` 本就是写入时快照 | 需要真语义时给账本加 `resolved_at_step`（须改 `deviation_store.py` 与 §14.1-C），属后续期 |
| F5 | **F5-D2** `budget_consumption` 的 `D_max` 无配置来源（**已关闭**） | 曾因 §14.6 的 `budget.d_max` 未落地而恒 `None`（F2「配置 vs §14.6」偏差） | **已由 `13d1b0d` 关闭**（生产路径 analysis `0.1667` / tracking `0.5` / 未知 `task_kind` `None`） |
| F5 | **F5-D3** §14.6 的 `budget.on_exceed`（`degrade \| escalate \| abort`） | 全仓 `grep on_exceed` **零命中** ⇒ 未实现；但**并非遗漏**：预算超限行为已由 **F2 的 `cost_exposure_threshold` 直接升级（T5）+ 阶梯裁决**承接，当前**没有 `D_cum > D_max` 的独立执行点**；按「不新增未接线字段」登记 | 出现"需要按 scope 选择超限动作"的真实需求时，接在 `_budget_limit` 的同一解析点上 |
| F5 | **F5-L1** per-symbol 画像**反哺** `resolve_industry_context` / `resolve_company_track` | §13 的 F5 描述提到该反哺，但 §14.8 PR8 明确 F5 是「只读、可独立合并、回滚 = 不调用即可」⇒ 反哺属**行为变更**，本期不做 | 另立一期，并带 config 开关（沿用"新行为由开关控制"纪律） |
| F5 | **F5-L2** §11.2 跨 run 指标（指纹复发率 / 降级率趋势） | 本期只做 §11.1 的**单 run** 指标；跨 run 聚合需要趋势口径与窗口定义 | 需要"系统性缺陷 vs 偶发漂移"判据时实现 |
| F5 | **F5-L3** §11.3 离线回放验证 | 用历史 run 落盘数据回放、比较"步数 H + 偏离指标集"对 run success 的预测力 —— 属**研究性**工作，非本期工程范围 | 指标数据积累后另立研究项 |
| F5 | **F5-D4** CLI 端到端**未在测试内真跑** | `apps/cli/__init__` → `main` → orchestrator 会拉起 `tushare.set_token`（写 `$HOME/tk.csv`、HOME 只读时 OSError）⇒ 测试改用「按文件路径加载 `args.py` 真跑 3 种 argv」+「`main()` 分派 AST 钉住」；复审侧另以**真跑 `main()` + 四入口哨兵**（`run_query`/`run_chat_session`/`run_framework_monitor`/`handle_task_cli` 全换一调即炸）验证**只读且真实可达** | 把 CLI 拉起链的副作用解耦（惰性 token 初始化）后，可恢复"测试内真跑 CLI" |
| 卫生 | `tests/midterm/test_factors.py`、`tests/midterm/test_insight_evidence_adapter.py` **既存未格式化** | 固定版 ruff 0.15.0 `format --check` 实测二者 would-reformat；**非本框架改动、未被触碰**，但它们是"**将来任何触碰它们的提交都会被 pre-commit 改写**"的同类陷阱（与 F5-1 同型） | 下次因其他原因动到这两个文件时顺带格式化（**勿**为它单独开提交、勿顺手格式化以免污染白名单） |
| P3 | **P3-L1** reconcile 异常早退分支是否也入账 | §15.3-B 接线点只覆盖正常返回路径（`_write_alerts` 之后、`return` 之前）；异常早退时帧未落盘、报告残缺，入账会记录不完整投影 | captain 裁定（2026-09-22）：**不额外入账**，维持最小实现；若后续需要"失败帧留痕"另开 P3.x 并做行为变更登记 |
| P3 | **P3-L2** 是否与 `deviation.ledger.enabled` 总开关联动 | §15.3-B 明示写库开关 = `persist`（与帧落盘同一开关：只读预演不得写库）；引入第二个开关属新增行为面，规格未要求 | captain 裁定（2026-09-22）：**不联动**，以 `persist` 为准；若需独立开关另开 P3.x 并做行为变更登记 |
| P6 | **P6-L1** `ResearchContext.evidence_ids` / `open_questions` 的**主图消费者接线** | §15.6-C 明示 v1 **不注入**这两键（主图当前无消费者，注入即 dead-end）；字段形状已在协议中定义（`list[str]`），`collectors.py` 的四键注入面已留出合并分支 | 主图出现证据跨 run 复用/未探索区域消费需求时，按 steward 流程登记 `OrchestratorState`/节点契约变更后接线（另立一期） |
| RC | **RC-1** 外部引擎接入（MiroThinker / MiroFlow / Tongyi） | §15.6-B v1 明确非目标：只交付协议 + `PipelineEngine` 适配器 + stub 引擎测试作为替换接缝，未重构主图为插件系统 | 有真实外部引擎需求时另立一期（须按 steward 流程登记图结构/契约变更） |
| RC | **RC-2** §14.3 D2 的 B3/B4（告警消费编排 / 联动动作） | 本期落地 B2（入口前置校验 + 只读视图）与记账；B3/B4 未做 | 需要告警驱动的编排/联动时另立一期并做行为变更登记 |
| RC | **RC-3** W1 普通 `--midterm` 不落帧 | §3 W1 设计预期：tracking 路径（`python -m alphabee.tracking`）已闭合并端到端实测，普通 midterm run 仍不持久化帧 | 若要让普通分析 run 落帧，另立一期并做行为变更登记（当前不属缺陷） |
| RC | **RC-4** mypy 严格模式收口（8 errors / 4 files） | **已关闭（t1 实现 + t2 复审 + t3 提交 `dc4c92e`，2026-09-23）**：`apps/web/api/{chat,health,sessions}.py` 7 条（`8e825b5` 引入，untyped-decorator/no-untyped-def/type-arg）+ `tracking/engines/pipeline_engine.py:102 call-overload` 1 条（局部 `# type: ignore[call-overload]`，延迟 import 未动）；修复全为注解/ignore 层，14/14 函数 co_code 与基线逐字节一致 ⇒ 运行时零变化；`poetry run mypy alphabee` = 277 files 零问题（pre-push 钩子口径可过） | 已关闭（`dc4c92e`）；pipeline_engine 的 call-overload 采用局部 ignore 而非类型重构，因 `initial_state()` 返回 `dict[str, Any]` 而图签名期望 `OrchestratorState` 的收窄需动运行时类型，留待需要时另立工单 |
| RC | **RC-5** `--state-dir` 不隔离 thesis 版本目录（单测非密闭性） | **已关闭（t4 实现 + t5 复审 + t6 提交 `690451a`，2026-09-23）**：`scheduler._reconcile_frame` 按 `_versions_data_dir(state_dir)` 派生版本目录（`state_dir` 非 None ⇒ `Path(state_dir)/"thesis_versions"`）并透传 `register_thesis_if_changed(data_dir=...)`；**缺省逐字不变**（`state_dir=None` ⇒ `data_dir=None` ⇒ `data/midterm/thesis_versions`，回归用例 `test_data_dir_none_resolves_to_verbatim_default_path` 钉住）；单测非密闭性消除：persist=True 且显式 state_dir 的用例版本随帧落位 tmp，不再触碰真实 `data/midterm/thesis_versions`（隔离用例 `test_reconcile_writes_thesis_versions_under_state_dir`，对「透传被删」变异敏感） | 已关闭（`690451a`）；行为变更见上方登记表 2026-09-23 行 |
| RC | **RC-6** 落盘帧 `deviations_recorded` 恒 0 | P3 按 §15.3-B 在 `_write_alerts` **之后**写账本 ⇒ 落盘帧快照早于记账，真值只在内存 report 与账本 | 若需要落盘帧携带真值，调整写入顺序（属行为变更，另立 P3.x）；当前 `--deviations` 视图不受影响 |

> **F4 测试侧双钉登记与归因更正（2026-09-18，reviewer 在 R15 后明确要求补落 docs）**：`tests/tracking/test_scheduler.py`（已随 `d65164f` 入库）含**两组等价钉住** —— 实现者「安全红线」段 `:660-767` 共 6 条 + captain 在 t72 补钉 `:773-847` 共 4 条。经 reviewer 逐条判定：**仅 `:806` 与 `:700`（gate 变异）为纯等价重复（low）**；`:821` 的 16 键**字面量**列表、`:773` 的 `PositionDecision` 另一形态、`:787` 的伪造 dict 变体 + `DSH_APPROVED` 环境变量为**增量**；`:714` 全域 AST 扫描为⑥段独有 ⇒ 两组**互补**，并集严格优于任一单组。结论：**low、可接受、不去重**（去重须再动已入库文件并移动锚点，成本高于收益）。**归因更正**：「首次把 F4-G2/P3 钉进仓库测试」的功劳在**实现者的⑥段**，captain t72 的实际增量为上述四项 —— t72 的任务记录已终态不可改写，故以此处为准。

### 偏离控制框架工程实践登记（本会话以真实事故固化的提交门纪律）

> 下列规则在 F0–F5 的实施中各被违反过至少一次并造成真实损失（返工、锚点失效、弱证据），故具名登记，供后续期次直接沿用。

| # | 规则 | 触发它的事故 |
|---|---|---|
| 1 | **基线必须是提交号**（如 `d65164f`），不得用 `HEAD`/工作区描述；证据块须含「锚定提交号 + 现提交号 + 生成命令原文 + 逐路径 SAME/CHANGED 计数」 | 多次出现"锚点在工作区漂移后失效" |
| 2 | **清单必须命令枚举**（`git show --name-only --pretty=format: <commit> \| awk 'NF'`），禁止手写清单 | 手写清单导致路径误计数 |
| 3 | **冻结协议**：review 前 `chmod 444`；提交前 `chmod 644`、提交后**立刻复冻 444** | 444 会让 pre-commit 的 `end-of-file-fixer` 抛 `PermissionError`；未冻结则 reviewer 无法认证（F4-B1） |
| 4 | **提交门标准动作**：`git add` 只用显式路径（禁 `-A/-u`）→ **暂存后、提交前**逐条核对 `git show :<path> \| sha256sum` == 认证值 → 提交 → `git show HEAD:<path>` 复算 + 路径枚举 + docs/外部改动命中数检查 → **复冻** | 把"钩子改写导致认证树 ≠ 提交树"提前到**提交前**拦截 |
| 5 | **门禁双跑**：`ruff check` **与** `ruff format --check`，后者用 **pre-commit 缓存内的固定版二进制**，且**只对 inScope 文件**判绿 | F5-1：只跑 `ruff check` 漏掉 format 漂移 ⇒ pre-commit 会改写已认证文件 |
| 6 | **认证基线保留区 `tmp/certified/**` 禁止清理**（各期的"复审前"与"提交态"两份都要留） | 基线副本被 tmp 清理后，reviewer 只能退回"无法逐字节对差"的弱证据 |
| 7 | **计数三口径同写**：顶层函数 / `test_*` / pytest **收集数**（`parametrize` 会让三者不相等） | 同一文件先后被报成 65 与 69，实为口径混淆 |
| 8 | **角色分离**：实现者不得审自己的实现；实现类工单若被派给复审方，须立刻 `reassign_task` 改派；**终态门不可改写，结论由新的门承接** | t57 自审落库不能作为 review 门；t75 判 needs_revision 后由 t83 承接 |
| 9 | **断言判别力自证**：任何新增/变更断言必须自证判别力（把被修对象改回缺陷态 ⇒ 断言必红），等价变异体必须如实标注为"不可观测"而非计入杀死 | F5 期中位数断言对 2 样本恒真（median≡mean）；F1 一条用例 monkeypatch 了被修对象本身 ⇒ 对"修/未修"不敏感；P4 M3 等价变异体被如实排除后未虚报 |
| 10 | **全量门禁离线口径（R19-1 裁定，研究连续体收官定案）**：本环境无 `TUSHARE_TOKEN` 且无外网 ⇒ 裸 `poetry run pytest` 数学上不可达（integration 用例 skip 门只读 `TUSHARE_TOKEN` 环境变量、不读 `~/tk.csv`；实验 A 设 token 后仍 `2 failed + 8 errors`，真跑必须联网）⇒ 正式口径 = `poetry run env HOME=/data/freedom/AlphaBee/tmp/pytest_home pytest tests -q -m "not integration"`（2057 passed / 31 deselected / EXIT=0）；**增量判定以基线奇偶校验替代**（实验 B：worktree `adec8b9` 与 HEAD 同两文件均 20 errors，逐字一致 ⇒ 非回归铁证）；文件级 5-file `--ignore` 清单（1974 passed EXIT=0）为 marker 纪律失效时的备选；收集范围必须固定 `tests/`（否则误收 `tmp/certified/*/tests` 与 `tmp/pre-commit-home/**`） | t19 验收按字面裸口径三次落入 failed，captain 裁定后一次收口；`ruff-format --all-files` 曾改写 2 个既存未格式化文件（已 `git restore` 复原），故 format 门禁用 `--files <认证路径>` 而非 `--all-files` |
| 11 | **派单提示 ≠ 授权；门归属以 claim/reassign 为准**：带 attempt_id 的派单提示不等于"已 claim"，动手前必须先 `claim_task` 验归属；成员在无 captain 明确授权时**不得执行任何写型 git 命令**（add/commit/reset/stash/checkout/push）；每期契约内的 chmod 444/644/复冻循环属**已授权动作**（定调 (a)），无需逐次申请 | t3 提交门被误派给 engineer-rc，其把派单提示误读为 claim、在"先报后做"未等回执时越权提交 `e42aab2`（未 push，内容 == 认证值，captain 复核后裁定接受并登记） |
| 12 | **交接/冻结前确认零写入**：reassign 或指令不能停止在飞写批次——交接、快照认证、复冻之前必须做同间隔零写入采样（`权限位\|mtime\|sha256` 三轮一致）并确认无 `index.lock`；跨期共享文件被后续期合法改写时，证书校验用**相位顺序覆盖**（取「最后认证它的那一期」）而非单期快照直比 | F4-B1 锚点失效 4 次；t22 复审窗口内文件被 F8 工作改写导致认证失效；收官校验单期快照直比大面积 MISMATCH（根因 = `deviation.py`/`config`/`scheduler.py`/`ROADMAP.md` 跨期共享），相位覆盖后 31/31 MISMATCH=0 |

---

## 当前关键问题

> 状态标记：✅ 已解决（见上方状态跟踪）　🟡 部分解决　⬜ 未解决
> 以下 9 项为历史梳理，已按当前 `alphabee/orchestrator/` 代码逐项核对（2026-08）。

### 1. ThesisEngine 只是聚合器，不是观点引擎 🟡

当前 `ThesisEngine` 的主体仍是：

```text
signal level × thesis_impact direction → 按维度平均 → judgment
```

这会得到 `financial_quality=negative`、`earnings_quality=neutral` 这类结构化判断，但不会自然形成：

```text
公司当前核心矛盾是：市场仍按高成长定价，但财务数据已经显示增长质量下降；
应收扩张快于收入、现金流没有同步兑现，当前估值需要更强利润兑现能力支撑。
```

已通过 `_apply_insight`（`agents/thesis/engine.py`）把 InsightAgent 的 `central_tension / counter_evidence / confidence` 作为定性语境注入维度并调节置信度；但 `core_view` 尚未显式参与维度判断，`materiality_rank` 也未驱动报告排序——观点主轴目前主要靠报告 prompt（`prompts.py`）承载，而不是 thesis 本身生成。

### 2. anomaly/conflict 没有充分进入 thesis 判断 ✅

已完成（0.2）：`nodes/thesis.py` 全量传入 `anomaly_report / conflict_analysis / verification_results / company_context / insight`；`engine.py` 的 `_apply_conflict_analysis` 会把 verified/partial 冲突计入维度扣分与 evidence、rejected 进入 counter_evidence、unknown 进入 missing_evidence，`_apply_anomaly_report` 把异常模式投影为维度 evidence。

### 3. Report Generator 被限制为格式化器 ✅

已完成：`REPORT_GENERATOR_PROMPT`（`prompts.py`）已改为“有观点、有论证、可证伪”的忠实裁决模式——允许压缩/排序/合并，只禁止引入 payload 之外的新事实；LLM 空输出有确定性降级报告（`reporter.py` `build_deterministic_report`）。

### 4. Reviewer 维度覆盖落后 ✅

已完成：`agents/thesis/reviewer.py` 的 `ThesisReviewer` 遍历全部 8 个维度（`dimensions/` 8 个 YAML）生成 `dimension_verdicts`，审查逻辑已随维度定义同步扩展。

### 5. 缺少重要性排序 🟡

`InsightOutput.materiality_rank` 已产出并随报告 payload 下发（`payload_builders.py`），报告 prompt 也要求 investment_viewpoint / scenario_analysis 引用其中关键变量；但尚无机制强制“按 materiality_rank 决定报告内部排序”，仍依赖 LLM 自觉执行。

### 6. InsightAgent 已接入，但稳定性不足 🟡

`synthesize_insights`（`nodes/insights.py`）已进入主流程，负责输出：

- `core_view`
- `central_tension`
- `main_driver`
- `supporting_evidence / counter_evidence`
- `base_case / bull_case / bear_case`
- `what_would_change_my_mind`

当前仍存在（经代码确认）：

- ✅ 枚举归一化 + 四级降级阶梯已落地（`agents/insights/rescue.py`）：严格解析 → 宽松救援 → 确定性兜底 → 最小骨架，任何失败模式下 insight artifact 必然存在（见 `tests/orchestrator/test_insight_degradation.py`）
- 🟡 `materiality_rank` 仍未显式驱动报告排序（依赖 LLM 自觉引用）

这意味着“观点层”已经稳定存在（不再整层丢失），但降级产出的观点质量仍低于 LLM 综合，且重要性排序尚未成为硬约束。

### 7. 冲突探索与验证的状态边界不够清晰 ✅

已完成（0.5）：`explore_conflicts` 只产出 provisional 冲突，不再直接升格 issue；`verify_hypotheses` 作为结算层（verified/partial 高严重度 → `verified_conflict` issue、rejected → decision、状态显式回写 `conflicts_result`）；`review_thesis` 只对“正向维度 vs 已验证冲突”制造 `thesis_conflict`；quality gate 只统计已结算类别。见 `tests/orchestrator/test_conflict_lifecycle.py`。

显式区分已经落地：

- provisional conflict（候选冲突 / 待验证）→ 不进入 issue
- verified conflict（已验证冲突 / 可进入最终判断）→ 进入 issue / thesis / gate

### 8. 证据链没有稳定闭环 🟡

quality gate 已检查 `evidence_coverage / grounding_score / disclosed_issue_ids`（`gates.py`），但上游 Decision 仍普遍未填 `based_on / evidence_refs`——目前沉淀的 decision 主要来自 thesis 维度 verdict（`thesis_reviewer`）与 conflict rejected（`conflict_verifier`），且大多没有证据引用。

后果是：

- 报告虽然写了很多结论，但“源可追”能力弱
- gate 会持续提示 evidence_coverage 低
- report rewrite 会越来越像“修措辞”，而不是“补证据”

### 9. 用户报告与系统调试信息混在一起 ⬜

`main.py` `_render_final_report()` 仍把全部 issues（含 parse_error / report_rewrite_needed / subagent_failure 等调试信息）打印到“🐞 系统问题”段（对应 0.6，未落地）。这虽然对开发排障有帮助，但会显著破坏用户看到的成品感，也会让报告从“研究结论”退化成“运行日志”。

---

## Roadmap

## Phase 0：修正当前链路的结构性问题

### 0.1 anomaly 进入 signal/thesis

状态：已完成第一版。

已实现：

```text
DerivedFacts
→ AnomalyEngine
→ 注入 anomaly fact_values
→ SignalEngine
→ ThesisEngine
```

已明确 anomaly signal dependencies：

- `anomaly_cluster_risk`
- `cross_validation_break`

后续可继续增强 anomaly signals，让二阶异常模式直接映射到 thesis 维度。

### 0.2 ThesisEngine 显式消费 anomaly/conflict

状态：✅ 已实现。

接口已落地（`agents/thesis/engine.py` 的 `run()` 实际接收，`nodes/thesis.py` 全量传入）：

```python
ThesisEngine.run(
    symbol,
    period,
    signal_results,
    anomaly_report=None,
    conflict_analysis=None,
    verification_results=None,
    company_context=None,
    insight=None,
)
```

落地效果：

- 已验证 high/critical conflict 下调相关维度（`_apply_conflict_analysis`）
- anomaly pattern 直接生成 thesis evidence（`_apply_anomaly_report`）
- rejected hypotheses 作为反向证据进入 thesis（counter_evidence）
- unknown hypotheses 进入 missing evidence
- insight 的 central_tension / counter_evidence / confidence 注入维度（`_apply_insight`）

### 0.3 修复 canonical field / signal rule 不一致

重点检查：

- signal rules 是否只依赖 canonical fields
- `operating_cash_flow` vs `operating_cashflow` 这类字段不一致
- anomaly facts 是否有统一 schema 记录
- blocked/missing_fact 是否被误判为 none

### 0.4 修复 Insight schema 脆弱性

状态：✅ 已实现（2026-08），落地方式见 `docs/design/INSIGHT_DEGRADATION_DESIGN.md`。

目标：不要让观点骨架因为轻微枚举值漂移而整层失效。

已实现：

- `_coerce_confidence / _coerce_importance`（`agents/insights/models.py`）：`moderate -> medium` 等常见枚举值归一化
- 对 `importance/confidence/weight` 等字段增加容错映射
- **四级降级阶梯**（`agents/insights/rescue.py`）：严格解析（Tier 0）→ 宽松救援 `lenient_parse`（Tier 1，只修结构不补内容）→ 确定性兜底 `build_fallback_insight`（Tier 2，从 `build_insight_context` 的 dict 转述合成，只转述不虚构）→ 最小骨架 `build_minimal_insight`（Tier 3）
- 任何失败模式下 `INSIGHT_ANALYSIS` artifact 必然存在；降级标记（`degraded / fallback_tier / degradation_reason`）随 artifact 落库，报告 prompt 有对应降级分支（`prompts.py`）
- 测试：`tests/orchestrator/test_insight_degradation.py`（14 用例，含诚实性硬规则 H1 断言）

### 0.5 分离“待验证冲突”与“已验证冲突”

**状态：✅ 已实现（2026-08）**，落地方式：

- `explore_conflicts` 只产出 provisional conflicts，不再把 high/critical 冲突直接升格为 issue（`nodes/conflicts.py`）
- `verify_hypotheses` 作为结算层：verified/partial 高严重度冲突升格为 `verified_conflict` issue；rejected 假设沉淀为 decision；unknown 保持 provisional（`nodes/verification.py`）
- 结算状态显式回写进 `conflicts_result` artifact（`verified / partial / rejected / unknown`），下游只读该 artifact 即可拿到真实状态
- `review_thesis` 只对“正向维度 vs 已验证冲突”制造 `thesis_conflict`，不再重复制造 verified_conflict / rejected decision（`orchestrator/agent.py`）
- 测试：`tests/orchestrator/test_conflict_lifecycle.py`

目标：探索可以更自由，但最终判断只消费已结算结果。

建议（历史记录）：

- `explore_conflicts` 只产出 provisional conflicts，不直接升格为 high issue
- `verify_hypotheses` 之后再决定哪些冲突进入 thesis/review/gate
- 对 `verified / partial / rejected / unknown` 做显式状态传播
- `rejected` 假设进入 counter evidence，避免所有疑点都悬而未决

### 0.6 修复用户输出与调试输出串层

目标：默认交付“分析结果”，而不是“系统运行诊断”。

建议：

- 默认报告中只保留用户有意义的不确定性披露
- parse_error / report_rewrite_needed / 内部调试信息转入 debug 视图或附录
- 区分“分析结论中的风险”和“系统实现层的问题”

---

## Phase 1：把 InsightAgent 从“已接入”升级为“稳定观点骨架”

这是从“数字堆砌”变成“观点驱动”的关键阶段。
当前不是“要不要有 InsightAgent”的问题，而是“如何让它稳定地主导下游表达”。

现状：

```text
alphabee/agents/insights/
  models.py
  prompts.py
  agent.py
```

在 orchestrator 中插入：

```text
verify_hypotheses
→ synthesize_insights
→ run_thesis / review_thesis
→ generate_report
```

当前进度（2026-08 与代码对齐）：

- ✅ 已接入主图（`nodes/insights.py`），报告 prompt 以 `core_view / central_tension` 为主线（`prompts.py`）
- ✅ `what_would_change_my_mind → falsification_conditions` 已贯通，insight 的 central_tension / counter_evidence / confidence 进入 thesis（`_apply_insight`）
- ✅ parse fail 已有四级降级（0.4，`agents/insights/rescue.py`），观点层不再整层丢失
- 🟡 `materiality_rank` 未显式驱动报告排序（已产出并下发，排序仍依赖 LLM 自觉）

### 目标输出结构

```json
{
  "core_view": "一句话核心观点",
  "central_tension": "最关键矛盾",
  "main_driver": "决定结论的核心变量",
  "supporting_evidence": [],
  "counter_evidence": [],
  "materiality_rank": [],
  "business_model_context": "",
  "base_case": "",
  "bull_case": "",
  "bear_case": "",
  "what_would_change_my_mind": []
}
```

### InsightAgent 核心职责

- 从 signals / anomaly / conflicts / verification 中提炼中心矛盾
- 识别最重要的 1-3 个判断变量
- 区分主证据、反证和缺失证据
- 输出可证伪的观点，而不是指标摘要

### 本阶段新增要求

- 下游 `run_thesis / generate_report` 必须优先消费 `core_view / central_tension`
- 如果 insight 缺失，报告应明确降级为“结构化摘要模式”
- `what_would_change_my_mind` 必须进入最终报告，作为观点可证伪条件
- `materiality_rank` 要真正影响报告排序，而不只是存档

---

## Phase 1.5：建立“探索自由，结论收敛”的中间层契约

状态：🟡 核心结算层已随 0.5 落地（provisional 不升格 issue、verified/partial 升格、rejected 沉淀 decision、状态回写 `conflicts_result`）；剩余增强项见下文（验证预算 / 最短排除路径 / 未探索区域记录 / evidence refs 硬约束）。

目标不是简单增加 agent 自由度，而是：

```text
探索可以发散，结论必须收敛
允许提出怀疑，不允许把怀疑伪装成事实
允许多轮验证，不允许无来源结论进入 final report
```

建议将中间层明确拆成三种职责：

### 1. Explore layer

- 允许提出多个候选冲突 / 假设
- 允许使用较开放的模式识别和跨维度联想
- 输出必须保持 provisional，不得直接改写最终判断

#### Explore layer 的具体增强方向

##### 1. 探索目标从“找风险”升级为“解释矛盾”

探索节点的核心任务不应只是继续罗列风险，而应围绕一个核心矛盾生成解释空间，例如：

- 真恶化：基本面正在变差
- 周期/季节性波动：短期数据偏离但不代表趋势反转
- 商业模式导致的正常错位：项目制、账期、扩产节奏带来的表观异常
- 会计口径或一次性因素：政策变更、并表、税务、补贴等扰动
- 市场预期先行：估值先反映未来，而财务兑现暂时滞后

目标是让 ExploreAgent 回答：

```text
为什么这些事实会互相打架？
```

而不只是：

```text
这里还有哪些风险？
```

##### 2. 强制“多假设并存”，避免单路径早收敛

每个高价值冲突至少保留三类解释：

- 主假设（当前最可能）
- 替代假设（第二解释）
- 反向假设（解释为什么它可能并不是问题）

这样可以避免系统看到一个 high signal 就一路向负面叙事滑坡。

##### 3. 引入“验证预算”机制，而不是无限自由

探索自由度应该通过预算控制，而不是完全放开 prompt。建议对每个 conflict 设置：

- 最多验证 2-3 个最高价值假设
- 每个假设最多调用 N 次工具
- 优先选择“最快能排除”的证据
- 严重度 × 可验证性 × 对最终判断影响度 共同决定预算分配

这样探索会更像 research triage，而不是无边界扩散。

##### 4. 引入“最短排除路径”策略

对每个候选假设，不只输出“还可以查什么”，还要输出：

```text
只要再确认哪 1-2 个事实，就能基本排除这个解释？
```

这会显著提升验证效率，也能减少 agent 为了显得勤奋而堆工具调用。

##### 5. 区分“异常”与“可解释异常”

探索层应显式回答：

- 这是经营异常？
- 这是会计口径变化？
- 这是扩产/项目制/行业周期下的正常偏离？

也就是说，不把 z-score 高自动等同于问题，而是把“发现偏离”推进到“解释偏离”。

##### 6. 行业/商业模式特化探索模板

探索不能只依赖通用 prompt。建议按 business model 切探索模板：

- 制造业：库存、产能、capex、毛利率传导
- To B / 项目制：应收、验收节奏、合同负债、回款滞后
- 周期行业：价格、库存、盈利弹性、资本开支周期
- 金融类：杠杆、资产质量、久期错配、流动性

这样 agent 才会像 analyst，而不是 generic summarizer。

##### 7. 记录“未探索区域”

探索质量不只取决于查了什么，也取决于是否知道自己没查什么。建议输出：

- 已验证方向
- 已排除方向
- 未验证但重要的方向
- 为什么没继续查（缺数据 / 工具不适合 / 性价比低）

这既有助于控制幻觉，也有助于后续人机协同接力。

### 2. Verification layer

- 可以自主决定用 Tushare / Eastmoney / web_search 查询什么
- 但每个裁决必须回填：
  - `supporting_evidence`
  - `refuting_evidence`
  - `gaps`
  - `confidence`
- unknown 不是失败，而是明确的“证据未闭环”

#### Verification layer 的执行原则

- 数值优先于叙述
- 优先查能最快区分多个竞争假设的证据
- 不追求“查得更多”，而追求“把解释空间缩小得更明确”
- 每次验证应服务于 hypothesis ranking，而不是重复采样已有结论

### 3. Settlement layer

- 只有经过验证结算的冲突和假设，才能进入 thesis / report
- 所有结论必须映射到 evidence refs
- report 不允许新增任何中间层没出现过的新判断

#### Settlement layer 的核心要求

- provisional hypothesis 不得直接进入 final judgment
- verified / partial / rejected / unknown 必须显式传播到 thesis 与 report
- report 只消费“已结算结果”，不直接消费探索阶段的自由文本
- 若仍存在多个未分胜负的解释，报告必须把它表述为“竞争性解释”，而不是伪装成单一确定结论

---

## Phase 2：建立 Business Model Context 层

当前 company context 只有行业、生命周期、市值分类，无法支撑高质量财务解释。

关于这一层如何进一步扩展为 **公司特定驱动画像 + ContextRouter + Domain Playbooks + EventOverlay**，已单独整理为：

```text
docs/roadmap/DOMAIN_CONTEXT_ROADMAP.md
```

该子 roadmap 的核心主张是：

- 不把 domain context 做成静态行业词典
- 用 `domain_primitives/ + domain_playbooks/ + runtime_context/` 三层架构
- 让上下文在运行时根据标的、问题、地域暴露和事件环境动态激活
- 让最终分析主线更像“牧原看猪周期、金诚信看矿业 CAPEX + 天气扰动”

建议新增：

```text
BusinessModelClassifier
```

输出：

```json
{
  "revenue_model": "to_b_credit_sales | to_c_cash_sales | project_based | subscription | commodity_cycle",
  "asset_intensity": "light | medium | heavy",
  "working_capital_pattern": "receivable_heavy | inventory_heavy | advance_payment | cash_conversion_fast",
  "cycle_sensitivity": "low | medium | high",
  "key_financial_pressure_points": [
    "accounts_receivable",
    "inventory",
    "capex",
    "gross_margin"
  ]
}
```

同样的财务信号在不同行业和商业模式下含义不同：

- 白酒的应收增长可能高度异常
- 军工的应收增长可能来自结算周期
- 软件公司的应收可能来自项目验收节奏
- 医药流通企业天然账期较重
- 光伏制造要结合库存、价格周期和资本开支

---

## Phase 3：从风险信号升级到论证图谱

建议引入 claim-evidence graph：

```json
{
  "claims": [
    {
      "claim": "公司增长质量下降",
      "stance": "bearish",
      "confidence": 0.72,
      "evidence_for": [],
      "evidence_against": [],
      "missing_evidence": [],
      "depends_on": []
    }
  ]
}
```

目标是让报告从：

```text
列指标、列风险、列维度
```

升级为：

```text
提出观点 → 给出证据 → 给出反证 → 指出还缺什么 → 说明什么会改变判断
```

本阶段的真正落点不是“多一个图结构”，而是让以下约束变成硬契约：

- 每个核心 claim 必须有 `evidence_for`
- 每个强判断必须允许 `evidence_against`
- 缺失证据必须显式挂在 `missing_evidence`
- 只有进入 claim graph 的结论，才允许进入最终报告

### Decision / EvidenceRef 改造目标

建议所有进入 review / gate / report 的关键 Decision 都补齐：

```text
based_on / evidence_refs
```

使 quality gate 不再只是检查“文案写得像不像”，而是检查“结论是否真的有来源、能回放、能审计”。

示例：

```text
Claim: 增长质量下降

Evidence for:
- 应收增速高于收入增速
- 经营现金流/净利润下降
- 利润增速未显著超过收入增速

Evidence against:
- 毛利率仍稳定
- 行业账期可能普遍拉长

Missing:
- 应收账龄
- 前五大客户变化
- 同行应收周转天数
```

---

## Phase 4：接入市场预期 / 估值隐含假设

当前估值更多是静态指标：PE、PB、PEG、历史估值位置。

真正有洞见的分析需要回答：

```text
市场价格隐含了什么预期？
财务质量能不能支撑这个预期？
如果不能，风险在哪里？
```

建议新增：

```text
ExpectationFitAgent
```

输入：

- `pe_ttm`
- `pb`
- `roe`
- `net_profit_yoy`
- `revenue_yoy`
- 行业估值
- 历史估值
- 可选分析师预期

输出：

```json
{
  "implied_expectation": "市场当前定价隐含未来仍需维持高利润增长",
  "fundamental_support": "weak | medium | strong",
  "expectation_gap": "估值要求的增长质量高于当前财务数据能证明的水平",
  "de_rating_risk": "high"
}
```

---

## Phase 5：报告升级为投资研究备忘录

当前报告偏“体检报告”。建议升级为“观点优先”的研究备忘录。

目标结构：

```text
1. 核心观点
2. 最关键矛盾
3. 支撑证据
4. 反向证据
5. 商业模式语境
6. 估值 / 预期匹配
7. 情景分析：Bull / Base / Bear
8. 需要继续验证的 3 个问题
9. 结论置信度
```

原则：

```text
数字服务观点，而不是观点附着在数字后面。
```

补充原则：

```text
报告负责裁决，不负责转储全部中间结果。
```

建议最终用户报告固定围绕 4 个问题组织：

1. 一句话观点：当前最值得相信/最该怀疑的是什么？
2. 核心矛盾：哪两个事实或预期在打架？
3. 裁决依据：支持观点的 2-3 条关键证据和最强反证是什么？
4. 证伪条件：未来看到什么数据，这个判断需要改变？

### Report Generator 升级方向

将当前“忠实转写”升级为“忠实裁决”：

- 允许压缩、排序、合并相近信息
- 允许统一语气和改善可读性
- 不允许引入 payload 中不存在的新事实或新数字
- 每个维度最多保留“2 条支持 + 1 条反证 + 1 个裁决”
- 高优先级 issue 以“分析不确定性披露”形式进入正文
- 内部调试问题默认不进入用户主报告

---

## 推荐优先级

| 优先级 | 事项 | 价值 | 当前状态 |
|---|---|---|---|
| P0 | Insight 降级：parse fail 保留观点骨架（0.4 收尾） | 保住观点主轴，不因 LLM JSON 漂移退回模板模式 | ✅ 已实现（四级降级，见 `agents/insights/rescue.py` + `test_insight_degradation.py`） |
| P0 | Decision 补齐 evidence refs | 解决 evidence_coverage 低和结论不可追溯 | 🟡 部分（gate 已检查，多数 Decision 仍未填） |
| P0 | anomaly 进入 signal/thesis | 修正当前链路断点 | ✅ 已实现 |
| P0 | ThesisEngine 显式消费 conflict/anomaly | 让高价值发现影响结论 | ✅ 已实现 |
| P0 | provisional / verified conflict 分层 | 提高探索自由度，同时避免怀疑冒充事实 | ✅ 已实现 |
| P1 | 用户输出与调试输出分层（0.6） | 提升成品感，减少“运行日志感” | ⬜ 未实现 |
| P1 | materiality_rank 驱动报告排序 | 让重要性真正影响表达顺序 | 🟡 部分（已产出并下发，未强制排序） |
| P1 | 稳定 InsightAgent 成为观点主轴 | 从数字堆砌变成观点生成 | 🟡 已接入主图，report 已以其为主线 |
| P1 | 报告结构改成核心观点优先 | 立刻改善用户感知 | ✅ 已实现（观点驱动报告重构） |
| P2 | BusinessModelContext | 提升行业/公司语境判断 | 🟡 基础字段已有，Classifier/Playbook 未做 |
| P2 | Claim-Evidence Graph | 让观点可追踪、可审查 | ⬜ 未实现 |
| P3 | ExpectationFitAgent | 打通财务质量与投资价值 | ⬜ 未实现 |
| P3 | 同行基准 / 行业分位 | 降低固定阈值误判 | 🟡 Phase 0 已落地（`resolve_industry_context` + 相对基准阈值 + `market_share_change` 复活，见 `docs/industry/industry-context-injection-plan.md`）；完整研究工作流/报告层未做 |

---

## 下一步建议

短期最值得做的 2 件事（更新于 2026-08 状态跟踪，按收益排序）：

1. 给关键 Decision 补齐 evidence refs（Phase 3 前置）：thesis 维度 verdict、conflict rejected、insight 判断都填上 `based_on / evidence_refs`，让 gate 的 evidence_coverage / grounding_score 从“文案检查”变成“来源审计”。
   **状态**：🟡 部分（gate 已检查，多数 Decision 未填）。
2. 落地用户输出与调试输出分层（0.6）：`main.py` `_render_final_report()` 只打印用户侧不确定性披露（verified_conflict / thesis_conflict / thesis_gap 等），parse_error / report_rewrite_needed / subagent_failure 转入 debug 视图或附录。
   **状态**：⬜ 未做（CLI 仍打印“🐞 系统问题”段）。

已完成（2026-08）：

- ✅ Insight 降级（0.4 收尾）：四级降级阶梯落地于 `agents/insights/rescue.py`，任何失败模式下 insight artifact 必然存在（见 `tests/orchestrator/test_insight_degradation.py`）。
