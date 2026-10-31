"""配置感知分类器调度（TEF design §4.4；状态证据 V2 design §6.4）。

只做三件事：resolve 顺序字符串 + resolve 公式版本（都 fail-open）+ 路由到对应模块。

纪律（与 ``tracking.triggers.thresholds_from_settings`` 同构）：

- 配置在**调用时**读取（不 import 期读），缺失/异常/非法值 → warn + 缺省值，绝不抛；
- 模块属性延迟 import（monkeypatch 可证伪"真的路由到目标模块"）；
- 默认公式 ``v1`` ⇒ 旧 config 行为不变；``v2`` 是显式 opt-in（设计 §6.4 / §7.1）。
"""

from __future__ import annotations

import logging

from alphabee.midterm.models import ClassifierResult, StateBelief, VariableScores

logger = logging.getLogger(__name__)

ORDER_ETF = "etf"
ORDER_TEF = "tef"
KNOWN_ORDERS = (ORDER_ETF, ORDER_TEF)

# 状态证据公式版本（设计 §6.4）：v1 = classifier.py / classifier_tef.py（冻结）；
# v2 = classifier_v2.py（唯一演进分支）。缺省 v1 ⇒ 旧 config 行为不变。
FORMULA_V1 = "v1"
FORMULA_V2 = "v2"
KNOWN_FORMULAS = (FORMULA_V1, FORMULA_V2)


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


def resolve_formula_revision(formula: str | None) -> str:
    """解析状态证据公式版本：显式合法值优先；``None`` → 读 ``settings.midterm.formula_revision``。

    失败口径与 :func:`resolve_classifier_order` 完全同构（fail-open，绝不抛）：

    - 显式值非法 → warn + ``"v1"``；
    - 配置段缺失 / 读配置异常 / 配置值非法 → warn + ``"v1"``。

    缺省 ``v1`` ⇒ 旧 config 与历史帧行为不变（设计 §2 目标 G7 / §6.4）。
    """
    if formula is not None:
        value = str(formula).strip().lower()
        if value in KNOWN_FORMULAS:
            return value
        logger.warning("classifier_formula 非法值 %r，回落 v1", formula)
        return FORMULA_V1

    try:
        from alphabee.config import get_settings

        configured = getattr(getattr(get_settings(), "midterm", None), "formula_revision", None)
    except Exception as exc:  # noqa: BLE001 - 配置不可用绝不打断分类
        logger.warning("midterm.formula_revision 配置不可用（%s），回落 v1", exc)
        return FORMULA_V1

    value = str(configured or "").strip().lower()
    if value in KNOWN_FORMULAS:
        return value
    logger.warning("midterm.formula_revision 配置值非法 %r，回落 v1", configured)
    return FORMULA_V1


def classify_state(
    scores: VariableScores,
    *,
    previous: StateBelief | None = None,
    order: str | None = None,
    formula: str | None = None,
) -> ClassifierResult:
    """先 resolve ``order`` 与 ``formula``，再路由到对应实现模块。

    - ``formula="v2"`` → ``classifier_v2.classify_state(scores, order=resolved_order,
      previous=previous)``（v2 内部用同一 ``order`` 表达两种顺序的门控落点）；
    - v1 → 原两条模块属性路由不变：``tef`` → ``classifier_tef.classify_state``、
      其余 → ``classifier.classify_state``（数值逐值不变）。

    两条路径都走模块属性延迟 import（monkeypatch 目标模块的 ``classify_state`` 可改变行为，
    即"真路由"可证伪）。显式值优先于配置；非法/异常一律 fail-open 到 v1 与 etf。
    """
    resolved_order = resolve_classifier_order(order)
    resolved_formula = resolve_formula_revision(formula)
    if resolved_formula == FORMULA_V2:
        from alphabee.midterm import classifier_v2

        return classifier_v2.classify_state(scores, order=resolved_order, previous=previous)
    if resolved_order == ORDER_TEF:
        from alphabee.midterm import classifier_tef

        return classifier_tef.classify_state(scores, previous=previous)
    from alphabee.midterm import classifier

    return classifier.classify_state(scores, previous=previous)
