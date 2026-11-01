"""与业务无关的通用纯函数算子（对应设计文档 §4.1–§4.5）。

所有算子都是无状态纯函数：不读取配置、不依赖全局状态、不出现任何业务状态名 / 轴名 /
阈值。宽度 ``w``、饱和点 ``scale`` / ``ref``、死区与饱和点 ``lo`` / ``hi``、每个状态的
可达上限 ``caps``、状态数 ``K`` 全部由调用方注入；非法参数一律 raise ``ValueError``，
不做静默兜底（配置解析层的 fail-open 是另一层口径，不在这里实现）。

缺失值口径（与设计文档 §4.10 一致）：

=============  ==================  ==================================================
算子           缺失输入            语义
=============  ==================  ==================================================
``up`` / ``dn`` ``None`` → ``0``    没有强度；给 0.5 会凭空伪造强度证据
``neutral_band`` ``None`` → ``0.5``  信息不足，给半分信用
``not_yet``    ``None`` → ``0.5``    信息不足；不能因缺失被当成「单侧已发生」
``severe``     ``None`` → ``0``      不制造证伪证据
``mean_missing`` 空 / 全缺失 → ``None`` 缺失不补 0、不稀释（如何降级由调用方决定）
=============  ==================  ==================================================

值域约定：``clip01`` / ``up`` / ``dn`` / ``neutral_band`` / ``severe`` / ``saturate`` /
``log1p_saturate`` / ``normalize_evidence`` 落在各自声明的区间内。``not_yet`` 是刻意的
例外：``x > w`` 时返回负值作为反证据，由调用方在归一化入口统一 ``clip01`` 收口；不要在
算子内部再 clip 一次，否则「反证据」与「零证据」将不可区分。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

__all__ = [
    "clip01",
    "dn",
    "entropy",
    "entropy_normalized",
    "log1p_saturate",
    "mean_missing",
    "neutral_band",
    "normalize_evidence",
    "not_yet",
    "saturate",
    "severe",
    "softmax",
    "up",
]


def _require_positive(value: float, name: str) -> None:
    """参数护栏：``value <= 0`` 时 raise ``ValueError``，不做静默回退。"""
    if value <= 0.0:
        raise ValueError(f"{name} 必须 > 0，实际 {value!r}")


def clip01(x: float) -> float:
    """把数值夹到 ``[0, 1]``。

    缺失由调用方在入口处理（本函数不接受 ``None``）。值域 ``[0, 1]``。
    """
    return max(0.0, min(1.0, x))


def up(x: float | None) -> float:
    """方向分「正部」：``clip01(x)``。

    ``None → 0``：缺失表示没有强度，不制造虚假证据（设计文档 §4.2）。值域 ``[0, 1]``。
    """
    return 0.0 if x is None else clip01(x)


def dn(x: float | None) -> float:
    """方向分「负部」：``clip01(-x)``。

    ``None → 0``：同 :func:`up`，缺失不伪造强度。值域 ``[0, 1]``。
    """
    return 0.0 if x is None else clip01(-x)


def neutral_band(x: float | None, w: float) -> float:
    """双侧中性带：``clip01(1 - |x| / w)``。

    - ``x = 0 → 1``；``|x| ≥ w → 0``；中间线性衰减；
    - ``None → 0.5``：信息不足，给半分信用（设计文档 §4.10）；
    - ``w <= 0`` raise ``ValueError``（宽度由调用方注入，不是模块常量）。

    值域 ``[0, 1]``。语义是「这个轴接近中性 / 尚未成形」，两侧都应衰减；若只需要单侧
    「尚未发生」，用 :func:`not_yet`。
    """
    _require_positive(w, "w")
    if x is None:
        return 0.5
    return clip01(1.0 - abs(x) / w)


def not_yet(x: float | None, w: float) -> float:
    """单侧「尚未达到」：``1 - max(0, x) / w``。

    - ``x ≤ 0 → 1``；``x = w → 0``；``x > w`` 时为负值 = 反证据（刻意保留）；
    - ``None → 0.5``：信息不足，给半分信用；
    - ``w <= 0`` raise ``ValueError``。

    值域 ``(-∞, 1]``。负值由归一化入口（如 :func:`normalize_evidence`）统一 ``clip01``
    收口，因此本算子内部不 clip。
    """
    _require_positive(w, "w")
    if x is None:
        return 0.5
    return 1.0 - max(0.0, x) / w


def severe(x: float | None, lo: float, hi: float) -> float:
    """仅负侧的严重度（死区 + 线性 + 饱和）。

    - ``x >= 0 → 0``；``|x| ≤ lo → 0``（死区，轻微负值不算证伪）；
    - ``|x| ≥ hi → 1``（饱和）；
    - ``lo < |x| < hi`` 时线性插值 ``(|x| - lo) / (hi - lo)``；
    - ``None → 0``：不制造证伪证据（设计文档 §4.10）；
    - ``lo < 0`` 或 ``hi <= lo`` raise ``ValueError``。

    值域 ``[0, 1]``。``lo`` / ``hi`` 由调用方注入，本模块不含业务默认值。
    """
    if lo < 0.0 or hi <= lo:
        raise ValueError(f"severe 需要 0 <= lo < hi，实际 lo={lo!r}, hi={hi!r}")
    if x is None or x >= 0.0:
        return 0.0
    magnitude = -x
    if magnitude <= lo:
        return 0.0
    if magnitude >= hi:
        return 1.0
    return (magnitude - lo) / (hi - lo)


def saturate(v: float, scale: float) -> float:
    """线性饱和：``clip(v / scale, -1, 1)``。

    ``scale`` 是「一单位饱和点」（原始量纲），必须 ``> 0``；``scale <= 0`` raise
    ``ValueError``。值域 ``[-1, 1]``。适合「超过某阈值就没有额外信息」的量；需要保留大值
    幅度时用 :func:`log1p_saturate`。缺失由调用方过滤（本函数不接受 ``None``）。
    """
    _require_positive(scale, "scale")
    return max(-1.0, min(1.0, v / scale))


def log1p_saturate(v: float, ref: float) -> float:
    """``log1p`` 反饱和：``sign(v) · min(1, log1p(|v|) / log1p(ref))``。

    ``ref`` 是原始量纲的正参考点（不是 log 空间值）：``|v| = ref → ±1``，小值近似线性、
    长尾被压缩。例：``ref=100`` 时 ``45 → ≈0.83``、``5 → ≈0.39``。

    ``ref <= 0`` raise ``ValueError``；值域 ``[-1, 1]``；``v = 0 → 0``。缺失由调用方过滤。
    """
    _require_positive(ref, "ref")
    sign = 1.0 if v >= 0.0 else -1.0
    return sign * min(1.0, math.log1p(abs(v)) / math.log1p(ref))


def mean_missing(values: Iterable[float | None]) -> float | None:
    """缺失感知均值：只对非 ``None`` 项求平均；空输入 / 全缺失 → ``None``。

    缺失不补 0：补 0 等于把「不知道」当成「中性」，会系统性稀释强度（设计文档 §4.10）。
    值域随调用方（典型为 ``[-1, 1]``），本算子不做 clip。
    """
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present) / len(present)


def normalize_evidence(evidence: Mapping[str, float], caps: Mapping[str, float]) -> dict[str, float]:
    """逐状态归一化：``clip01(evidence[state] / caps[state])``。

    ``caps`` 是调用方为每个状态注入的「可达上限」（结构常数，不是超参）。``evidence`` 中
    出现但 ``caps`` 缺失的状态 raise ``ValueError``（拒绝用隐式默认 cap 静默降权）；``caps``
    中任一值 ``<= 0`` raise ``ValueError``；多余的 ``caps`` 条目忽略。

    返回值域 ``[0, 1]``；负证据（如 :func:`not_yet` 的反证据）在此统一收口为 0。
    """
    for state, cap in caps.items():
        _require_positive(cap, f"caps[{state!r}]")
    normalized: dict[str, float] = {}
    for state, value in evidence.items():
        if state not in caps:
            raise ValueError(f"caps 缺少状态 {state!r} 的可达上限")
        normalized[state] = clip01(value / caps[state])
    return normalized


def softmax(logits: Mapping[str, float]) -> dict[str, float]:
    """数值稳定的 softmax：``exp(v - max(v)) / Σ exp(v - max(v))``。

    减去最大值不改变结果、只避免指数溢出；返回分布和为 1（浮点容差内）、每项非负。
    ``len(logits) < 2`` raise ``ValueError``（单状态没有可比较的分布）。

    均匀先验等价于给所有 logit 加同一常数，因此这里不额外加先验；非均匀先验由调用方显式
    注入 logits。注意分布是「模式权重」而非概率（设计文档 §3.5）。
    """
    if len(logits) < 2:
        raise ValueError(f"softmax 至少需要 2 个状态，实际 {len(logits)} 个")
    peak = max(logits.values())
    weights = {state: math.exp(value - peak) for state, value in logits.items()}
    total = sum(weights.values())
    return {state: weight / total for state, weight in weights.items()}


def entropy(distribution: Mapping[str, float]) -> float:
    """香农熵 ``H = -Σ p·ln p``（``p <= 0`` 的项按 0 处理，避免 ``ln 0``）。

    值域 ``[0, ln K]``；确定性分布（单个 ``p = 1``）→ ``0``。本函数不校验分布是否已归一化。
    """
    return -sum(p * math.log(p) for p in distribution.values() if p > 0.0)


def entropy_normalized(distribution: Mapping[str, float]) -> float:
    """归一化熵 ``H_norm = H / ln K``，把不同状态数的帧压到 ``[0, 1]``。

    ``K = len(distribution)``；``K < 2`` raise ``ValueError``（``ln K = 0`` 无定义）。结果
    额外 ``clip01`` 以吸收浮点误差，保证严格落在 ``[0, 1]``。它只是分布离散度，不是
    「不确定性概率」（设计文档 §3.5）。
    """
    k = len(distribution)
    if k < 2:
        raise ValueError(f"entropy_normalized 至少需要 2 个状态，实际 {k} 个")
    return clip01(entropy(distribution) / math.log(k))
