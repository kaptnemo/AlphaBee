import ast
import operator as op
from collections.abc import Callable
from functools import singledispatchmethod
from pathlib import Path
from typing import Any, cast

RULES_DIR = Path(__file__).resolve().parent / "rules"

_ALLOWED_BIN_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.FloorDiv: op.floordiv,
    ast.Mod: op.mod,
    ast.Pow: op.pow,
}

_ALLOWED_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: op.pos,
    ast.USub: op.neg,
}

_ALLOWED_COMPARE_OPS: dict[type[ast.cmpop], Callable[[Any, Any], Any]] = {
    ast.Gt: op.gt,
    ast.GtE: op.ge,
    ast.Lt: op.lt,
    ast.LtE: op.le,
    ast.Eq: op.eq,
    ast.NotEq: op.ne,
}


def safe_eval_formula(formula: str, fact_values: dict[str, float]) -> float | bool:
    tree = ast.parse(formula, mode="eval")

    def eval_node(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return eval_node(node.body)

        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float, bool)):
                return node.value
            raise ValueError("Only numeric and boolean constants are allowed")

        if isinstance(node, ast.Name):
            if node.id not in fact_values:
                raise ValueError(f"Unknown variable: {node.id}")
            return fact_values[node.id]

        if isinstance(node, ast.BinOp):
            op_type = type(node.op)
            if op_type not in _ALLOWED_BIN_OPS:
                raise ValueError(f"Operator not allowed: {op_type.__name__}")
            return _ALLOWED_BIN_OPS[op_type](
                eval_node(node.left),
                eval_node(node.right),
            )

        if isinstance(node, ast.UnaryOp):
            unary_op_type = type(node.op)
            if unary_op_type not in _ALLOWED_UNARY_OPS:
                raise ValueError(f"Unary operator not allowed: {unary_op_type.__name__}")
            return _ALLOWED_UNARY_OPS[unary_op_type](eval_node(node.operand))

        if isinstance(node, ast.Compare):
            left = eval_node(node.left)

            for operator_node, comparator in zip(node.ops, node.comparators):
                compare_op_type = type(operator_node)
                if compare_op_type not in _ALLOWED_COMPARE_OPS:
                    raise ValueError(f"Comparison not allowed: {compare_op_type.__name__}")

                right = eval_node(comparator)
                if not _ALLOWED_COMPARE_OPS[compare_op_type](left, right):
                    return False

                left = right

            return True

        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                for value in node.values:
                    if not eval_node(value):
                        return False
                return True

            if isinstance(node.op, ast.Or):
                for value in node.values:
                    if eval_node(value):
                        return True
                return False

            raise ValueError("Boolean operator not allowed")

        raise ValueError(f"Expression not allowed: {type(node).__name__}")

    return cast(float | bool, eval_node(tree))


class DerivedFactRule:
    name: str
    required_facts: list[str]
    description: str = ""
    formula: str = ""
    thresholds: dict[str, str | list[str]] = {}
    interpretation: dict[str, str] = {}
    zero_division_policy: str = "invalid"
    zero_division_error: str = "division_by_zero"
    not_applicable_if: list[str] = []
    not_applicable_error: str = "not_applicable_condition"

    @singledispatchmethod  # type: ignore[misc]
    def __init__(self, fact_name: str) -> None:
        raise NotImplementedError("Unsupported type for fact_name")

    @__init__.register(str)
    def _from_fact_name(self, fact_name: str) -> None:
        self.name = fact_name
        self.fact_definition_file = RULES_DIR / f"{fact_name}.yaml"

        self.load_definition()

    @__init__.register(Path)
    def _from_definition_file(self, definition_file: Path) -> None:
        self.name = definition_file.stem
        self.fact_definition_file = definition_file

        self.load_definition()

    def load_definition(self) -> None:
        import yaml

        with open(self.fact_definition_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
            self.description = data.get("description", "")
            self.formula = data.get("formula", "")
            self.thresholds = data.get("thresholds", {})
            self.interpretation = data.get("interpretation", {})
            self.required_facts = data.get("required_facts", [])
            self.required_derived_facts = data.get("required_derived_facts", [])
            self.zero_division_policy = data.get("zero_division_policy", "invalid")
            self.zero_division_error = data.get("zero_division_error", "division_by_zero")
            declared = data.get("not_applicable_if", [])
            self.not_applicable_if = [declared] if isinstance(declared, str) else list(declared)
            self.not_applicable_error = data.get("not_applicable_error", "not_applicable_condition")

    def _not_applicable_result(self, reason: str, interpretation: bool) -> dict[str, Any]:
        """构建 not_applicable 结果（值以 None 落地，供下游识别"该指标不适用"）。"""
        result: dict[str, Any] = {
            self.name: None,
            "level": "not_applicable",
            "error": reason,
        }
        if interpretation:
            result["interpretation"] = self.interpretation.get(
                "not_applicable",
                self.interpretation.get("invalid", "未知"),
            )
        return result

    def _check_not_applicable(
        self,
        fact_values: dict[str, float],
        interpretation: bool,
    ) -> dict[str, Any] | None:
        """计算前的"不适用"判定，返回 None 表示可以正常计算。

        两类来源：

        1. 依赖字段存在但值为 ``None``（上游已显式判定不适用/缺失）—— 此时把 None
           丢进公式只会得到 invalid，应显式降级为 not_applicable，让下游知道
           "指标不适用"而不是"算出错了"；
        2. YAML 声明了 ``not_applicable_if`` 条件表达式且命中 —— 表达经济含义上的
           不适用（例如 PEG 在净利润非正增长时无意义）。表达式求值失败（引用的
           字段缺失）时跳过该项，与阈值回退链同策略。
        """
        required = [*self.required_facts, *self.required_derived_facts]
        unavailable = [name for name in required if name in fact_values and fact_values[name] is None]
        if unavailable:
            return self._not_applicable_result(
                f"input_not_applicable: {', '.join(unavailable)}",
                interpretation,
            )

        for expression in self.not_applicable_if:
            try:
                matched = safe_eval_formula(expression, fact_values)
            except Exception:
                continue
            if matched:
                return self._not_applicable_result(self.not_applicable_error, interpretation)

        return None

    def compute(
        self,
        fact_values: dict[str, float],
        interpretation: bool = False,
    ) -> dict[str, Any]:
        not_applicable = self._check_not_applicable(fact_values, interpretation)
        if not_applicable is not None:
            return not_applicable

        try:
            derived_value = safe_eval_formula(self.formula, fact_values)
        except ZeroDivisionError:
            result: dict[str, Any] = {
                self.name: None,
                "level": self.zero_division_policy,
                "error": self.zero_division_error,
            }
            if interpretation:
                result["interpretation"] = self.interpretation.get(
                    self.zero_division_policy,
                    self.interpretation.get("invalid", "未知"),
                )
            return result
        except KeyError as e:
            return {
                self.name: None,
                "level": "missing_fact",
                "error": f"missing fact: {e}",
            }
        except Exception as e:
            return {
                self.name: None,
                "level": "invalid",
                "error": str(e),
            }

        result = {
            self.name: derived_value,
            "level": "unknown",
        }

        threshold_context = {
            **fact_values,
            "value": derived_value,
        }

        # 阈值支持"回退链"（industry-context Phase 0，见 docs/industry/industry-context-injection-plan.md 3.0）：
        # 每个 level 的表达式可以是字符串或字符串列表。列表按顺序求值，
        # 第一个求值成功且为 True 即命中；表达式抛异常（如引用的行业字段缺失）
        # 则顺延到下一项——实现"行业基准缺失 → 回退绝对阈值"的天然降级链。
        for level, expressions in self.thresholds.items():
            if isinstance(expressions, str):
                expressions = [expressions]
            for expression in expressions:
                try:
                    matched = safe_eval_formula(expression, threshold_context)
                except Exception:
                    continue
                if matched:
                    result["level"] = level
                    break
            if result["level"] != "unknown":
                break

        if interpretation:
            result["interpretation"] = self.interpretation.get(
                result["level"],
                self.interpretation.get("unknown", "未知"),
            )

        return result


RULES: dict[str, DerivedFactRule] = {}


def load_rules() -> None:
    for rule_file in RULES_DIR.glob("*.yaml"):
        rule = DerivedFactRule(rule_file)
        RULES[rule.name] = rule


if __name__ == "__main__":
    load_rules()
    print(RULES)
    asset_turnover = RULES["asset_turnover"]
    fact_values: dict[str, float] = {"revenue": 100000, "total_assets": 78888}
    print(asset_turnover.compute(fact_values))
