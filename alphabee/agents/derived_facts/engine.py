"""Engine — 带链式依赖的衍生事实计算引擎。

规则之间的依赖通过 YAML 中的 required_derived_facts 字段声明（区别于
required_facts，后者只含调用方提供的 canonical 字段）。Engine 按拓扑
顺序依次计算，并将每条规则的结果注入工作集，供下游规则使用。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alphabee.agents.derived_facts.registry import RULES, DerivedFactRule, load_rules

if TYPE_CHECKING:
    from alphabee.agents.facts.models import FinancialFacts, MarketFacts


class CyclicDependencyError(Exception):
    """规则之间存在循环依赖。"""


class Engine:
    def __init__(self, rules: dict[str, DerivedFactRule] | None = None):
        if rules is None:
            load_rules()
            self.rules = dict(RULES)  # snapshot，避免 load_rules 重入时修改全局状态
        else:
            self.rules = dict(rules)

    # ── 拓扑排序 ──────────────────────────────────────────────────

    def _resolve_order(self, rule_names: list[str]) -> list[str]:
        """对 rule_names（含传递依赖）做拓扑排序，返回计算顺序。

        只追踪 required_derived_facts 依赖；required_facts（canonical 字段）
        由调用方通过 fact_values 提供，不参与排序。

        Raises:
            CyclicDependencyError: 存在循环依赖。
            ValueError: required_derived_facts 中引用了未知规则名。
        """
        visited: set[str] = set()
        order: list[str] = []
        in_stack: set[str] = set()

        def dfs(name: str) -> None:
            if name in in_stack:
                raise CyclicDependencyError(f"检测到循环依赖，规则链中包含：'{name}'")
            if name in visited:
                return

            in_stack.add(name)
            rule = self.rules.get(name)
            if rule is not None:
                for dep in rule.required_derived_facts:
                    if dep not in self.rules:
                        raise ValueError(f"规则 '{name}' 声明了未知的衍生事实依赖：'{dep}'")
                    dfs(dep)
            in_stack.discard(name)
            visited.add(name)
            order.append(name)

        for name in rule_names:
            dfs(name)

        return order

    # ── 计算 ──────────────────────────────────────────────────────

    def run(
        self,
        rule_names: list[str],
        fact_values: dict[str, float] | None = None,
        *,
        financial_facts: FinancialFacts | None = None,
        market_facts: MarketFacts | None = None,
        extra_fields: dict[str, float] | None = None,
    ) -> dict[str, dict]:
        """按依赖顺序计算 rule_names（含传递依赖），返回每条规则的结果。

        支持两种输入方式，可混用：

        1. **平面值**（dict）：直接传入 ``fact_values``。
        2. **Pydantic 模型**：传入 ``financial_facts`` / ``market_facts``，
           引擎自动调用 ``.to_fact_values()`` 展开为平面 dict 并合并。

        优先级：``fact_values`` 先加载，模型值覆盖同名字段，``extra_fields``
        最后叠加（最高优先级）。

        Args:
            rule_names:       需要计算的规则名列表。
            fact_values:      canonical 字段值字典（平面值路径）。
            financial_facts:  ``FinancialFacts`` 模型实例，自动调用
                              ``.to_fact_values()`` 提取财务字段。
            market_facts:     ``MarketFacts`` 模型实例，自动调用
                              ``.to_fact_values()`` 提取行情字段。
            extra_fields:     手动补充的字段，优先级最高。

        Returns:
            ``{rule_name: result_dict}``，顺序与计算顺序一致。
            result_dict 键：rule_name（计算值）、level、interpretation、
            error（可选）、blocked_by（可选，上游依赖失败时出现）。
        """
        # ── 合并 fact_values：dict → 模型展开 → extra_fields ─────
        merged: dict[str, float] = dict(fact_values) if fact_values else {}

        if financial_facts is not None:
            merged.update(financial_facts.to_fact_values())
        if market_facts is not None:
            merged.update(market_facts.to_fact_values())
        if extra_fields is not None:
            merged.update(extra_fields)

        order = self._resolve_order(rule_names)

        all_values = merged.copy()
        results: dict[str, dict] = {}
        failed: dict[str, str] = {}  # name → error 描述

        for name in order:
            rule = self.rules.get(name)
            if rule is None:
                continue

            # 检查上游衍生依赖是否有失败
            blocked_by = [dep for dep in rule.required_derived_facts if dep in failed]
            if blocked_by:
                root_errors = "; ".join(f"{dep}: {failed[dep]}" for dep in blocked_by)
                err = f"上游衍生事实计算失败 — {root_errors}"
                results[name] = {
                    name: None,
                    "level": "blocked",
                    "error": err,
                    "blocked_by": blocked_by,
                }
                failed[name] = err
                continue

            result = rule.compute(all_values, interpretation=True)
            results[name] = result

            computed_value = result.get(name)
            if computed_value is not None:
                all_values[name] = computed_value
            elif result.get("level") in ("invalid", "missing_fact"):
                failed[name] = result.get("error", "计算失败")

        return results
