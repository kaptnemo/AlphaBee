"""配置感知分类器调度（TEF design §4.4）。

只做两件事：resolve 顺序字符串（fail-open）+ 路由到对应模块。

纪律（与 ``tracking.triggers.thresholds_from_settings`` 同构）：

- 配置在**调用时**读取（不 import 期读），缺失/异常/非法值 → warn + ``"etf"``，绝不抛；
- 模块属性延迟 import（monkeypatch 可证伪"真的路由到目标模块"）。
"""

from __future__ import annotations

import logging

from alphabee.midterm.models import ClassifierResult, StateBelief, VariableScores

logger = logging.getLogger(__name__)

ORDER_ETF = "etf"
ORDER_TEF = "tef"
KNOWN_ORDERS = (ORDER_ETF, ORDER_TEF)


def resolve_classifier_order(order: str | None) -> str:
    """解析分类器顺序：显式合法值优先；``None`` → 读 ``settings.midterm.classifier_order``。

    失败口径（fail-open，绝不抛）：

    - 显式值非法 → warn + ``"etf"``；
    - 配置段缺失 / 读配置异常 / 配置值非法 → warn + ``"etf"``。
    """
    if order is not None:
        value = str(order).strip().lower()
        if value in KNOWN_ORDERS:
            return value
        logger.warning("classifier_order 非法值 %r，回落 etf", order)
        return ORDER_ETF

    try:
        from alphabee.config import get_settings

        configured = getattr(getattr(get_settings(), "midterm", None), "classifier_order", None)
    except Exception as exc:  # noqa: BLE001 - 配置不可用绝不打断分类
        logger.warning("midterm.classifier_order 配置不可用（%s），回落 etf", exc)
        return ORDER_ETF

    value = str(configured or "").strip().lower()
    if value in KNOWN_ORDERS:
        return value
    logger.warning("midterm.classifier_order 配置值非法 %r，回落 etf", configured)
    return ORDER_ETF


def classify_state(
    scores: VariableScores,
    *,
    previous: StateBelief | None = None,
    order: str | None = None,
) -> ClassifierResult:
    """resolve 后路由：``tef`` → ``classifier_tef.classify_state``；其余 → ``classifier.classify_state``。

    ``order`` 传非 ``None`` 时以显式值为准（同样 fail-open 到 ``etf``）。
    路由走模块属性（monkeypatch 目标模块的 ``classify_state`` 可改变行为）。
    """
    resolved = resolve_classifier_order(order)
    if resolved == ORDER_TEF:
        from alphabee.midterm import classifier_tef

        return classifier_tef.classify_state(scores, previous=previous)
    from alphabee.midterm import classifier

    return classifier.classify_state(scores, previous=previous)
