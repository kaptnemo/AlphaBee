# DRIVER_PROFILE P0-1/P0-2 修复方案（收敛层数值可靠性）

> **状态：📝 方案待评审（未动代码）**
> 背景：药明康德（603259.SH）Tier B 真实产物评审（见会话记录与 `data/driver_profiles/603259.SH.json`），
> 暴露 Tier C 收敛层两个正确性缺陷。本方案只改**收敛层**（`alphabee/domain_context/research.py`）、
> **偏离分类登记**（`orchestrator/services/deviation.py`）与**测试**；
> 不动 prompt / agent / `DriverProfile` 契约字段 / 下游消费方。
>
> 修复对象：
> - **P0-1A**：跨假设隐含分母一致性——「美国客户收入 140.3 亿、占集团收入 67.45%」与
>   「化学业务收入 249.9 亿（占集团 86.46%）」分别隐含集团收入 208 亿 / 289 亿，互相矛盾却通过了收敛层。
> - **P0-1B**：数字出处强度——H3 的 2026H1 数字（140.3 亿 / 67.45%）没有任何同值同期的证据 quote
>   或输入包数值支撑（evidence 只引了 2025 年报 quote），C2 只查"ref 非空"查不出来。
> - **P0-2**：毛利率校验的口径误配——「生物学业务毛利率 34.92%」被拿去与集团毛利率 53.9% 比较，
>   产生假冲突（`research_meta.issues` 里的 `driver_profile_research_fact_conflict` 即此误报）。

---

## 1. 修复总览

| 修复 | 新增校验 | 触发后的行为 | 新增 issue category |
|---|---|---|---|
| P0-1A | 同一 (基数, 期别) 下「X 亿 + 占…Y%」配对的隐含总量自洽 | 相关假设**同时降权**（conf ≤ 0.3，无法判定孰对孰错） | `driver_profile_research_internal_inconsistency` |
| P0-1B | company_form 的财务类数字声明必须被**同值同期**的证据或输入包覆盖 | 占比句中的无出处数字 ⇒ 降权；其余无出处数字 ⇒ 只留痕不降权 | `driver_profile_research_claim_uncovered` |
| P0-2 | 毛利率校验按**口径**分流：具名分部值只与同名分部基准比；无名/集团口径才与集团值比 | 同名分部偏差超容差 ⇒ 降权；无分部基准 ⇒ **跳过**（不再误报） | 复用 `driver_profile_research_fact_conflict` |

设计原则（三处通用）：

1. **只做"发现与降权"，不替下游裁决**——P0-1A 无法判定两个矛盾声明哪个错，所以相关假设同时降权，
   issue 里把双方声明与隐含总量都列出，交人工/下游定夺。
2. **宁可漏检，不可误报**——期别归属歧义（一句含多个报告期）的声明一律跳过；
   无分部基准的分部毛利率一律跳过；只会产生确定性的真冲突。
3. **纯函数、可单测**——所有新校验无 I/O、无随机源，直接单测穷举；符合仓库"确定性内核"约束。

> **实现偏离登记（t2/t10 审查 R1/R2 授权，代码为准）**：P0-2 的口径分流在实现与回归中暴露了
> 「期别/语境前缀被 `_MARGIN_NAMED_RE` 当成业务名（如 `2025年毛利率30%` ⇒ name=`2025年`）」
> 的**静默漏检**，实现按审查要求做了三处增强（§5 代码块因此与实现不同，差异以本条为准）：
> 1. **期别前缀词表剥离**：name 先剥离 `_PERIOD_CONTEXT_TOKENS` 与 `_PERIOD_TOKEN_RE`
>    （年份/H1·H2/Q1–Q4/季度/半年报/年报/季报/一季报/三季报/中报/月报/本月/本季/上季/本报告期…）；
> 2. **非业务名兜底归零**：剥离后为空、长度 < 2、或仅由期别量词字符（年|月|季|报|度|期）构成
>    （`2025年报`⇒`报`）⇒ 视为"非业务名"，不吞 named span，回到残余文本按无名/集团口径与集团值比；
> 3. **冲突文案带后缀**：`_MARGIN_NAMED_RE` 捕获 `业务|分部` 后缀，冲突文案为
>    `{name}{suffix}毛利率: …`（不再输出被截断的「生物学毛利率:」）。
>
> 上述增强不改变 P0-1A/P0-1B 的语义与本表行为列，也不改变"无同名分部基准 ⇒ 跳过"的 P0-2 防误报性质；
> 边界见 §9 第 6/7 条。

---

## 2. 新增常量与正则（`research.py` 常量区，现 105–130 行附近追加）

```python
# ══════════════════════════════════════════════════════════════════════════
# P0-1/P0-2 修复：数值声明抽取与口径感知校验（药明康德 run 暴露的收敛层缺口）
# ══════════════════════════════════════════════════════════════════════════

#: P0-1A：同一 (基数, 期别) 的隐含总量相对差超过此值 ⇒ 判定互相矛盾。
_IMPLIED_TOTAL_TOLERANCE = 0.05
#: P0-1B：输入包权威值兜底的容差（占比/毛利率用绝对百分点，金额用相对差）。
_INPUT_PCT_TOLERANCE_PP = 2.0
_INPUT_AMOUNT_TOLERANCE = 0.03
#: P0-1B：不检查的单位（运营性口径，非财务声明）。
_IGNORED_UNITS = {"倍", "kL", "L", "个"}

# 期别归一：年份 + 报告期形态
_YEAR_RE = re.compile(r"(20\d{2})")
_END_DATE_RE = re.compile(r"(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])")

# 「X 亿(元/人民币)」——隐含总量的分子
_AMOUNT_YI_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*亿(?:元|人民币)?")

# 「占<base><收入|营收>?<P>%」——隐含总量的分母声明（base 归一后作为分组键）
# 注：只覆盖"占+基数+数字%相邻"的形态；「占持续经营收入 434.2 亿的约 72%」这类
# 分母金额在前、占比在后的形态**故意不覆盖**（保守漏检，见 §9）。
_ZHAN_RE = re.compile(r"占(?P<base>[\u4e00-\u9fa5]{1,12}?)(?:收入|营收)?\s*(?P<pct>\d+(?:\.\d+)?)\s*%")
#: 「占比」形态中 base 会误抓到的废词（"占比86.46%" → base="比"）。
_ZHAN_BASE_STOP = {"比", "比重", "例", "比例", "率"}

# 财务类数字声明（P0-1B 的声明单元；company_form 侧的声明必须带单位）
_NUMBER_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>亿(?:元|人民币)?|万(?:元)?|%|％|倍|kL|L|个)"
)
#: 证据侧的裸数值抽取（tushare ref 的值是无单位形态："netprofit_margin=42.53"，
#: 不能用 _NUMBER_RE；裸抽取会顺带收进 603259/end_date 等无害串，不影响同值匹配）
_RAW_NUMBER_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)")

# 具名毛利率（P0-2）：「<名>业务/分部?毛利率 P%」；名称为集团口径词时走集团校验
_MARGIN_NAMED_RE = re.compile(
    r"(?P<name>[\u4e00-\u9fa5A-Za-z0-9/·\-]{2,20}?)\s*(?:业务|分部)?毛利率\s*(?P<value>-?\d+(?:\.\d+)?)\s*%"
)
_GROUP_SCOPE_NAMES = ("集团", "公司", "整体", "综合", "当年", "上年")

_SENTENCE_SPLIT_RE = re.compile(r"[。；;，,\n]")
```

> 注：`re` 已导入；`_MARGIN_RE`（无名毛利率）保留原样不变，供 P0-2 的残余文本回退检查使用。

---

## 3. P0-1A：跨假设隐含分母一致性

### 3.1 新增函数（放在 Tier C 区，`_dedup`/`_contains` 之后、`_segment_shares` 之前）

```python
@dataclass
class _ShareClaim:
    """一条「X 亿 + 占<base>Y%」配对声明：隐含总量 = X / Y × 100。"""

    amount_yi: float | None   # 分子（亿元）；同句找不到分子 ⇒ None（不参与校验）
    pct: float                # 占比（%）
    base: str                 # 归一化基数名（"集团"/"化学业务"…）
    period: str | None        # 单一期别标签；None = 一句含多个期别（歧义，跳过）
    hypothesis_index: int     # 归属假设序号（降权定位）
    snippet: str              # 所在句（issue 展示）


def _period_tags(text: str) -> set[str]:
    """提取文本中的期别标签，归一为 ``{year}{H1|Q1|Q3|FY}`` 集合。

    顺序敏感：先判半年报（"2026年半年度报告" 含 "年度" 子串）、再判季度、其余归全年；
    ``end_date`` 形态（``20260630``）按月份映射，且其区间**遮蔽**年份扫描，避免
    "2026" 被同时归成 FY 与 H1 两个标签。
    """
    text = text or ""
    masked: list[tuple[int, int]] = []
    tags: set[str] = set()
    for match in _END_DATE_RE.finditer(text):
        masked.append(match.span())
        year, month = match.group(1), int(match.group(2))
        if month <= 3:
            tags.add(f"{year}Q1")
        elif month <= 6:
            tags.add(f"{year}H1")
        elif month <= 9:
            tags.add(f"{year}Q3")
        else:
            tags.add(f"{year}FY")
    for match in _YEAR_RE.finditer(text):
        if any(start <= match.start() < end for start, end in masked):
            continue
        year = match.group(1)
        window = text[max(0, match.start() - 8): match.end() + 12]
        if re.search(r"半年度|上半年|半年报|中期|H1", window):
            tags.add(f"{year}H1")
        elif re.search(r"第一季度|一季度|Q1", window):
            tags.add(f"{year}Q1")
        elif re.search(r"第三季度|三季度|Q3", window):
            tags.add(f"{year}Q3")
        else:
            tags.add(f"{year}FY")
    return tags


def _extract_share_claims(text: str, hypothesis_index: int) -> list[_ShareClaim]:
    """从 company_form 抽取「X 亿 + 占<base>P%」配对（P0-1A 的分子分母素材）。

    配对规则：同一句内，「占…%」向左找最近的「X 亿」，没有再向右找最近的；
    无分子 ⇒ 该声明不参与隐含总量校验（交由 P0-1B 出处检查兜底）。
    一句含多个期别 ⇒ 整句声明 ``period=None``（归属歧义，不参与跨句比较）。
    """
    claims: list[_ShareClaim] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text or ""):
        sentence_periods = sorted(_period_tags(sentence))
        if len(sentence_periods) > 1:
            continue  # 期别归属歧义：宁可漏检不可误报
        period = sentence_periods[0] if sentence_periods else ""
        for zhan in _ZHAN_RE.finditer(sentence):
            base = (zhan.group("base") or "").strip()
            if base in _ZHAN_BASE_STOP:
                continue
            amount: float | None = None
            for match in _AMOUNT_YI_RE.finditer(sentence):
                if match.end() <= zhan.start():
                    amount = float(match.group("value"))
                else:
                    break
            if amount is None:
                for match in _AMOUNT_YI_RE.finditer(sentence):
                    if match.start() >= zhan.end():
                        amount = float(match.group("value"))
                        break
            claims.append(
                _ShareClaim(
                    amount_yi=amount,
                    pct=float(zhan.group("pct")),
                    base=base,
                    period=period,
                    hypothesis_index=hypothesis_index,
                    snippet=sentence.strip()[:120],
                )
            )
    return claims


def _implied_total_contradictions(
    hypotheses: Sequence[ResearchHypothesis],
) -> list[dict[str, Any]]:
    """P0-1A：同一 (基数, 期别) 下「X 亿 + 占Y%」的隐含总量必须自洽。

    药明康德真实案例：H3「美国客户收入 140.3 亿、占集团收入 67.45%」⇒ 隐含集团 ≈ 208 亿；
    H1「化学业务收入 249.9 亿（占集团 86.46%）」⇒ 隐含集团 ≈ 289 亿——同一基数互相矛盾。
    本检查只**发现并降权**（无法判定孰对孰错），不替下游裁决。
    """
    groups: dict[tuple[str, str], list[_ShareClaim]] = {}
    for index, hypothesis in enumerate(hypotheses):
        for claim in _extract_share_claims(hypothesis.company_form, index):
            if claim.amount_yi and claim.amount_yi > 0 and claim.pct > 0:
                groups.setdefault((claim.base, claim.period or ""), []).append(claim)

    findings: list[dict[str, Any]] = []
    for (base, period), claims in sorted(groups.items()):
        if len(claims) < 2:
            continue
        totals = [claim.amount_yi / claim.pct * 100.0 for claim in claims]
        lo, hi = min(totals), max(totals)
        if lo <= 0 or (hi - lo) / lo <= _IMPLIED_TOTAL_TOLERANCE:
            continue
        findings.append(
            {
                "base": base,
                "period": period or "期别未标注",
                "implied_totals": [round(value, 1) for value in totals],
                "hypothesis_indices": sorted({claim.hypothesis_index for claim in claims}),
                "snippets": [claim.snippet for claim in claims],
            }
        )
    return findings
```

### 3.2 `converge()` 接线（C3 块）

见 §6 的统一接线 diff——P0-1A 的 findings 在 C3 开头计算，把涉及的假设序号登记进
`downgrade` 字典并各追加一条 `internal_inconsistency` issue。

---

## 4. P0-1B：数字出处强度（C2.1）

### 4.1 新增函数（与 §3.1 同区插入）

```python
@dataclass
class _NumericClaim:
    """company_form 里的一条财务类数字声明（P0-1B 的声明单元）。"""

    value: str           # 原样数值串（issue 展示）
    unit: str
    period: str          # 单一期别标签；"" = 未标注（同值即算有出处）
    in_share_clause: bool  # 该数字位于「占…%」从句内部 ⇒ 无出处时升级为降权
    snippet: str


def _normalize_number(text: str) -> str:
    """归一化数值串（去尾零）："140.3" 与 "140.30" 视为同值。"""
    value = (text or "").strip()
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value or "0"


def _input_pack_numbers(research_input: Mapping[str, Any] | None) -> dict[str, list[float]]:
    """输入包中的权威数值（P0-1B 出处兜底：结构化事实与证据同权）。

    ``pct`` 桶：financials 全部值 + 分部 revenue_share / gross_margin；
    ``amount_yi`` 桶：分部 revenue（元）折算亿元。
    """
    out: dict[str, list[float]] = {"pct": [], "amount_yi": []}
    if not research_input:
        return out
    financials = research_input.get("financials")
    if isinstance(financials, Mapping):
        for value in financials.values():
            if isinstance(value, int | float):
                out["pct"].append(float(value))
    items = research_input.get("latest_segments")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            for key in ("revenue_share", "gross_margin"):
                value = item.get(key)
                if isinstance(value, int | float):
                    out["pct"].append(float(value))
            revenue = item.get("revenue")
            if isinstance(revenue, int | float) and revenue > 0:
                out["amount_yi"].append(float(revenue) / 1e8)
    return out


def _uncovered_claims(
    hypothesis: ResearchHypothesis,
    research_input: Mapping[str, Any] | None,
) -> list[_NumericClaim]:
    """P0-1B：company_form 的财务类数字声明必须有出处。

    出处判定（按序，任一命中即视为覆盖）：
    1. **证据同值同期**：数值出现在任一 evidence 的 quote/ref 中，且声明期别
       ∈ 证据期别集（未标注期别的声明只看同值）；
    2. **输入包同值**：数值与输入包权威值之差在容差内（期别无关：输入包即结构化事实）。
    规则：运营单位（倍/kL/个）不查；一句含多个期别 ⇒ 整句跳过；数字 0 跳过。
    """
    evidence_periods: set[str] = set()
    evidence_numbers: set[str] = set()
    for item in hypothesis.evidence:
        evidence_periods |= _period_tags(item.ref)
        evidence_periods |= _period_tags(item.quote)
        # 裸数值抽取：tushare ref（"netprofit_margin=42.53"）与 quote 中的数字都收进来；
        # 顺带收进的 symbol/end_date 等无害串不会与任何财务声明同值。
        for match in _RAW_NUMBER_RE.finditer(f"{item.ref} {item.quote}"):
            evidence_numbers.add(_normalize_number(match.group("value")))

    input_numbers = _input_pack_numbers(research_input)
    uncovered: list[_NumericClaim] = []
    for sentence in _SENTENCE_SPLIT_RE.split(hypothesis.company_form or ""):
        sentence_periods = sorted(_period_tags(sentence))
        if len(sentence_periods) > 1:
            continue  # 期别归属歧义：宁可漏检不可误报
        period = sentence_periods[0] if sentence_periods else ""
        zhan_spans = [match.span() for match in _ZHAN_RE.finditer(sentence)]
        for match in _NUMBER_RE.finditer(sentence):
            unit = match.group("unit")
            if unit in _IGNORED_UNITS:
                continue
            normalized = _normalize_number(match.group("value"))
            if normalized == "0":
                continue
            # ① 证据同值同期
            if normalized in evidence_numbers and (not period or period in evidence_periods):
                continue
            # ② 输入包同值
            numeric = float(match.group("value"))
            if unit in {"%", "％"}:
                if any(abs(numeric - ref) <= _INPUT_PCT_TOLERANCE_PP for ref in input_numbers["pct"]):
                    continue
            elif unit.startswith("亿"):
                if any(ref > 0 and abs(numeric - ref) / ref <= _INPUT_AMOUNT_TOLERANCE
                       for ref in input_numbers["amount_yi"]):
                    continue
            elif unit.startswith("万"):
                amount_yi = numeric / 1e4
                if any(ref > 0 and abs(amount_yi - ref) / ref <= _INPUT_AMOUNT_TOLERANCE
                       for ref in input_numbers["amount_yi"]):
                    continue
            uncovered.append(
                _NumericClaim(
                    value=match.group("value"),
                    unit=unit,
                    period=period,
                    in_share_clause=any(start <= match.start() < end for start, end in zhan_spans),
                    snippet=sentence.strip()[:120],
                )
            )
    return uncovered
```

**降权规则（关键）**：只有当无出处数字**位于「占…%」从句内部**（即占比值本身，如 H3 的 67.45%）
才降权；句子里的其他数字（如 H1 的 56.55% 不在占比从句内）即使无出处也只留痕不降权——
避免"同句有一个无出处数字就连坐整条假设"的误伤。

### 4.2 `converge()` 接线（C2 之后新增 C2.1）

见 §6。

---

## 5. P0-2：毛利率校验的口径感知（重写 `_margin_contradictions`）

### 5.1 新增辅助函数 + 重写主函数（替换现 857–868 行的实现）

```python
def _segment_margins(research_input: Mapping[str, Any] | None) -> dict[str, float]:
    """最新报告期分部毛利率（P0-2：分部口径校验的基准；无 ⇒ 空表 = 不校验分部口径）。"""
    if not research_input:
        return {}
    items = research_input.get("latest_segments")
    if not isinstance(items, list):
        return {}
    margins: dict[str, float] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("segment_name") or "").strip()
        margin = item.get("gross_margin")
        if name and isinstance(margin, int | float):
            margins[name] = float(margin)
    return margins


def _without_spans(text: str, spans: Sequence[tuple[int, int]]) -> str:
    """挖掉给定区间后的残余文本（具名毛利率已被口径分流，无名检查只看残余）。"""
    out: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        out.append(text[cursor:start])
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def _margin_contradictions(
    texts: Sequence[str],
    financials: Mapping[str, float],
    segment_margins: Mapping[str, float] | None = None,
) -> list[str]:
    """C3（P0-2 修复）：毛利率**口径感知**校验——分部值与集团值不再混比。

    - 「<名>业务毛利率 P%」：只与**同名分部**的毛利率基准比；无同名分部基准 ⇒ 跳过
      （不比较 ≠ 不矛盾；旧实现拿分部值比集团值，是药明康德 FX 假设误报的根源）；
    - 「毛利率 P%」（无名）与集团口径（集团/公司/整体/当年…）：与 ``financials.gross_margin`` 比
      （保留原行为）。
    """
    segment_margins = segment_margins or {}
    actual_group = financials.get("gross_margin")
    conflicts: list[str] = []
    for text in texts:
        named_spans: list[tuple[int, int]] = []
        for match in _MARGIN_NAMED_RE.finditer(text or ""):
            named_spans.append(match.span())
            name = (match.group("name") or "").strip()
            claimed = float(match.group("value"))
            if any(_contains(name, scope) for scope in _GROUP_SCOPE_NAMES):
                if actual_group is not None and abs(claimed - actual_group) > _MARGIN_TOLERANCE_PP:
                    conflicts.append(f"集团毛利率: 假设声称 {claimed:.1f}% vs 实测 {actual_group:.1f}%")
                continue
            actual = None
            for segment_name, margin in segment_margins.items():
                if _contains(name, segment_name):
                    actual = margin
                    break
            if actual is None:
                continue  # 未知分部/无分部基准 ⇒ 跳过（不误报）
            if abs(claimed - actual) > _MARGIN_TOLERANCE_PP:
                conflicts.append(f"{name}毛利率: 假设声称 {claimed:.1f}% vs 实测 {actual:.1f}%")
        if actual_group is None:
            continue
        residual = _without_spans(text or "", named_spans)
        for match in _MARGIN_RE.finditer(residual):
            claimed = float(match.group("value"))
            if abs(claimed - actual_group) > _MARGIN_TOLERANCE_PP:
                conflicts.append(f"毛利率: 假设声称 {claimed:.1f}% vs 实测 {actual_group:.1f}%")
    return conflicts
```

**签名向后兼容**：`segment_margins` 为新增可选参数，现有调用方零改动；
现有测试若直接调旧签名（2 参）行为不变（`segment_margins` 空表 ⇒ 具名分部匹配不到 ⇒ 走旧路径）。

> **本代码块为初始方案；实现的授权偏离（§1 末尾登记，R1/R2 审查要求）如下**——阅读实现请以
> `alphabee/domain_context/research.py` 为准：
> - `_MARGIN_NAMED_RE` 的 `(?:业务|分部)?` 改为 `(?P<suffix>业务|分部)?`（匹配语义不变，仅供文案）；
> - 新增 `_PERIOD_CONTEXT_TOKENS` / `_PERIOD_TOKEN_RE` / `_PERIOD_QUANTIFIER_CHARS` /
>   `_MIN_BUSINESS_NAME_LEN` / `_is_business_name()` / `_strip_period_tokens()`；
> - 循环里 name 先 `_strip_period_tokens(match.group("name"))`：**剥空（含期别量词字符残余、<2 字）⇒
>   `continue` 且不记 named span**，交回残余文本按无名/集团口径与集团值比；否则用剥离后的业务名做
>   集团口径判定与 `segment_margins` 同名比对；
> - 分部冲突文案：`f"{name}{suffix}毛利率: 假设声称 …"`。
> 语义结果：`生物学业务毛利率34.92%`（基准 34.9）无冲突、基准 60 有冲突、`TIDES业务毛利率45%`
> 无 TIDES 基准仍跳过、集团/当年口径仍与集团值比；`2025年毛利率30%`+集团 53.9 ⇒ 报冲突（初始方案漏检）。

---

## 6. `converge()` 统一接线（替换现 C2 尾部 + C3 块，约 986–1013 行）

```python
    # ── C2：证据可溯（无证据者进 unverified_drivers，绝不进 main_driver）──
    verified: list[ResearchHypothesis] = []
    unverified: list[str] = []
    for hypothesis in clean:
        if any((evidence.ref or "").strip() for evidence in hypothesis.evidence):
            verified.append(hypothesis)
        else:
            unverified.append(hypothesis.variable)

    # ── C2.1：数字出处强度（P0-1B）──────────────────────────────────────
    # ref 非空只保证"有引用"，不保证 company_form 里的数字被引用覆盖
    # （药明康德 H3：2026H1 的 140.3 亿 / 67.45% 只有一条 2025 年报 quote 支撑）。
    uncovered_map: dict[int, list[_NumericClaim]] = {
        index: _uncovered_claims(hypothesis, research_input)
        for index, hypothesis in enumerate(verified)
    }

    # ── C3：事实交叉校验（矛盾 ⇒ 降权 + 显式 issue，不静默）────────────
    shares = _segment_shares(research_input)
    financials = _financial_facts(research_input)
    segment_margins = _segment_margins(research_input)     # P0-2 新增
    consistency = _implied_total_contradictions(verified)  # P0-1A 新增
    downgrade: dict[int, list[str]] = {}

    # P0-1A：同一基数/期别的占比声明互相矛盾 ⇒ 相关假设同时降权
    for finding in consistency:
        for index in finding["hypothesis_indices"]:
            downgrade.setdefault(index, []).append(
                f"内部不一致：基数「{finding['base']}」（{finding['period']}）"
                f"隐含总量 {finding['implied_totals']} 亿"
            )
        issues.append(
            {
                "severity": "medium",
                "category": "driver_profile_research_internal_inconsistency",
                "message": (
                    f"同一基数「{finding['base']}」（{finding['period']}）的占比声明互相矛盾："
                    f"「{'」 vs 「'.join(finding['snippets'])}」⇒ 隐含总量分别为 "
                    f"{finding['implied_totals']} 亿（相对差 > {_IMPLIED_TOTAL_TOLERANCE:.0%}）。"
                    "无法判定孰对孰错，相关假设已同时降权，请下游据此裁决。"
                ),
            }
        )

    # P0-1B：占比从句内的无出处数字 ⇒ 降权；其余无出处数字只留痕
    for index, claims in uncovered_map.items():
        if not claims:
            continue
        escalated = [claim for claim in claims if claim.in_share_clause]
        if escalated:
            downgrade.setdefault(index, []).append(
                "数字无出处："
                + "；".join(
                    f"{claim.value}{claim.unit}（{claim.period or '期别未标注'}）"
                    for claim in escalated
                )
            )
        issues.append(
            {
                "severity": "medium",
                "category": "driver_profile_research_claim_uncovered",
                "message": (
                    f"假设「{verified[index].variable}」的 company_form 有 {len(claims)} 处"
                    "财务数字声明无出处（证据同值同期不覆盖，输入包也无同值）："
                    + "；".join(
                        f"{claim.value}{claim.unit}（{claim.period or '期别未标注'}）：{claim.snippet}"
                        for claim in claims
                    )
                    + ("。其中含占比从句数字，已降权。" if escalated else "。")
                ),
            }
        )

    checked: list[ResearchHypothesis] = []
    for index, hypothesis in enumerate(verified):
        texts = [hypothesis.company_form, hypothesis.variable, *hypothesis.observables]
        conflicts = _share_contradictions(texts, shares) + _margin_contradictions(
            texts, financials, segment_margins
        )
        if conflicts or downgrade.get(index):
            hypothesis = hypothesis.model_copy(
                update={"confidence": min(hypothesis.confidence, _CONTRADICTION_MAX_CONFIDENCE)}
            )
        if conflicts:
            issues.append(
                {
                    "severity": "medium",
                    "category": "driver_profile_research_fact_conflict",
                    "message": f"{hypothesis.variable}: " + "；".join(conflicts),
                }
            )
        checked.append(hypothesis)
```

> 之后的 C4/C5/C6 块不动。注意：`_share_contradictions`（"名称占比"形态）本身不改——
> "占集团 86.46%"这类分母形态**不属于**分部占比语义，交给 P0-1A 的隐含总量校验处理，
> 混入分部占比校验会产生语义错配（"占化学业务收入 29.0%"的分母是化学业务，而不是
> "化学业务的占比"）。
>
> **实现同步（R1/R2 授权偏离，以代码为准）**：C2.1 与 C3 的接线与上面代码块逐行一致
> （`uncovered_map` → `downgrade` + 两条 issue；降权条件 `conflicts or downgrade.get(index)`），
> 唯一差异在 `_margin_contradictions` 内部——它对 name 先做期别前缀剥离与"非业务名"兜底归零
> （§5 末尾与 §9 第 6/7 条），因此 C3 传给它的 `texts`/`financials`/`segment_margins` 不变，
> 而"期别标签当前缀"的毛利率句不再被静默吞掉（回到集团口径比较）。

---

## 7. 偏离账本与测试锚同步（必须同期）

| 文件 | 改动 |
|---|---|
| `alphabee/orchestrator/services/deviation.py`（现 92/126 行附近） | 追加两行分类登记：`"driver_profile_research_internal_inconsistency": DeviationClass.D3_ARGUMENT`、`"driver_profile_research_claim_uncovered": DeviationClass.D3_ARGUMENT`（与既有 `driver_profile_evidence_missing: D3_ARGUMENT` 同族——都是"论点可信度"层面缺陷） |
| `tests/orchestrator/test_deviation_service.py` | `_EXPECTED_CLASS_BY_CATEGORY`（55/86 行附近）与 `test_real_world_category_extension`（345 行附近的参数化）同步补两条 |
| `docs/design/DEVIATION_CONTROL_FRAMEWORK.md` | 分类表补两行（仓库惯例：category → 类别的文档表与代码同源） |

> `research_meta["issues"]` 的 dict 会在 `finalize_research_artifact`（research.py:1161）中
> 随 artifact 落地，节点侧再转成正式 `Issue`——新 category 走同一条路径，无需改节点。

---

## 8. 测试计划（新增 `tests/domain_context/test_research_veracity.py`）

沿用既有测试范式（`tests/domain_context/test_research.py`：纯函数 + `ResearchHypothesis` 构造 +
`converge()` 增量断言，**无 LLM**）。关键是把药明康德的两个真实缺陷做成**回归 fixture**：

### 8.1 P0-1A（`_implied_total_contradictions` + converge 降权）

| 用例 | 构造 | 断言 |
|---|---|---|
| `test_wuxi_implied_total_inconsistency_downgrades_both` | H1：「2026H1 化学业务收入249.9亿（占集团86.46%）…」；H3：「2026H1 美国客户收入140.3亿、占集团收入67.45%（同比+31.0%）…」 | 两假设 conf 均被压到 ≤0.3；`issues` 含 `driver_profile_research_internal_inconsistency`，message 含「208」「289」两个隐含总量 |
| `test_consistent_implied_totals_no_issue` | 同基数同期别两条声明都隐含 200 亿（如 100亿/50% 与 120亿/60%） | 无该 category issue、不降权 |
| `test_different_periods_are_not_compared` | 同一基数、一条 2026H1 一条 2025FY | 无 issue |
| `test_ambiguous_period_sentence_is_skipped` | 一句含「2026H1…2025年…」两个期别 | 无 issue（宁可漏检） |
| `test_share_claim_without_amount_is_ignored` | 「占集团 50%」但同句无「X 亿」 | 无 issue |
| `test_zhan_base_stop_words_are_ignored` | 「占比86.46%」形态 | 无 issue（base="比" 被过滤） |

### 8.2 P0-1B（`_uncovered_claims` + converge 降权/留痕）

| 用例 | 构造 | 断言 |
|---|---|---|
| `test_wuxi_uncovered_share_claim_downgrades`（**回归 fixture**） | H3 的 company_form + 只有一条 2025 年报 quote 的 evidence | `issues` 含 `driver_profile_research_claim_uncovered`；H3 conf ≤ 0.3（67.45% 位于占比从句内） |
| `test_claim_covered_by_evidence_same_period_no_issue` | 声明「2026H1…149.9亿」+ quote 含 149.9 且 ref 为 2026年半年度报告 | 无该 category issue |
| `test_claim_covered_by_unitless_tushare_ref` | 声明「2025年…净利率42.53%」+ evidence=`kind=tushare, ref="fina_indicator 603259.SH end_date=20251231 netprofit_margin=42.53"` | 无 issue（裸数值抽取覆盖无单位形态，钉死 §4 的实现坑） |
| `test_claim_covered_by_input_pack_no_issue` | 声明「占集团86.46%」+ `research_input.latest_segments` 含 `化学业务 revenue_share=86.5` | 无 issue（输入包兜底，容差 2pp 内） |
| `test_untagged_claim_covered_by_number_match` | 声明无期别 + 任一 quote 同值 | 无 issue（未标注期别只看同值） |
| `test_operational_units_ignored` | 「反应釜总体积>4,000kL」 | 无 issue（kL 在忽略单位集） |
| `test_uncovered_non_share_claim_issue_without_downgrade` | 「2026H1 毛利率56.55%（同比+7.48pp）」无出处、句子无占比从句 | 有 issue；conf **不**降权 |

### 8.3 P0-2（`_margin_contradictions` 口径分流）

| 用例 | 构造 | 断言 |
|---|---|---|
| `test_wuxi_segment_margin_no_false_conflict`（**回归 fixture**） | 「生物学业务毛利率34.92%，同比-0.12pp」+ `segment_margins={"生物学业务": 34.9}` + `financials={"gross_margin": 53.9}` | **无冲突**（旧实现必然误报，此用例钉死修复） |
| `test_segment_margin_conflict_when_really_deviates` | 同上但分部基准 60.0 | 有冲突 + 降权 |
| `test_unknown_segment_margin_skipped` | 「TIDES业务毛利率45%」+ segments 无 TIDES | 无冲突（跳过而非与集团值比） |
| `test_group_scoped_margin_still_checked` | 「当年毛利率47.64%」+ 集团 53.9（差 ≤15pp）→ 无冲突；「集团毛利率20%」→ 有冲突 | 集团口径路径保留原行为 |
| `test_bare_margin_behavior_unchanged` | 无名「毛利率 40%」+ 集团 53.9 | 有冲突（>15pp，原行为） |

### 8.4 `_period_tags` 单元测试

`2026年半年度报告`→{2026H1}；`2025年年度报告`→{2025FY}；`end_date=20260630`→{2026H1}；
`end_date=20251231 / 20260630`→{2025FY, 2026H1}；`2026年收入指引`→{2026FY}。

### 8.5 既有测试回归

- `tests/domain_context/`（现 26 + 研究层用例）必须全绿——**预期零改动**：
  `_margin_contradictions` 新参数可选、旧调用路径行为不变；C3 的 share 校验未动；
  converge 现有 issue 断言不涉及新 category。
- `tests/orchestrator/test_deviation_service.py`：**预期改动**（§7 两行期望字典/参数化）。
- 运行方式提醒：`HOME=<可写目录> .venv/bin/python -m pytest tests/domain_context`（tushare
  `set_token` 写 `~/tk.csv`，不可写 HOME 会中断 collection）。

---

## 9. 已知局限（保守漏检清单，均为"宁可漏检"取向）

1. 「占持续经营收入 434.2 亿的约 72%」这种**分母金额在前、占比在后**的形态不覆盖
   （`_ZHAN_RE` 只匹配相邻形态）——漏检不误报。
2. 一句含多个期别的声明整句跳过（P0-1A 与 P0-1B 同规则）。
3. 无分子（同句找不到「X 亿」）的占比声明不做隐含总量校验，只靠 P0-1B 出处检查兜底。
4. P0-1B 的"证据同值"只做集合匹配，不校验数值与期别在**同一条** evidence 内的对应关系
   （如 ref 同时列了 2025/2026 两期数值时，只要求"数值出现过 + 期别出现过"）。
5. 万→亿换算、输入包兜底容差（2pp / 3%）可能放过少量边缘口径差异——容差是刻意放宽的，
   收敛层目标是抓"差一个数量级"的矛盾，不追 1pp 级口径差。
6. **期别标签恰好等于分部名**（分部就叫 `H1`/`H2`/`Q3`）时，该名会被期别词表剥成"非业务名"，
   从而按无名/集团口径与集团值比，而**不是**与同名分部基准比——与修复前（HEAD）行为一致，
   属防漏检增强带来的已知退化边界（真实分部名不会取报告期标签，故判为可接受）。
7. **分部名含 `_MARGIN_NAMED_RE` 字符类之外的字符**（如「小分子D&M」的 `&`）时无法被具名匹配，
   整句按无名回退与集团值比——同样与 HEAD 行为一致；若该句数字与集团值差 > 15pp 会报一条
   `fact_conflict`（口径错配方向的误报），需按 §8.3 的口径分流补 `segment_margins` 或后续放宽
   字符类才能消除。

---

## 10. 落地顺序与验收

1. **改代码**：§2 常量 → §3/§4/§5 函数 → §6 `converge` 接线（一个提交，逻辑内聚）。
2. **同步登记**：§7 三处（deviation.py + 测试锚 + 框架文档表）。
3. **补测试**：§8 新文件 + 跑全量 `tests/domain_context` + `tests/orchestrator/test_deviation_service.py`。
4. **真机回归（可选但推荐）**：对 `data/driver_profiles/603259.SH.json` 的缓存输入重放
   `converge()`，断言两条预期：
   - H3（美国客户）与 H1（化学业务）conf 被压到 ≤0.3，且各产出一条 `internal_inconsistency`；
   - FX 假设（生物学毛利率）**不再**出现 `fact_conflict` 误报（`_margin_contradictions` 跳过）。
   若缓存输入包不可得，用 §8.1/§8.3 的回归 fixture 等效替代。

**验收判据**：新测试全绿 + 既有 domain_context 测试零改动全绿 + 重放药明康德 case
两条预期成立 + 偏离分类登记同步。
