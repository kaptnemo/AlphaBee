"""策略参数的类型化载体（P0：只定义，不接线）。

三组 frozen dataclass 是 ``params/registry.yaml`` 与生产代码之间的**唯一类型面**：

- :class:`ScoreParams`：F/E 方向分与状态证据的 11 个策略常量（当前来源
  ``alphabee/midterm/score_engine.py`` 模块常量）；
- :class:`EvidenceParams`：V2 证据层 softmax / 严重度 / 断链参数（当前来源
  ``alphabee/midterm/classifier_v2.py`` 常量区与 ``EvidenceParams``）；
- :class:`ReportQualityParams`：报告门禁 G4/G3 阈值（设计 §5.1，本轮不接入任何门禁）。

**默认值纪律**（P0 不迁移生产常量，避免行为变化）：

1. 本模块默认值是上述模块常量的**硬编码副本**；``tests/params/test_registry.py``
   逐项钉住"dataclass 默认值 == 模块常量 == registry 值"三方一致，任何漂移立即报错；
2. 本模块只依赖 stdlib（``dataclasses``），**不 import** midterm / report / config
   代码：保证 ``from alphabee.params.models import ScoreParams`` 不触发配置读取与
   业务模块 import 环（注册表缺失时也能拿到内置默认）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ScoreParams:
    """F/E 方向分与状态证据的策略参数（设计 §4.6.2）。

    字段与 ``score_engine.py`` 模块常量逐项对应：

    ====================================  ==========================================
    字段                                  对应模块常量
    ====================================  ==========================================
    ``fundamental_yoy_scale``              ``_FUNDAMENTAL_YOY_SCALE`` (30.0)
    ``ocf_negative_penalty``               ``_OCF_NEGATIVE_PENALTY`` (0.5)
    ``beat_bonus``                         ``_BEAT_BONUS`` (0.2)
    ``miss_penalty``                       ``_MISS_PENALTY`` (0.3)
    ``miss_gap_threshold``                 ``_MISS_GAP_THRESHOLD`` (20.0)
    ``segment_divergence_threshold``       ``_SEGMENT_DIVERGENCE_THRESHOLD`` (15.0)
    ``segment_divergence_bonus``           ``_SEGMENT_DIVERGENCE_BONUS`` (0.2)
    ``f_marginal_weight``                  ``F_MARGINAL_WEIGHT`` (0.30)
    ``anchor_weight``                      ``_ANCHOR_WEIGHT`` (0.6)
    ``research_weight``                    ``_RESEARCH_WEIGHT`` (0.4)
    ``min_consensus_coverage``             ``_MIN_CONSENSUS_COVERAGE`` (3，两处同值)
    ====================================  ==========================================
    """

    fundamental_yoy_scale: float = 30.0
    ocf_negative_penalty: float = 0.5
    beat_bonus: float = 0.2
    miss_penalty: float = 0.3
    miss_gap_threshold: float = 20.0
    segment_divergence_threshold: float = 15.0
    segment_divergence_bonus: float = 0.2
    f_marginal_weight: float = 0.30
    anchor_weight: float = 0.6
    research_weight: float = 0.4
    min_consensus_coverage: int = 3


@dataclass(frozen=True)
class EvidenceParams:
    """V2 证据层参数（设计 §4.6.2；默认值与 ``classifier_v2.DEFAULT_PARAMS`` 逐项一致）。

    P0 只在 params 包内保留类型化副本，**不迁移** ``classifier_v2.EvidenceParams``
    （迁移与 re-export 属 P1 接线，见设计 §4.6.1）。
    """

    s0_baseline: float = 0.30
    s0_floor: float = 0.10
    w_e: float = 0.60
    w_t: float = 0.80
    w_f: float = 1.00
    severe_lo: float = 0.25
    severe_hi: float = 0.60
    break_lambda: float = 2.0
    temperature: float = 0.22
    uncertain_p_top: float = 0.55


@dataclass(frozen=True)
class ReportQualityParams:
    """报告质量门禁参数（设计 §5.1；Q2 才接线，本轮只定义类型与默认值）。

    P0 阶段不进入 ``params_hash``（设计 §4.7：报告阈值只影响报告门禁，不混入帧指纹）。
    """

    main_max_chars: int = 10000
    exec_summary_max_chars: int = 800
    claims_min_count: int = 3
    claims_max_count: int = 5
    max_chars_per_claim: int = 500
    min_grounds: int = 1
    numeric_rel_tolerance: float = 0.005
    numeric_max_unmatched_ratio: float = 0.0


DEFAULT_SCORE_PARAMS = ScoreParams()
DEFAULT_EVIDENCE_PARAMS = EvidenceParams()
DEFAULT_REPORT_QUALITY_PARAMS = ReportQualityParams()
