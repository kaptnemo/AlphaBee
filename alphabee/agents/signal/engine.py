"""Signal Engine — 信号规则批量评估，自动处理衍生事实依赖。

执行流程：
1. 收集所有请求信号规则的 required_derived_facts
2. 调用 DerivedFacts Engine 批量计算衍生事实（含链式依赖）
3. 将计算成功的衍生事实值合并到 extended_values
4. 对每条信号规则调用 SignalRule.evaluate(extended_values)
5. 若某规则的衍生依赖计算失败，标记该规则为 blocked
"""

from __future__ import annotations

import logging

from alphabee.agents.derived_facts.engine import Engine as DerivedFactsEngine
from alphabee.agents.signal.registry import (
    SIGNAL_RULES,
    SignalRule,
    load_signal_rules,
)

logger = logging.getLogger(__name__)


class SignalEngine:
    def __init__(self, signal_rules: dict[str, SignalRule] | None = None):
        if signal_rules is None:
            load_signal_rules()
            self.signal_rules = dict(SIGNAL_RULES)
        else:
            self.signal_rules = dict(signal_rules)

        # 缓存 DerivedFactsEngine 实例，避免每次 run() 重新加载 21 条 YAML 规则
        try:
            self._df_engine = DerivedFactsEngine()
        except Exception:
            self._df_engine = None
            logger.warning("derived_facts_engine_init_failed", exc_info=True)

    def run(
        self,
        rule_names: list[str],
        fact_values: dict[str, float],
    ) -> dict[str, dict]:
        """评估指定信号规则，自动计算所需衍生事实。

        Args:
            rule_names: 要评估的信号规则 ID 列表。
            fact_values: canonical 字段值字典（由上游 fact_collector 提供）。

        Returns:
            ``{rule_id: result_dict}``，result_dict 键：
            - level: "high" / "medium" / "low" / "none" / "blocked" /
                     "missing_fact" / "invalid"
            - interpretation: 文字解释（blocked/missing_fact/invalid 时为空）
            - critic_questions: 追问清单（列表）
            - thesis_impact: flat dict，{dimension: impact}（level 已解析）
            - error: 仅在异常 level 时出现
            - blocked_by: 仅在 "blocked" 时出现，列出失败的上游依赖名
        """
        # ── 1. 收集所有需要的衍生事实 ────────────────────────────
        all_required_derived: set[str] = set()
        for name in rule_names:
            rule = self.signal_rules.get(name)
            if rule:
                all_required_derived.update(rule.required_derived_facts)

        # ── 2. 批量计算衍生事实 ──────────────────────────────────
        extended_values = dict(fact_values)
        derived_failed: set[str] = set()  # 计算失败的衍生事实名

        if all_required_derived:
            try:
                df_engine = self._df_engine or DerivedFactsEngine()
                df_results = df_engine.run(list(all_required_derived), fact_values)
                for df_name, df_result in df_results.items():
                    value = df_result.get(df_name)
                    if value is not None and isinstance(value, (int, float, bool)):
                        extended_values[df_name] = value
                    else:
                        derived_failed.add(df_name)
            except Exception as e:
                # 引擎初始化或拓扑排序失败，所有衍生事实均视为失败
                logger.warning(
                    "derived_facts_computation_failed",
                    error=str(e),
                    required_facts=list(all_required_derived),
                )
                derived_failed.update(all_required_derived)

        # ── 3. 逐条评估信号规则 ──────────────────────────────────
        results: dict[str, dict] = {}
        for name in rule_names:
            rule = self.signal_rules.get(name)
            if rule is None:
                results[name] = {
                    "level": "unknown",
                    "error": f"未知信号规则：{name}",
                }
                continue

            # 若该规则的某个衍生依赖计算失败，标记 blocked
            blocked_by = [d for d in rule.required_derived_facts if d in derived_failed]
            if blocked_by:
                root_errors = "; ".join(blocked_by)
                results[name] = {
                    "level": "blocked",
                    "error": f"上游衍生事实计算失败：{root_errors}",
                    "blocked_by": blocked_by,
                }
                continue

            results[name] = rule.evaluate(extended_values)

        return results
