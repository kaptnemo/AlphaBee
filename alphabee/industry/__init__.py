"""行业知识工作流（industry-context Phase 1 + Phase 2）。

独立行业/产业知识工作流 → 产出可版本化、可复用、可审计的行业知识资产
（``IndustryContextArtifact`` JSON 快照）。在线个股分析阶段（Phase 3）将按
``classification_standard + industry_code`` 加载最新非过期快照注入 orchestrator。

公共 API：
- ``IndustryContextWorkflow`` —— 离线工作流（采集→归一化→基准→定性→审核→持久化）
- ``IndustryContextArtifact`` —— v2 行业上下文契约
- ``IndustryProfileStore`` —— JSON 快照存储（原子写、latest-wins）
- ``derive_benchmarks`` / ``normalize_industry_records`` / ``fetch_industry_peers``
  —— 纯函数层（Phase 0 遗产 + Phase 1 归一化）
- ``names`` / ``classification`` —— 行业名规范字典 + 申万分类匹配（Phase 2 字段治理）
- ``crosscheck`` —— 多来源行业交叉校验（Phase 2）
"""

from alphabee.industry.benchmarks import (
    BENCHMARK_CATEGORIES,
    INDUSTRY_BENCHMARK_FIELDS,
    IndustryBenchmarks,
    derive_benchmarks,
    flatten_benchmarks,
    group_benchmarks,
)
from alphabee.industry.classification import match_sw_industry
from alphabee.industry.contracts import (
    CLASSIFICATION_STANDARDS,
    IndustryContextArtifact,
    IndustryQualitative,
    IndustryReview,
    IndustryTarget,
    IndustryWorkflowState,
    PeriodAlignment,
    WorkflowOptions,
)
from alphabee.industry.crosscheck import (
    IndustryCrossCheck,
    SourceMatch,
    crosscheck_industry,
    fetch_industry_crosscheck,
)
from alphabee.industry.data import fetch_industry_peers, fetch_peer_financials
from alphabee.industry.names import (
    EXTRACTION_HINTS,
    catalog,
    group_defs,
    group_keys,
    industry_display_name,
    industry_in_group,
    industry_keys_for_name,
    keyword_extract_industry,
    normalize_name,
)
from alphabee.industry.normalize import (
    assess_period_alignment,
    normalize_industry_records,
)
from alphabee.industry.persistence import (
    STALE_AFTER_DAYS,
    IndustryProfileStore,
    ProfileInfo,
    is_stale,
    suggest_stale_after,
)
from alphabee.industry.workflow import IndustryContextWorkflow

__all__ = [
    "BENCHMARK_CATEGORIES",
    "CLASSIFICATION_STANDARDS",
    "EXTRACTION_HINTS",
    "INDUSTRY_BENCHMARK_FIELDS",
    "IndustryBenchmarks",
    "IndustryContextArtifact",
    "IndustryContextWorkflow",
    "IndustryCrossCheck",
    "IndustryProfileStore",
    "IndustryQualitative",
    "IndustryReview",
    "IndustryTarget",
    "IndustryWorkflowState",
    "PeriodAlignment",
    "ProfileInfo",
    "STALE_AFTER_DAYS",
    "SourceMatch",
    "WorkflowOptions",
    "assess_period_alignment",
    "catalog",
    "crosscheck_industry",
    "derive_benchmarks",
    "fetch_industry_crosscheck",
    "fetch_industry_peers",
    "fetch_peer_financials",
    "flatten_benchmarks",
    "group_benchmarks",
    "group_defs",
    "group_keys",
    "industry_display_name",
    "industry_in_group",
    "industry_keys_for_name",
    "is_stale",
    "keyword_extract_industry",
    "match_sw_industry",
    "normalize_industry_records",
    "normalize_name",
    "suggest_stale_after",
]
