# DRIVER_PROFILE P1/P2 修复方案（框架提案与下游交付质量）

> **状态：📝 方案待评审（未动代码）**
> 背景：药明康德（603259.SH）Tier B 真实产物评审暴露的 P1/P2 级缺陷。
> 本方案与 `docs/design/DRIVER_PROFILE_P0_FIX_PLAN.md` 同源同风格：只改收敛层/契约增量/测试，
> 不动研究层 prompt 的主干逻辑（仅 P1-3/P2-6 两处**增量**prompt 条款）。
>
> 修复对象（对应评审结论）：
> - **P1-3**：`candidates` 引用了不存在的 playbook id（`capacity_ramp_cdmo` / `policy_regulatory_risk`
>   在 `domain_playbooks/` 中不存在），且把当前兜底 `generic_fundamental` 列为"备选"。
> - **P1-4**：`novel_drivers` 11 条中 7 条是 `driver_hypotheses[].variable` 的逐字副本——
>   收敛层把"C4 未对齐的变量"与"LLM 自述的新概念"两路语义混进了一个字段。
> - **P1-5**：C4 对齐器 7/7 未命中——`_match_primitive` 用**整串双向子串**匹配，且候选池
>   只有 `profile.activated_primitives`（fallback 时仅通用 3 原语），`project_delivery` 等
>   根本不在池内（"在手订单余额"与 `project_delivery.key_variables` 的"新签订单与在手订单"
>   共享 4 字却无法对齐）。
> - **P2-6**：company_form 期别混用（FY2025 的 51.89% 与 H1 同比 +7.48pp 无标注并列；
>   年化 ROE 27.69% 与半年 ROE 13.59% 相邻放置）。
> - **P2-7**：`unverified_drivers` 语义混装——实现把 `output.open_questions`（模型自述的
>   **认知缺口**）并进了 `unverified_drivers`（被 C2 剥夺证据资格的**驱动变量**），
>   两类语义被下游同一种方式对待。
> - **P2-8**：artifact 体量巨大（7 假设 × 长 company_form + 6 条长议程），D2 接线时
>   若整包注入 insight prompt 会撑爆上下文——需要确定性的有界摘要形状。

---

## 1. 修复总览

| # | 修复 | 关键动作 | 契约/分类变更 |
|---|---|---|---|
| P1-3 | candidates 闭包 + 提案拆分 | 输入包注入 `available_playbooks`；LLM 输出新增 `proposed_frameworks`；收敛层闭包校验，非法 id 移出 `candidates` 并留痕 | `DriverProfile.proposed_frameworks`（新增字段）；新 category `driver_profile_research_invalid_candidate`（LOW，D2_STRUCTURE） |
| P1-4 | novel_drivers 语义收敛 | LLM 输出字段更名 `novel_concepts` → 只进 `research_meta`；契约 `novel_drivers` = C4 推导的未对齐变量（唯一来源） | 无契约变更（meta 级） |
| P1-5 | C4 对齐器重写 | 最长公共子串（≥4 字符）匹配 + 候选池 = **全原语目录**（`load_primitives`）⊕ 激活原语同 id 覆盖 | `research_meta.match_details`（审计） |
| P2-6 | 期别标注 | prompt 增补硬条款（主防线）+ 同句多期别轻检测（辅助） | 新 category `driver_profile_research_period_mixing`（LOW，D3_ARGUMENT） |
| P2-7 | unverified / open_questions 拆分 | 契约新增 `open_questions`；`unverified_drivers` 只收 C2 剥离的变量 | `DriverProfile.open_questions`（新增字段） |
| P2-8 | 下游注入有界化 | `_build_driver_profile_summary` 增补**有界**的 hypotheses/agenda/open_questions 摘要（D2 接线时实施，本文档定死形状） | 无契约变更（payload 层） |

---

## 2. P1-3：candidates 闭包与 `proposed_frameworks` 拆分

### 2.1 LLM 输出契约（`alphabee/agents/driver_profile/output.py`）

```python
class DriverResearchOutput(BaseModel):
    """…（字段说明更新）"""

    profit_formula: str = ""
    driver_hypotheses: list[ResearchHypothesis] = Field(default_factory=list)
    research_agenda: list[ResearchAgendaItem] = Field(default_factory=list)
    # 备选 playbook：**只允许**输入包 available_playbooks 里给出的 id（收敛层闭包校验，
    # 非法 id 会被移出并留痕）
    candidates: list[str] = Field(default_factory=list)
    # 新框架提案：现有目录无法覆盖的分析框架短名（如「产能爬坡型CDMO」「定点监管风险」）
    # ——沉淀原料，不写库、不进 candidates
    proposed_frameworks: list[str] = Field(default_factory=list)
    # P1-4 更名（原 novel_drivers）：这家公司特有的、值得沉淀为分析原语的**概念**
    # ——不是 driver_hypotheses 变量的复述；收敛层只把它放 research_meta，契约的
    # novel_drivers 由 C4 对齐结果确定性推导
    novel_concepts: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "example": {
                # …前部不变…
                "candidates": ["generic_fundamental"],
                "proposed_frameworks": ["capacity_ramp_cdmo"],
                "novel_concepts": ["R阶段分子向D/M订单的漏斗转化率"],
                "open_questions": ["公司未披露的自研/代工拆分比例"],
            }
        },
    )
```

> ⚠️ 字段更名/新增后必须同步 `json_schema_extra.example`（`json_instruction` 的 few-shot
> 来源，模块 docstring 有硬性要求）。

### 2.2 输入包注入目录（`research.py::build_research_input`，return dict 增一键）

```python
from alphabee.domain_context.loader import load_playbooks  # 模块顶部 import（loader 无副作用）

    return {
        # …既有键不变…
        "available_playbooks": [
            {"id": playbook_id, "description": (playbook.description or "")[:160]}
            for playbook_id, playbook in sorted(load_playbooks().items())
        ],
        "notes": [
            # …既有两条不变，追加第三条…
            "candidates 只能引用 available_playbooks 中的 id；新框架提案走 proposed_frameworks。",
        ],
    }
```

### 2.3 收敛层闭包校验（`research.py::converge`，C4 之前插入；新常量/函数不赘述）

```python
    # ── C4.0（P1-3）：candidates 闭包校验（非法 id 移出，提案不污染候选）──
    catalog_ids = {playbook_id for playbook_id in load_playbooks()}
    valid_candidates: list[str] = []
    dropped_candidates: list[str] = []
    for candidate in _dedup(output.candidates):
        if candidate in catalog_ids:
            valid_candidates.append(candidate)
        elif candidate.strip():
            dropped_candidates.append(candidate)
    if dropped_candidates:
        meta["candidate_drops"] = dropped_candidates
        issues.append(
            {
                "severity": "low",
                "category": "driver_profile_research_invalid_candidate",
                "message": (
                    f"candidates 引用了目录中不存在的 playbook id：{dropped_candidates}，已移除。"
                    "（新框架提案请走 proposed_frameworks 字段，本轮提案已保留："
                    f"{_dedup(output.proposed_frameworks)}）"
                ),
            }
        )
```

> **实现口径（P1-3）**：闭包源取 `load_playbooks() ∪ load_primitives()` **并集**，而非上面代码块字面的 playbooks-only——提示面窄（输入包 `available_playbooks` 仍只给 playbook id、§2.2 不提供原语 id）、校验面宽（只拦"两个目录都不存在"的 id），属有意设计；§10.2 的 `candidates=["capacity_cycle"]` 用例与真实缓存 603259 的 3 个 primitive 级候选均依赖此口径，而文档举例的 `capacity_ramp_cdmo`/`policy_regulatory_risk` 两目录皆无、仍被移出并留痕。

最终落契约（C4/C6 的 update 块）：

```python
        update={
            # …
            "candidates": valid_candidates,                      # 原 _dedup(output.candidates)
            "proposed_frameworks": _dedup(output.proposed_frameworks),   # 新增
            # …
        }
```

### 2.4 prompt 增补（`prompts.py` §三，替换原 candidates/novel_drivers 两条）

```text
同时给出：
- `candidates`：若这家公司可能同时适用多套分析框架，**只能**列出输入包 available_playbooks
  里给出的 playbook id（目录外的框架不要写进 candidates）；
- `proposed_frameworks`：若你认为存在现有目录无法覆盖的分析框架，用短名提案
  （如「产能爬坡型CDMO」），每条 ≤ 12 字——这是知识库沉淀的原料，不是备选框架；
- `novel_concepts`：这家公司特有的、值得沉淀为分析原语的概念或观测口径（如
  「R阶段分子向D/M订单的漏斗转化率」），**不要复述 driver_hypotheses 的变量名**；
- `open_questions`：你无法验证、但会影响判断的猜测或数据缺口。
```

### 2.5 契约（`domain_context/contracts.py::DriverProfile`，v2 新增字段）

```python
    # 新框架提案（P1-3）：LLM 提出的、现有 playbook 目录无法覆盖的分析框架短名。
    # 与 candidates 语义分离：candidates 只含目录内 id（闭包校验保证）；
    # 本字段是 D3「novel 沉淀闭环」的正式输入，不写库、不进主线。
    proposed_frameworks: list[str] = Field(default_factory=list)
```

---

## 3. P1-4：`novel_drivers` 语义收敛（唯一来源 = C4 推导）

### 3.1 收敛层（`converge` 的 update 块）

```python
        update={
            # 契约 novel_drivers：只保留 C4 推导的「未对齐原语的驱动变量」——
            # LLM 自述的 novel_concepts 不混入（药明康德 run 里 11 条中 7 条
            # 是假设变量副本的根因），其内容进 research_meta 供沉淀工具使用。
            "novel_drivers": _dedup(novel),
            # …
        }
```

`meta` 增补（与 `matched_primitives` 同批）：

```python
        meta["novel_concepts"] = _dedup(output.novel_concepts)
```

### 3.2 解析层的同步（`research.py::salvage_research_output`）

```python
    output = DriverResearchOutput(
        profit_formula=str(parsed.get("profit_formula") or ""),
        driver_hypotheses=hypotheses,
        research_agenda=agenda,
        candidates=_coerce_str_list(parsed.get("candidates")),
        proposed_frameworks=_coerce_str_list(parsed.get("proposed_frameworks")),
        novel_concepts=_coerce_str_list(parsed.get("novel_concepts")),
        open_questions=_coerce_str_list(parsed.get("open_questions")),
    )
```

---

## 4. P1-5：C4 对齐器重写（最长公共子串 + 全目录候选池）

### 4.1 匹配函数（替换 `_match_primitive`，`research.py` 现 871–876 行）

```python
#: P1-5：对齐的最短共享连续子串（字符数；< 4 视为偶合不匹配，如"产能"/"成本"这类双字词）。
_MATCH_MIN_CHARS = 4


def _longest_shared_substring(a: str, b: str) -> str:
    """两串的最长公共连续子串（滑动窗口，输入均为短串，无需 DP 优化）。

    语义对齐的判据是「连续子串」：变量「持续经营业务在手订单余额及其同比」与
    key_variable「新签订单与在手订单」共享「在手订单」（4 字）⇒ 对齐；
    旧实现的整串双向子串（`_contains`）要求整个 key_variable 成为子串，导致 7/7 未命中。
    """
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return ""
    if len(a) > len(b):
        a, b = b, a
    for length in range(len(a), _MATCH_MIN_CHARS - 1, -1):
        for start in range(len(a) - length + 1):
            chunk = a[start:start + length]
            if chunk in b:
                return chunk  # 从最长到最短，首个命中即最长
    return ""


def _match_primitive(
    variable: str,
    primitives: Mapping[str, Any],
) -> tuple[Any | None, str, str]:
    """C4（P1-5 重写）：驱动变量 → primitive 语义对齐。

    返回 ``(primitive|None, 命中 key_variable, 共享子串)``：取**全候选池**内共享子串
    最长的命中；< ``_MATCH_MIN_CHARS`` 视为未对齐；同长按 primitive id 升序决平
    （迭代 ``sorted(primitives)``，仅严格更长才替换 ⇒ 天然确定性，不依赖 loader 目录序）。
    """
    best: tuple[int, Any | None, str, str] = (-1, None, "", "")
    for primitive_id in sorted(primitives):
        primitive = primitives[primitive_id]
        for key in primitive.key_variables:
            shared = _longest_shared_substring(variable, key)
            if len(shared) >= _MATCH_MIN_CHARS and len(shared) > best[0]:
                best = (len(shared), primitive, key, shared)
    return best[1], best[2], best[3]
```

### 4.2 候选池切换（`converge` 的 C4 块，替换现 1016–1024 行）

```python
    # ── C4：原语对齐（P1-5：全目录池 ⊕ 激活原语同 id 覆盖）────────────
    # 候选池从"仅 activated_primitives"改为**全原语目录**：
    # fallback 时激活的只有通用 3 原语，project_delivery 等永远不可能被对齐
    # （药明康德 run 的 7/7 未命中原因之二）。激活原语按 id 覆盖目录条目——
    # 这既是"弱锚时锚定知识优先"的业务语义，也保留测试的注入缝
    # （test_research.py 的 _capacity_primitive() 自定义 fixture 仍生效）。
    primitives_pool: dict[str, Any] = {
        primitive.id: primitive for primitive in load_primitives().values()
    }
    for primitive in profile.activated_primitives:
        primitives_pool[primitive.id] = primitive
    candidates: list[_Candidate] = []
    novel: list[str] = []
    matched_ids: list[str] = []
    match_details: list[dict[str, str]] = []          # P1-5 审计：为什么对齐/对齐到哪
    for hypothesis in checked:
        primitive, matched_key, shared = _match_primitive(hypothesis.variable, primitives_pool)
        if primitive is None:
            novel.append(hypothesis.variable)
            candidates.append(_Candidate(hypothesis=hypothesis, novel=True))
            continue
        matched_ids.append(primitive.id)
        match_details.append(
            {
                "variable": hypothesis.variable,
                "primitive": primitive.id,
                "key_variable": matched_key,
                "shared": shared,
            }
        )
        # …继承逻辑（falsifiers / causal_paths 去重追加）原样不变…
```

`meta` 增补：`meta["match_details"] = match_details`（`matched_primitives` 保持原键不变）。

> 对齐收益预期（药明康德重放）：H1→`capacity_cycle`（共享"产能利用率"）、
> H2→`project_delivery`（"在手订单"）、H4→`project_delivery`（"在手订单"）、
> H6→`capacity_cycle`（"资本开支"）；H3/H5/H7 仍为 novel。4/7 对齐（现 0/7），
> 各自继承对应原语的 `causal_paths` / `disconfirming_signals`。

---

## 5. P2-6：期别标注（prompt 主防线 + 同句轻检测）

### 5.1 prompt 增补（`prompts.py` §二"六件套"的 `company_form` 条目之后）

```text
- `company_form` 里每个数字必须标注报告期与口径：同比必须写清基期
  （如"56.55%（较 2025H1 的 49.07% +7.48pp）"）；年度与半年度数字不得无标注并列；
  半年度 ROE 不得与年度 ROE 直接相邻比较。
```

### 5.2 同句期别混用轻检测（`research.py` 新函数 + `converge` 接线）

```python
def _period_mix_sentences(hypotheses: Sequence[ResearchHypothesis]) -> list[str]:
    """P2-6 辅助检测：同一句内出现 ≥2 个期别标签且 ≥2 处数字声明 ⇒ 疑似期别混用。

    只查同句（确定性可判）；跨句的期别对照是正常的趋势讨论，不做机器裁决——
    主要防线在 prompt 的标注条款（药明康德 case 里模型在 agenda 中已展示它知道期别，
    只是 company_form 没写清）。
    """
    hits: list[str] = []
    for hypothesis in hypotheses:
        for sentence in _SENTENCE_SPLIT_RE.split(hypothesis.company_form or ""):
            years = sorted(_period_tags(sentence))       # 复用 P0 方案的 _period_tags
            numbers = list(_NUMBER_RE.finditer(sentence))  # 复用 P0 方案的 _NUMBER_RE
            if len(years) >= 2 and len(numbers) >= 2:
                hits.append(f"「{hypothesis.variable}」：{sentence.strip()[:120]}")
    return hits
```

`converge` 接线（C3 之后、C4 之前）：

```python
    mix_hits = _period_mix_sentences(verified)
    if mix_hits:
        issues.append(
            {
                "severity": "low",
                "category": "driver_profile_research_period_mixing",
                "message": "同一句内混用多个报告期且含多处数字，期别口径疑似未标注："
                + "；".join(mix_hits),
            }
        )
```

> 依赖说明：`_period_tags` / `_NUMBER_RE` 来自 P0 修复方案（同文件新增）；若 P0 先落地，
> 直接复用；若 P1/P2 先落地，须先并入这两个函数（P0 方案 §2）。

---

## 6. P2-7：`unverified_drivers` 与 `open_questions` 拆分

### 6.1 契约（`contracts.py::DriverProfile`，v2 新增字段）

```python
    # 研究层自述的"无法验证但影响判断"的开放问题（P2-7 与 unverified_drivers 语义分离）：
    # unverified_drivers = 被 C2 剥夺证据资格的**驱动变量**（隔离区，禁止进主线）；
    # open_questions    = 数据缺口/口径悬疑（如"公司未披露剥离处置收益的一次性占比"），
    #                     供研究议程补充与 D3 沉淀，不作为"被踢出的驱动"处理。
    open_questions: list[str] = Field(default_factory=list)
```

### 6.2 收敛层（`converge` 的 update 块）

```python
        update={
            # …
            "unverified_drivers": _dedup(unverified),            # 原 [_dedup(unverified), *output.open_questions]
            "open_questions": _dedup(output.open_questions),     # 新增：语义独立落位
            # …
        }
```

### 6.3 降级塑形同步（`_shape_for_tier`，Tier 2/3 的 update 块各补两键）

```python
                "unverified_drivers": [],
                "open_questions": [],          # P2-7 新增
                "proposed_frameworks": [],     # P1-3 新增
```

---

## 7. P2-8：下游注入有界化（`payload_builders.py`，D2 接线时实施）

> 本文档现在**只定死形状与测试**，代码随 D2 下游接线（`docs/design/DRIVER_PROFILE_RESEARCH_DESIGN.md` §9）
> 一并落地——当前 `_build_driver_profile_summary` 尚未携带 hypotheses/agenda，
> 提前实现会再次制造"没人读的字段"。

```python
def _bounded_hypotheses(profile: DriverProfile, *, top: int = 5) -> list[dict[str, Any]]:
    """驱动假设的有界摘要：按 confidence 取 top-N，长文本走既有 _bounded_text 裁剪。"""
    items = sorted(profile.driver_hypotheses, key=lambda item: item.confidence, reverse=True)[:top]
    return [
        {
            "variable": _bounded_text(item.variable, 120),
            "role": item.role,
            "mechanism": _bounded_text(item.mechanism, 200),
            "company_form": _bounded_text(item.company_form, 200),
            "falsifiers": _bounded_texts(item.falsifiers, limit=100, max_items=2),
            "evidence_count": len(item.evidence),
            "confidence": item.confidence,
            "matched_primitive": item.matched_primitive,
        }
        for item in items
    ]


def _bounded_agenda(profile: DriverProfile) -> list[dict[str, Any]]:
    """研究议程的有界摘要：保留可执行性字段，why_matters 裁剪。"""
    return [
        {
            "question": _bounded_text(item.question, 200),
            "priority": item.priority,
            "why_matters": _bounded_text(item.why_matters, 200),
            "decisive_evidence": _bounded_texts(item.decisive_evidence, limit=150, max_items=2),
            "preferred_sources": _bounded_texts(item.preferred_sources, limit=150, max_items=2),
        }
        for item in profile.research_agenda
    ]
```

`_build_driver_profile_summary` 尾部增补（rule 路径零膨胀）：

```python
    if profile.provenance in ("llm", "hybrid"):
        result.update(
            {
                "provenance": profile.provenance,
                "anchor_strength": profile.anchor_strength,
                "driver_hypotheses": _bounded_hypotheses(profile),
                "research_agenda": _bounded_agenda(profile),
                "unverified_drivers": _bounded_texts(profile.unverified_drivers, limit=150, max_items=4),
                "open_questions": _bounded_texts(profile.open_questions, limit=150, max_items=4),
                "novel_drivers": _bounded_texts(profile.novel_drivers, limit=100, max_items=6),
            }
        )
    else:
        result.update({"provenance": profile.provenance, "anchor_strength": profile.anchor_strength})
    return result
```

**有界性验收**（写入测试）：对药明康德体量级 fixture（7 假设 × 全字段 + 6 议程），
断言 `json.dumps(summary, ensure_ascii=False)` 长度 ≤ 8KB（人工拍板的上界，可调），
且完整内容仍可通过 `find_artifact_model(..., DriverProfile)` 从 artifact 取回（审计不丢）。

---

## 8. 契约变更清单（走 `alphabee-pipeline-contract-steward`）

`DriverProfile` 新增两个**带默认值**字段（纯增量，旧 artifact 反序列化不受影响）：

| 字段 | 类型 | 语义 | 落位 |
|---|---|---|---|
| `proposed_frameworks` | `list[str] = []` | LLM 提出的新框架短名（沉淀原料） | P1-3 |
| `open_questions` | `list[str] = []` | 研究层自述的开放问题（认知缺口） | P2-7 |

LLM 输出契约（`DriverResearchOutput`，非 artifact 契约，改字段名不涉及下游）：
`novel_drivers` → `novel_concepts`；新增 `proposed_frameworks`。

---

## 9. 偏离账本与测试锚同步（必须同期）

| 文件 | 改动 |
|---|---|
| `alphabee/orchestrator/services/deviation.py` | 追加：`"driver_profile_research_invalid_candidate": DeviationClass.D2_STRUCTURE`、`"driver_profile_research_period_mixing": DeviationClass.D3_ARGUMENT` |
| `tests/orchestrator/test_deviation_service.py` | `_EXPECTED_CATEGORY_CLASSES` 与 `test_real_world_category_extension` 参数化同步补两条 |
| `docs/design/DEVIATION_CONTROL_FRAMEWORK.md` | 分类表补两行 |

---

## 10. 测试计划

### 10.1 新增用例（`tests/domain_context/test_research_p1p2.py`）

**P1-3**
| 用例 | 断言 |
|---|---|
| `test_candidates_invalid_id_dropped_and_issue` | `candidates=["capacity_ramp_cdmo", "generic_fundamental"]` ⇒ 收敛后 `candidates==["generic_fundamental"]`；`meta["candidate_drops"]==["capacity_ramp_cdmo"]`；issue category `driver_profile_research_invalid_candidate` |
| `test_proposed_frameworks_passthrough` | `proposed_frameworks=["产能爬坡型CDMO"]` ⇒ 契约字段原样落地，且**不进** candidates |
| `test_build_research_input_carries_available_playbooks` | 输入包含 `available_playbooks` 且含 `hog_cycle`/`generic_fundamental` 的 id+description |

**P1-4**
| 用例 | 断言 |
|---|---|
| `test_novel_drivers_only_c4_derived` | LLM 传 `novel_concepts=["概念X"]` + 假设「铜箔-树脂价差」（未对齐）⇒ 契约 `novel_drivers==["铜箔-树脂价差"]`；`meta["novel_concepts"]==["概念X"]` |
| `test_novel_concepts_not_duplicated_into_drivers` | LLM 把假设变量逐字复述进 `novel_concepts` ⇒ 契约 novel_drivers 不出现重复（C4 推导值与 concept 分道扬镳） |

**P1-5**
| 用例 | 断言 |
|---|---|
| `test_shared_substring_aligns_order_book_to_project_delivery`（**药明康德回归**） | 变量「持续经营业务在手订单余额及其同比」+ 全目录池 ⇒ `matched_primitive=="project_delivery"`，mechanism 尾部追加「订单增长 -> 收入确认滞后 -> 利润逐步兑现」，`meta["match_details"][0]["shared"]=="在手订单"` |
| `test_two_char_shared_substring_does_not_match` | 变量「成本压力」与 cost_curve 只共享「成本」（2 字）⇒ 未对齐 |
| `test_tie_break_by_primitive_id_ascending` | 构造两个原语等长命中 ⇒ 取 id 较小者（确定性） |
| `test_activated_primitive_overrides_catalog_same_id` | 激活版 capacity_cycle（自定义 fixture）覆盖目录版 ⇒ 继承激活版的 causal_paths（保住既有注入缝） |

**P2-6**
| 用例 | 断言 |
|---|---|
| `test_period_mixing_same_sentence_flags` | company_form 一句含「2026H1…2025年…」+ 两处数字 ⇒ LOW issue `driver_profile_research_period_mixing` |
| `test_period_mixing_cross_sentence_not_flagged` | 两句各含单一期别 ⇒ 无 issue（跨句对照是正常趋势讨论） |

**P2-7**
| 用例 | 断言 |
|---|---|
| `test_open_questions_split_from_unverified` | `open_questions=["缺口X"]` + 无证据假设「变量Y」⇒ `unverified_drivers==["变量Y"]`、`open_questions==["缺口X"]` |

**P2-8**（`tests/orchestrator/`，D2 接线时同批）
| 用例 | 断言 |
|---|---|
| `test_driver_profile_summary_bounded` | 药明康德体量 fixture ⇒ summary JSON ≤ 8KB；`driver_hypotheses` 条数 ≤5 且按 confidence 降序；rule 路径 summary 不含 hypotheses/agenda 键（零膨胀） |

### 10.2 既有测试的**预期改动**（必须同期，逐条登记）

| 位置 | 改动 | 原因 |
|---|---|---|
| `tests/domain_context/test_research.py:483` | `novel_drivers=["铜箔-树脂价差"]` → `novel_concepts=["铜箔-树脂价差"]` | LLM 输出字段更名（P1-4） |
| 同文件 `:492` | `assert converged.novel_drivers == ["铜箔-树脂价差"]` → `assert converged.novel_drivers == []` + `assert converged.research_meta["novel_concepts"] == ["铜箔-树脂价差"]` | 契约 novel_drivers 唯一来源改为 C4 推导（该用例两条假设都对齐了 capacity_cycle，C4 推导为空） |
| 同文件 `:494` | `assert converged.unverified_drivers == ["公司未披露的自研/代工拆分"]` → `assert converged.unverified_drivers == []` + `assert converged.open_questions == ["公司未披露的自研/代工拆分"]` | P2-7 语义拆分 |
| 同文件 `test_finalize_tier2/tier3` | 增补断言 `payload["open_questions"]==[]`、`payload["proposed_frameworks"]==[]` | `_shape_for_tier` 清空清单补两键（§6.3） |
| `tests/orchestrator/test_deviation_service.py` | §9 两行期望 | 新 category 登记 |

其余既有用例（C4 的三个、C6 其余断言、finalize 系列）**预期零改动**——已在方案中
逐条核对：LCS 匹配器对「产能利用率」（5 字）仍命中 capacity_cycle；激活原语覆盖
机制保住 `_capacity_primitive()` 注入缝；`candidates=["capacity_cycle"]` 通过闭包。

### 10.3 与 P0 方案的顺序依赖

`_period_tags` / `_NUMBER_RE` / `_SENTENCE_SPLIT_RE` 由 P0 方案定义（§2），P2-6 直接复用。
建议落地顺序：**P0 先行**（正确性优先），P1/P2 随后；若顺序倒置，须先把 P0 方案 §2
的三个正则/函数并入。

---

## 11. 落地顺序与验收

1. **契约先行**：`output.py`（字段更名/新增 + example 同步）→ `contracts.py`（两个新字段）
   → `prompts.py`（P1-3/P2-6 增补条款）→ `research.py`（salvage 同步）。
2. **收敛层**：P1-3 闭包 → P1-4 novel 拆分 → P1-5 匹配器 + 池切换 → P2-6 轻检测 → P2-7 拆分
   → `_shape_for_tier` 补键（一个提交内聚）。
3. **同步登记**：§9 三处。
4. **测试**：§10.2 的既有用例改动 + §10.1 新用例；全量 `tests/domain_context` +
   `tests/orchestrator/test_deviation_service.py` + `tests/orchestrator/test_resolve_driver_profile.py`。
5. **真机回归（可选）**：对药明康德缓存重放 `converge()`，断言——
   ① `matched_primitives` 含 `capacity_cycle`/`project_delivery`（P1-5 生效，0/7 → 4/7）；
   ② `candidates` 不再含不存在的 id，`proposed_frameworks` 承接两个提案（P1-3）；
   ③ `novel_drivers` 与假设变量无逐字重复（P1-4）；
   ④ `open_questions` 与 `unverified_drivers` 分列（P2-7）。

**验收判据**：新用例全绿 + §10.2 登记的既有用例改动全绿（其余零改动）+
偏离分类登记同步 + 重放药明康德 case 的四条预期成立。
