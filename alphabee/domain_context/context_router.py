"""ContextRouter（DOMAIN_CONTEXT_ROADMAP P0 第 3 步）——规则版公司 → playbook 匹配。

确定性、可单测的公司→框架路由：用 `track_label` / `industry` / `sub_industry` /
`business_model`（archetype）对 playbook 的 ``match_*`` 字段打分，命中最高分 playbook
并展开为其 primitive 集合；无命中时回退 ``generic_fundamental``。

设计约定：
- **映射表 = playbook 的 ``match_*`` 字段**（而非独立 router_mapping.yaml）。它们已随
  ``PlaybookSchema`` 拥有 schema + 版本，数据驱动、非硬编码——这是 Review 问题 #1 的落地。
- **business_model 只作低权输入信号**（权重 1），不产 playbook、不竞争（见
  DOMAIN_CONTEXT_ROADMAP「与 business_model archetype 的边界」）。
- 命中打分：track_label=3 > sub_industry/industry=2 > business_model=1；同分按 playbook id 决平。
- P0 不评分、不排序：``activated_contexts`` 的 ``score`` 统一 1.0，``trend`` 统一 "stable"（P2 引入）。

业务逻辑（为什么这样设计）：
- **路由回答的是「这家公司的盈利由什么驱动」，而不是「它属于哪个行业」**。所以匹配的核心
  依据是公司「真实赛道」（track_label，来自业务线收入解构）而非申万行业标签——工业富联按
  申万是「通信设备」，但真实驱动是 AI 服务器，若按申万匹配就会把分析框架带偏。
- **fallback 与 degraded 是两种本质不同的情况**，必须在投资流水线里分开对待（见 ``route``）：
  普通未命中（大多数公司）是常态、静默回退通用框架即可；输入缺失（数据问题）必须留痕，
  否则下游会把「没有数据」误判成「真的没有专用框架」。
- **P0 只做「命中/未命中」的硬路由，不做「适用程度」的软评分**（score 统一 1.0）：先把
  「报告主线切换」这条最小闭环跑通，context score/ranking/趋势（P2）再叠加。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from alphabee.domain_context.loader import load_playbooks
from alphabee.domain_context.schemas import PlaybookSchema

GENERIC_FALLBACK_ID = "generic_fundamental"

# ── 命中信号权重：反映「身份信号的业务可信度」，而非简单计数 ─────────
# track_label（公司真实赛道，来自业务线收入解构，company_track 产出）最贴近公司实际业务，
#   最可信 → 权重最高 3。
# industry/sub_industry（申万分类，统计口径）比赛道粗，可能掩盖真实增长引擎 → 权重 2。
# business_model（archetype，由毛利率/研发费率等财务结构「推断」）最泛化、且可能误判，
#   只能作弱佐证 → 权重最低 1。
# 权重落差的意义：当赛道与申万行业「打架」时（工业富联 track=AI 服务器 vs 申万=通信设备），
# 以更可信的赛道为准，而不是被申万粗分类误导到错误的分析框架。
_WEIGHT_TRACK_LABEL = 3
_WEIGHT_SUB_INDUSTRY = 2
_WEIGHT_BUSINESS_MODEL = 1

# 结构信号（申万代码 / 分部结构 / 财务结构）各 +1：它们不是"更高可信度的身份信号"，
# 而是**锚强度的判据**——用于区分「靠真实赛道/精确代码命中」（强锚）与「靠宽行业名命中」
# （弱锚）。权重与会变动的身份信号解耦，避免改变既有打分语义。
_WEIGHT_STRUCTURE_SIGNAL = 1


class ActivatedContext(BaseModel):
    """一个已激活的分析原语（playbook 已展开为 primitive）。"""

    context: str  # primitive id
    score: float = 1.0  # P0 统一 1.0；P2 引入 context score/ranking
    trend: str = "stable"


class RouterInput(BaseModel):
    """ContextRouter 的输入（全部来自已落地产物，不重复取数）。"""

    symbol: str = ""
    track_label: str = ""  # COMPANY_TRACK.track_label
    industry: str = ""  # INDUSTRY_CONTEXT.industry
    sub_industry: str = ""  # INDUSTRY_CONTEXT.sub_industry
    business_model: str = ""  # COMPANY_TRACK.business_model（archetype）
    business_model_summary: str = ""  # 公司业务描述（P0 暂不参与匹配，保留字段）
    # ── 结构信号（全部来自已落地产物，供锚强度判据与结构性兜底驱动使用）──
    sw_code: str = ""  # INDUSTRY_CONTEXT.sw_code（申万 L1/L2/L3 代码，如 801010.SI）
    dominant_segment: str = ""  # COMPANY_TRACK.dominant_segment
    dominant_share: float | None = None  # 主力分部收入占比（0~100）
    fastest_segment: str = ""  # 最快增速分部
    fastest_yoy: float | None = None  # 最快分部同比（%）
    segment_summary: list[str] = Field(default_factory=list)  # ["云计算/服务器 42%(+58%)", ...]
    financial_structure: dict[str, float] = Field(default_factory=dict)  # gross_margin/rnd_ratio/…

    def has_identity_signals(self) -> bool:
        """是否携带任何可用于匹配的身份信号（全空 = 输入缺失，应标记降级）。"""
        return bool(
            (self.track_label or "").strip()
            or (self.industry or "").strip()
            or (self.sub_industry or "").strip()
            or (self.business_model or "").strip()
        )


class RouterResult(BaseModel):
    """ContextRouter 输出：命中的 playbook + 展开后的 primitive + 匹配理由 + 降级标记。"""

    playbook_id: str = ""
    playbook_version: int = 1
    activated_contexts: list[ActivatedContext] = Field(default_factory=list)
    # 主/次驱动变量（变量名，如「猪价」「能繁母猪」，用于报告主线；非 primitive id）
    primary_drivers: list[str] = Field(default_factory=list)
    secondary_drivers: list[str] = Field(default_factory=list)
    why_selected: list[str] = Field(default_factory=list)
    # 锚定强度（strong / weak / none）：表达"靠什么命中的"。strong = 靠真实赛道或申万代码
    # 精确前缀命中，且公司结构事实不与框架声明矛盾；weak = 只有 archetype 或宽行业名命中
    # （生产环境 sub_industry 恒空，用它识别"仅一级行业"命中）；none = 无命中（走 fallback）。
    anchor_strength: str = ""
    fallback: bool = False  # True = 未命中任何专用 playbook，回退 generic_fundamental
    degraded: bool = False  # True = 输入缺失导致无法正常匹配（非"普通无命中"）
    degraded_reason: str = ""


def _contains(a: str, b: str) -> bool:
    """双向子串匹配（大小写/空白不敏感）。

    业务动机：赛道标签与 playbook 匹配词存在「口径不一致」——同一个东西有不同叫法：
    ``"生猪养殖"`` vs ``"养殖"`` vs ``"养猪"``。单向 ``in`` 会漏配（如 track_label="养猪"
    匹配不到 match="养殖"）。双向包含（任一包含另一方）能把这类同义叫法都兜住，
    代价是偶尔误配，但对「路由到分析框架」这类低风险匹配是可接受的取舍。
    """
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return False
    return a in b or b in a


def _threshold_hit(fin: dict[str, float], spec: dict[str, dict[str, float]]) -> bool:
    """财务结构阈值判定：``spec`` 形如 ``{"inventory_ratio": {"gt": 0.25}}``。

    ``{"gt": x}`` / ``{"lt": x}`` 为字段级比较规则；声明多个字段时需全部命中。
    缺失字段视为未命中（而非 0）："没拿到这个财务口径"与"这个口径不达标"是两件事，
    但都不构成"命中该框架的财务形态"。空 spec 不命中（避免无声明即命中）。
    """
    if not spec:
        return False
    for field, rule in spec.items():
        value = fin.get(field)
        if value is None:
            return False
        if "gt" in rule and not value > rule["gt"]:
            return False
        if "lt" in rule and not value < rule["lt"]:
            return False
    return True


def _sw_code_matches(inp: RouterInput, pb: PlaybookSchema) -> bool:
    """申万代码前缀命中（L1/L2/L3 均可，代码自带层级）。

    业务动机：行业「名称」匹配会被分类改名/口径差异影响（申万"电力设备" vs 证监会
    "电气设备"），代码则稳定；且代码能把"宽 L1 行业名命中"与"精确行业命中"分开。
    """
    return bool(inp.sw_code) and any(inp.sw_code.startswith(code) for code in pb.match_sw_codes if code)


def _segment_candidates(inp: RouterInput) -> list[str]:
    return [inp.dominant_segment, inp.fastest_segment, *inp.segment_summary]


def _segment_matches(inp: RouterInput, pb: PlaybookSchema) -> bool:
    """分部结构命中（主力/最快分部名 + 分部摘要，复用 ``_contains`` 双向匹配）。"""
    if not pb.match_segments:
        return False
    candidates = [c for c in _segment_candidates(inp) if (c or "").strip()]
    return any(_contains(c, s) for c in candidates for s in pb.match_segments)


def _financial_structure_matches(inp: RouterInput, pb: PlaybookSchema) -> bool:
    """财务结构阈值命中（声明多组阈值时需全部命中）。"""
    return _threshold_hit(inp.financial_structure, pb.match_financial_structures)


def _structure_conflicts(inp: RouterInput, pb: PlaybookSchema) -> bool:
    """框架声明的结构条件与公司结构事实是否矛盾。

    只在**公司确实提供了该维度数据**时才算矛盾（拿不到数据 ≠ 矛盾，否则几乎所有缺
    分部数据的公司都会被降级）；playbook 未声明该维度时一律视为不矛盾。
    """
    if pb.match_segments and any((c or "").strip() for c in _segment_candidates(inp)):
        if not _segment_matches(inp, pb):
            return True
    if pb.match_financial_structures and inp.financial_structure:
        if not _financial_structure_matches(inp, pb):
            return True
    return False


def _derive_anchor_strength(inp: RouterInput, pb: PlaybookSchema, reasons: list[str]) -> str:
    """据命中理由推导锚定强度（strong / weak / none）。

    - strong：靠真实赛道（track_label）或申万代码精确前缀命中，且结构事实不与框架矛盾；
    - weak：只有 archetype（business_model）或只有宽行业名命中，或强信号被结构事实推翻
      —— 仍然采用该框架，但 D1 研究层需要复核；
    - none：无任何命中（走 fallback 路径）。

    注意：weak 的"仅一级行业命中"用 ``sub_industry == ""`` 识别，**不依赖 sub_industry
    字段值**（生产环境该字段恒空，行业名只在 industry 里）。
    """
    if not reasons:
        return "none"
    if "track_label_match" in reasons or "sw_code_match" in reasons:
        if not _structure_conflicts(inp, pb):
            return "strong"
    return "weak"


def _structural_drivers(inp: RouterInput) -> list[str]:
    """从公司结构化事实确定性推导兜底框架的主驱动（D0-c）。

    业务动机：兜底框架 ``generic_fundamental`` 的 ``primary_drivers`` 是空的，导致画像
    出现「驱动: —」。与其编造，不如把**已经落地的结构事实**转述成驱动变量（source 口径
    内嵌在字符串里，如"主力分部 …收入占比 …%"）。没有结构事实就返回空列表，由调用方
    回退旧行为——**绝不编造**。
    """
    drivers: list[str] = []
    if inp.dominant_segment and inp.dominant_share is not None:
        drivers.append(f"主力分部 {inp.dominant_segment} 收入占比 {inp.dominant_share:.0f}%")
    if inp.fastest_segment and inp.fastest_yoy is not None:
        drivers.append(f"{inp.fastest_segment} 同比 {inp.fastest_yoy:.0f}%（快于主力）")
    gm = inp.financial_structure.get("gross_margin")
    bench = inp.financial_structure.get("peer_gross_margin")
    if gm is not None and bench is not None:
        drivers.append(f"毛利率 {gm:.1f}%（行业基准 {bench:.1f}%，差 {gm - bench:+.1f}pp）")
    return drivers


def _score_playbook(inp: RouterInput, pb: PlaybookSchema) -> tuple[int, list[str]]:
    """对单个 playbook 打分，返回 (score, 命中理由)。score=0 表示未命中。

    业务语义：
    - track_label 命中：公司真实赛道（如"生猪养殖"）直接落在该框架的适用范围内，最可信；
    - sw_code 命中：申万代码前缀精确命中（比行业名匹配稳定，也更能区分层级）；
    - industry/sub_industry 命中：申万行业落在框架范围内（如"养殖业"），次可信；
    - segment/financial_structure 命中：公司的分部结构/财务形态与框架声明一致；
    - business_model 命中：archetype 恰好匹配（如"integrator"），最弱的佐证。
    每个信号维度只加一次分（任一关键词命中即可），避免同一 playbook 因为写了多个同义
    匹配词而重复加分、虚高排名。
    """
    score = 0
    reasons: list[str] = []

    if inp.track_label and any(_contains(inp.track_label, t) for t in pb.match_track_labels):
        score += _WEIGHT_TRACK_LABEL
        reasons.append("track_label_match")

    if _sw_code_matches(inp, pb):
        score += _WEIGHT_STRUCTURE_SIGNAL
        reasons.append("sw_code_match")

    # industry / sub_industry 任一命中即 +2（不重复计）：申万一、二级任一层对上都算命中，
    # 因为 sub_industry 目前常为空（industry-context 只解析到申万一级），不能强求二级。
    industries = [x for x in (inp.industry, inp.sub_industry) if (x or "").strip()]
    if industries and any(_contains(x, s) for x in industries for s in pb.match_sub_industries):
        score += _WEIGHT_SUB_INDUSTRY
        reasons.append("sub_industry_match")

    if _segment_matches(inp, pb):
        score += _WEIGHT_STRUCTURE_SIGNAL
        reasons.append("segment_match")

    if _financial_structure_matches(inp, pb):
        score += _WEIGHT_STRUCTURE_SIGNAL
        reasons.append("financial_structure_match")

    if inp.business_model and inp.business_model in pb.match_business_models:
        score += _WEIGHT_BUSINESS_MODEL
        reasons.append("business_model_match")

    return score, reasons


def _build_result(
    playbook_id: str,
    playbook: PlaybookSchema,
    why_selected: list[str],
    *,
    fallback: bool,
    degraded: bool,
    degraded_reason: str = "",
    inp: RouterInput | None = None,
    anchor_strength: str = "",
) -> RouterResult:
    primary_drivers = list(playbook.primary_drivers)
    if fallback and inp is not None:
        # D0-c：兜底框架没有专用驱动变量，改用公司结构化事实确定性推导；
        # 无任何结构事实时保持旧行为（playbook 的空驱动），不编造。
        primary_drivers = _structural_drivers(inp) or primary_drivers
    return RouterResult(
        playbook_id=playbook_id,
        playbook_version=playbook.version,
        activated_contexts=[ActivatedContext(context=c) for c in playbook.primitives],
        primary_drivers=primary_drivers,
        secondary_drivers=list(playbook.secondary_drivers),
        why_selected=why_selected,
        anchor_strength=anchor_strength,
        fallback=fallback,
        degraded=degraded,
        degraded_reason=degraded_reason,
    )


def route(
    inp: RouterInput,
    playbooks: dict[str, PlaybookSchema] | None = None,
) -> RouterResult:
    """规则版路由：公司 → 命中 playbook（展开为 primitive）。

    Args:
        inp: 公司身份/结构信号（track_label / industry / sub_industry / business_model
            + sw_code / 分部结构 / 财务结构）。
        playbooks: 覆盖默认加载的 playbook（None 时用 ``load_playbooks()``，供测试注入）。

    Returns:
        ``RouterResult``。命中则置 ``anchor_strength``（strong/weak）；无命中时回退
        ``generic_fundamental`` 并置 ``fallback=True`` + ``anchor_strength="none"``；
        身份信号全空时额外置 ``degraded=True``（输入缺失，区别于"普通无命中"）。
    """
    playbooks = playbooks if playbooks is not None else load_playbooks()

    scored: list[tuple[int, str, PlaybookSchema, list[str]]] = []
    for playbook_id, playbook in playbooks.items():
        if playbook_id == GENERIC_FALLBACK_ID:
            continue
        score, reasons = _score_playbook(inp, playbook)
        if score > 0:
            scored.append((score, playbook_id, playbook, reasons))

    # 最高分优先，同分按 playbook id 决平（保证确定性）
    scored.sort(key=lambda item: (-item[0], item[1]))

    if scored:
        score, playbook_id, playbook, reasons = scored[0]
        return _build_result(
            playbook_id,
            playbook,
            reasons,
            fallback=False,
            degraded=False,
            inp=inp,
            anchor_strength=_derive_anchor_strength(inp, playbook, reasons),
        )

    # ── 未命中任何专用框架 → 兜底 ──────────────────────────────────────
    fallback = playbooks.get(GENERIC_FALLBACK_ID)
    if fallback is None:
        # 理论上不会发生（catalog 必含 generic_fundamental），防御性兜底
        return RouterResult(degraded=True, degraded_reason="generic_fallback_missing")

    # ── fallback vs degraded 的业务区分（投资流水线里必须分开）──────────
    # fallback（普通未命中）：公司有身份信号但命中不了专用框架（如白酒），这是「大多数
    #   公司」的常态路径，回退通用财务框架即可——不算异常、不产 issue、不报警。
    # degraded（输入缺失）：INDUSTRY_CONTEXT / COMPANY_TRACK 全缺（数据问题），必须置
    #   degraded + 由上层节点落 issue 留痕，否则下游会把「没有数据」误当成「真的没有专用
    #   框架」而静默用通用模板，掩盖数据链路断裂。
    degraded = not inp.has_identity_signals()
    return _build_result(
        GENERIC_FALLBACK_ID,
        fallback,
        why_selected=["no_playbook_matched"],
        fallback=True,
        degraded=degraded,
        degraded_reason="identity_signals_missing" if degraded else "",
        inp=inp,
        anchor_strength="none",
    )
