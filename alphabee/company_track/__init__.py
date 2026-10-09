"""公司赛道（COMPANY_TRACK_ROADMAP）——穿透申万标签的业务线数据层。

Phase A（✅）：业务线数据基础——主营构成取数（东财优先 / Tushare 兜底）、
canonical 归一化（占比/毛利率/成本/利润 + 跨期分项增速推导）。
Phase B（✅）：真实赛道标签——规则推导（占比×增速加权）+ 可选 LLM 复核 +
override 机制（与申万基线并存）+ 跨年报期业务主线漂移检测。

后续阶段（C-F）：对标组构建与基准、商业模式定位、消费端打通——
见 docs/roadmap/COMPANY_TRACK_ROADMAP.md。

公共 API：
- ``fetch_business_segments`` —— 业务线分项取数（EM 优先，fina_mainbz 兜底）
- ``derive_track_label`` / ``synthesize_track_label`` / ``detect_track_drift``
  —— 赛道标签推导（B1/B2/B4）
- ``build_company_track`` —— 组装 CompanyTrackArtifact（B3/B4）
- ``SegmentSnapshot`` / ``SegmentCollection`` / ``CompanyTrackArtifact`` —— 契约
"""

from alphabee.company_track.business_model import (
    BUSINESS_MODEL_FOCUS,
    BUSINESS_MODEL_LABELS,
    BUSINESS_MODELS,
    classify_business_model,
)
from alphabee.company_track.contracts import (
    CompanyTrackArtifact,
    SegmentCollection,
    SegmentSnapshot,
)
from alphabee.company_track.data import fetch_business_segments
from alphabee.company_track.label import (
    TrackLabelResult,
    derive_track_label,
    detect_track_drift,
    select_label_base,
    synthesize_track_label,
)
from alphabee.company_track.normalize import (
    assess_period_consistency,
    derive_segment_yoy,
    latest_report_period,
    normalize_segments,
    segments_for_period,
)
from alphabee.company_track.peer import derive_peer_benchmarks, peer_benchmark_fields
from alphabee.company_track.peer_extract import (
    extract_peer_candidates,
    infer_peer_candidates,
    select_peer_candidates,
)
from alphabee.company_track.peer_group_build import (
    PeerConfidence,
    build_peer_group,
    gate_candidates,
    peer_confidence_for_group,
    peer_confidence_from_group,
    synthesize_peer_confidence,
)
from alphabee.company_track.peer_group_store import PeerGroup, PeerGroupStore
from alphabee.company_track.peer_judge import (
    DEFAULT_MIN_OVERLAP as PEER_MIN_OVERLAP,
)
from alphabee.company_track.peer_judge import (
    DIMS as PEER_JUDGE_DIMS,
)
from alphabee.company_track.peer_judge import (
    MIN_PEERS_DEFAULT,
    build_judge_prompt,
    infer_peer_scoring,
    judge_peer_candidates,
    normalize_dims,
    overlap_score,
)
from alphabee.company_track.peer_report import fetch_local_report_fragments
from alphabee.company_track.peer_taxonomy import (
    assess_reliability,
    recall_pool,
    same_levels,
    stock_taxonomy,
    taxonomy_entry,
)
from alphabee.company_track.peer_universe import build_peer_universe
from alphabee.company_track.peer_validate import (
    normalize_peer_code,
    split_domestic_international,
    validate_a_share_codes,
)
from alphabee.company_track.track import build_company_track

__all__ = [
    "CompanyTrackArtifact",
    "PeerConfidence",
    "assess_reliability",
    "peer_confidence_for_group",
    "peer_confidence_from_group",
    "recall_pool",
    "same_levels",
    "stock_taxonomy",
    "synthesize_peer_confidence",
    "taxonomy_entry",
    "MIN_PEERS_DEFAULT",
    "PEER_JUDGE_DIMS",
    "PEER_MIN_OVERLAP",
    "PeerGroup",
    "PeerGroupStore",
    "SegmentCollection",
    "SegmentSnapshot",
    "TrackLabelResult",
    "BUSINESS_MODEL_FOCUS",
    "BUSINESS_MODEL_LABELS",
    "BUSINESS_MODELS",
    "assess_period_consistency",
    "build_company_track",
    "build_judge_prompt",
    "build_peer_group",
    "build_peer_universe",
    "classify_business_model",
    "derive_peer_benchmarks",
    "derive_segment_yoy",
    "derive_track_label",
    "extract_peer_candidates",
    "gate_candidates",
    "normalize_dims",
    "overlap_score",
    "fetch_local_report_fragments",
    "infer_peer_candidates",
    "infer_peer_scoring",
    "judge_peer_candidates",
    "select_peer_candidates",
    "detect_track_drift",
    "fetch_business_segments",
    "latest_report_period",
    "normalize_segments",
    "normalize_peer_code",
    "peer_benchmark_fields",
    "split_domestic_international",
    "validate_a_share_codes",
    "segments_for_period",
    "select_label_base",
    "synthesize_track_label",
]
