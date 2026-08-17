"""公司赛道数据契约（COMPANY_TRACK_ROADMAP Phase A）。

``SegmentSnapshot`` 是业务线解构的最小数据单元：一条（报告期 × 业务分项）记录，
字段全部为 canonical（见 ``alphabee/schemas/operation.yaml``），外部列名只在
adapter/采集层。Phase B 起的 ``CompanyTrackArtifact`` 将聚合本模型的列表。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SegmentSnapshot(BaseModel):
    """一条业务分项记录（报告期 × 分项）。"""

    report_date: str  # 报告期 YYYYMMDD（两数据源统一口径）
    segment_name: str  # 业务分项名（如 "云计算/服务器"、"通信网络设备"）
    category: str = ""  # 按产品 / 按行业 / 按地区
    revenue: float | None = None  # 分项营收（元）
    revenue_share: float | None = None  # 分项收入占比（%）
    revenue_yoy: float | None = None  # 分项同比增速（%，跨期推导）
    gross_margin: float | None = None  # 分项毛利率（%）
    cost: float | None = None  # 分项成本（元）
    profit: float | None = None  # 分项利润（元）
    is_calculated: bool = False  # share/yoy 是否由推导而非数据源直接给出
    source: str = ""  # 来源：em / tushare

    @property
    def key(self) -> tuple[str, str]:
        """（报告期, 分项名）——跨期 yoy 推导与去重的定位键。"""
        return (self.report_date, self.segment_name)


class SegmentCollection(BaseModel):
    """一次取数的完整结果（多报告期）。"""

    symbol: str = ""
    segments: list[SegmentSnapshot] = Field(default_factory=list)
    source: str = ""  # em / tushare / none
    latest_period: str = ""
    error: str | None = None

    def latest_segments(self) -> list[SegmentSnapshot]:
        """最新报告期的分项列表（Phase B 赛道标签推导的输入）。"""
        return [seg for seg in self.segments if seg.report_date == self.latest_period]


class CompanyTrackArtifact(BaseModel):
    """公司赛道 artifact（COMPANY_TRACK_ROADMAP §4.1）。

    Phase B 填充：segments / dominant_segment / fastest_segment / track_label /
    override_basis / 新鲜度元数据 / review_notes（含漂移）。
    Phase C-E 填充：business_model / peer_group / peer_benchmarks。
    B3 override 机制：``track_label``（公司赛道，修正字段）与 ``sw_industry``（申万基线）
    并存；下游引用 track 时必须注明「公司赛道标签（数据截至 X 报告期）」。
    """

    schema_version: str = "1"
    symbol: str = ""
    sw_industry: str = ""  # B3：申万基线行业名（并存展示，由调用方注入）
    sw_code: str = ""
    as_of_date: str = ""  # 最新报告期（YYYYMMDD）
    generated_at: str = ""
    stale_after: str | None = None  # 报告期 + 90 天（年报期口径）
    source_refs: list[str] = []

    segments: list[SegmentSnapshot] = Field(default_factory=list)
    dominant_segment: str | None = None  # 占比（或收入）最大业务线
    fastest_segment: str | None = None  # 增速最快业务线（占比 ≥ 阈值）
    track_label: str = ""  # 真实赛道标签（规则或 LLM）
    track_method: str = "rule"  # rule / llm
    override_basis: str = ""  # 标签依据（占比/增速数据 + LLM 复核记录）

    business_model: str = ""  # brand / odm / component / integrator / other（Phase E）
    business_model_evidence: str = ""
    peer_group: list[str] = Field(default_factory=list)  # 对标组（Phase C）
    peer_group_source: str = ""
    peer_benchmarks: dict[str, float | None] = Field(default_factory=dict)  # peer_*（Phase D）

    review_status: str | None = None  # approved / needs_review / rejected
    review_notes: list[str] = Field(default_factory=list)
    degraded: bool = False
    degraded_reason: str = ""
    stale: bool = False
