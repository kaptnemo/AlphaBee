"""降级传导阻尼：消费"降级产物"的结论必须保守化，但**只下调一档封顶**（F2 / §7.2 规则 2、§16）。

**核心动作**：把 insight 的既有先例（``low`` → thesis confidence ×0.85）泛化为全链路规则：

* :func:`degraded_inputs` —— 扫描 typed artifact 的 ``degraded`` 标记，给出降级输入的稳定标识；
* :func:`damp_confidence` —— 消费了降级输入的结论，confidence **下调一档**（离散）或 **×0.85**（连续）；
* :func:`apply_degradation` —— 降级产物的**唯一统一写入点**（§14.3-A），
  统一写 ``degraded`` + ``degradation_reason``，消除"各节点手写字段名"的漂移面；
  其 ``degraded=True`` 的起始档位由调用方通过 ``threshold`` 显式给出（默认 §7.2 的 Tier 2，
  见 :data:`DEGRADATION_TIER_THRESHOLD`）。

**为什么必须封顶**：§16「保守化螺旋」——若每个降级输入都叠加一次下调，全链路 confidence 会一路
塌到无信息量。故：**离散档位用映射表（high→medium→low→low）天然封顶；连续值固定单次 ×0.85，
与降级输入个数无关**（有测试钉住"只下调一档，多次降级输入不叠加"）。

工程约束（§14.0）：无 IO、无 import 期配置读取、确定性、纯函数（不改入参对象）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

from alphabee.core import Artifact

logger = logging.getLogger(__name__)

__all__ = [
    "CONFIDENCE_DOWNGRADE",
    "DAMPING_FACTOR",
    "DEGRADATION_FLAG",
    "DEGRADATION_REASON_FIELD",
    "DEGRADATION_TIER_THRESHOLD",
    "apply_degradation",
    "degraded_inputs",
    "damp_confidence",
]

#: 离散 confidence 的下调映射：**一档封顶**（``low`` 继续消费降级输入仍是 ``low``）。
CONFIDENCE_DOWNGRADE: dict[str, str] = {"high": "medium", "medium": "low", "low": "low"}

#: 连续 confidence 的阻尼因子（沿用 insight → thesis 的 ×0.85 先例，§7.2 规则 2）。
DAMPING_FACTOR = 0.85

#: ``degraded`` 标记所在的字段名（部分 payload 用 ``fallback_tier > 0`` 表达同级语义）。
_DEGRADED_FLAG = "degraded"
_FALLBACK_TIER = "fallback_tier"

#: Tier 2/3 降级产物的两个**字段名契约**（§14.3-A：由 :func:`apply_degradation` 统一写入，
#: 其它模块不得再手写这两个字面量，防止字段名漂移）。
DEGRADATION_FLAG = "degraded"
DEGRADATION_REASON_FIELD = "degradation_reason"

#: ``degraded=True`` 的**默认起始档位**（§7 阶梯：Tier 2 降级产出 / Tier 3 骨架跳过）。
#: §7.2 规则 1 的「Tier≥2 必须标 degraded」是**下界要求**：调用方可用
#: :func:`apply_degradation` 的 ``threshold`` 参数把更低档也标为降级
#: （``nodes/insights.py`` 的分档是 Tier 1，迁移时传 ``threshold=1`` 以保持行为等价）。
DEGRADATION_TIER_THRESHOLD = 2

#: 非映射值（如 LLM 结构化输出的 pydantic 模型）降级时的包装键，避免 metadata 写不进去。
_WRAPPED_VALUE_KEY = "value"


def _artifact_id(artifact: Any) -> str:
    return str(getattr(artifact, "id", "") or getattr(artifact, "type", "") or "")


def _is_degraded(artifact: Any) -> bool:
    """artifact 是否被标记为降级产出（``degraded=True`` 或 ``fallback_tier > 0``）。"""
    value = getattr(artifact, "value", None)
    payload = value if isinstance(value, dict) else {}
    if payload.get(_DEGRADED_FLAG) is True:
        return True
    tier = payload.get(_FALLBACK_TIER)
    return isinstance(tier, int) and tier > 0


def degraded_inputs(artifacts: Sequence[Artifact] | Iterable[Any] | None) -> list[str]:
    """返回**降级产物的稳定标识列表**（artifact id 优先，缺 id 时退回 type）。

    只读：不修改入参，也不回写任何 artifact。
    """
    found: list[str] = []
    for artifact in artifacts or []:
        if _is_degraded(artifact):
            identifier = _artifact_id(artifact)
            if identifier and identifier not in found:
                found.append(identifier)
    return found


def apply_degradation(
    artifact_value: Any,
    tier: int,
    reason: str,
    *,
    threshold: int = DEGRADATION_TIER_THRESHOLD,
) -> dict[str, Any]:
    """**降级产物的唯一写入点**（§14.3-A）：统一写 ``degraded`` + ``degradation_reason``。

    :param artifact_value: 待降级标记的产物 payload（通常是 ``Artifact.value`` 的映射）。
    :param tier: 本次采用的恢复档位（§7 的 0–5）。
    :param reason: 降级原因（人类可读；落地为 ``degradation_reason``）。
    :param threshold: ``degraded=True`` 的**起始档位**（下界，含）；默认
        :data:`DEGRADATION_TIER_THRESHOLD` = 2，即 §7.2 规则 1 / §14.3-A 的原文口径。

    **``threshold`` 的口径出处与为什么要显式暴露它**（t54）：

    * §7.2 规则 1 的原文是"**Tier≥2 的产物必须带 ``degraded=true``**"——这是一条**下界要求**，
      它规定"至少从 Tier 2 起必须标降级"，**并不禁止**调用方把更低档也标为 degraded；
    * 但不同生产者对"降级"的分档口径本就不同：``nodes/insights.py`` 现行的 fallback 分档是
      ``degraded = tier >= 1``（Tier 1 = 宽松救援解析也算降级产物）。若迁到本统一写入点时
      沿用默认门限（2），Tier 1 产物的 ``degraded`` 会由 ``True`` 变 ``False``，
      进而改变 ``degraded_inputs()`` / ``damp_confidence`` 的触发 —— 那是**行为回归**；
    * 因此本函数把门限**显式化**：调用方按自有分档传入（insights 传 ``threshold=1``），
      既能复用"字段名与写法唯一"这一收益，又**不必构造伪 tier**，迁移得以行为等价。

    **行为契约**（三条，均有单测钉住）：

    1. **不含入参变异**：always 返回**新 dict**；入参是 mapping 时先复制，绝不原地写；
    2. **两个字段名同时存在**：``degraded`` 与 ``degradation_reason`` 一律同时写入
       （这正是不允许各节点手写字段名的原因——单独写一个就会漂移）；
    3. ``tier`` 语义（默认门限下）：``tier >= 2``（Tier 2 降级产出 / Tier 3 骨架）→ ``degraded=True``；
       ``tier < 2``（Tier 0 完整通过 / Tier 1 局部修复）→ ``degraded=False``。
       传入 ``threshold=1`` 时，Tier 1 亦为 ``True``（insights 的既有分档，见上）。

    非 mapping 的入参（标量 / 字符串 / pydantic 模型等）**不抛异常**——
    它们没有可写的 metadata 面，故只返回降级元数据：
    ``{"degraded": ..., "degradation_reason": ..., "value": artifact_value}``
    （原值保留在 ``value`` 键下，不丢失调用方数据）。
    """
    degraded = bool(int(tier) >= int(threshold))
    payload: dict[str, Any]
    if isinstance(artifact_value, dict):
        payload = dict(artifact_value)  # 浅复制：不原地写调用方的 mapping
    else:
        payload = {_WRAPPED_VALUE_KEY: artifact_value}
    payload[DEGRADATION_FLAG] = degraded
    payload[DEGRADATION_REASON_FIELD] = reason or ""
    return payload


def damp_confidence(
    value: str | float | None,
    degraded_inputs: Sequence[str] | Iterable[str] | None,
) -> str | float | None:
    """消费降级输入的结论 → confidence 下调**一档**（离散）或 **×0.85**（连续）。

    * 无降级输入 → **原样返回**（不产生任何改动）；
    * 离散（``str``）：按 :data:`CONFIDENCE_DOWNGRADE` 下调一档；未知取值原样返回（不猜测语义）；
    * 连续（``float``/``int``）：``value * DAMPING_FACTOR``，夹在 ``[0, 1]``；
    * ``None`` → ``None``。

    **封顶保证**：下调量只取决于"是否存在降级输入"，与**个数无关** ⇒ 多次降级输入不叠加。
    """
    if value is None:
        return None
    has_degraded = any(True for _ in (degraded_inputs or []))
    if not has_degraded:
        return value

    if isinstance(value, bool):  # bool 是 int 子类，显式排除以免误当连续值
        return value
    if isinstance(value, str):
        return CONFIDENCE_DOWNGRADE.get(value.strip().lower(), value)
    if isinstance(value, int | float):
        damped = float(value) * DAMPING_FACTOR
        return round(max(0.0, min(1.0, damped)), 4)
    return value
