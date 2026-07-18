import ast
import operator as op
from functools import singledispatchmethod
from pathlib import Path
from typing import Any

RULES_DIR = Path(__file__).resolve().parent / "rules"

_ALLOWED_BIN_OPS = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.FloorDiv: op.floordiv,
    ast.Mod: op.mod,
    ast.Pow: op.pow,
}

_ALLOWED_UNARY_OPS = {
    ast.UAdd: op.pos,
    ast.USub: op.neg,
}

_ALLOWED_COMPARE_OPS = {
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
            op_type = type(node.op)
            if op_type not in _ALLOWED_UNARY_OPS:
                raise ValueError(f"Unary operator not allowed: {op_type.__name__}")
            return _ALLOWED_UNARY_OPS[op_type](eval_node(node.operand))

        if isinstance(node, ast.Compare):
            left = eval_node(node.left)

            for operator_node, comparator in zip(node.ops, node.comparators):
                op_type = type(operator_node)
                if op_type not in _ALLOWED_COMPARE_OPS:
                    raise ValueError(f"Comparison not allowed: {op_type.__name__}")

                right = eval_node(comparator)
                if not _ALLOWED_COMPARE_OPS[op_type](left, right):
                    return False

                left = right

            return True

        if isinstance(node, ast.BoolOp):
            values = [eval_node(v) for v in node.values]

            if isinstance(node.op, ast.And):
                return all(values)

            if isinstance(node.op, ast.Or):
                return any(values)

            raise ValueError("Boolean operator not allowed")

        raise ValueError(f"Expression not allowed: {type(node).__name__}")

    return eval_node(tree)


class DerivedFactRule:
    name: str
    required_facts: list[str]
    description: str = ""
    formula: str = ""
    thresholds: dict[str, str] = {}
    interpretation: dict[str, str] = {}

    @singledispatchmethod
    def __init__(self, fact_name: str):
        raise NotImplementedError("Unsupported type for fact_name")

    @__init__.register(str)
    def _from_fact_name(self, fact_name: str):
        self.name = fact_name
        self.fact_definition_file = RULES_DIR / f"{fact_name}.yaml"

        self.load_definition()

    @__init__.register(Path)
    def _from_definition_file(self, definition_file: Path):
        self.name = definition_file.stem
        self.fact_definition_file = definition_file

        self.load_definition()

    def load_definition(self):
        import yaml

        with open(self.fact_definition_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
            self.description = data.get("description", "")
            self.formula = data.get("formula", "")
            self.thresholds = data.get("thresholds", {})
            self.interpretation = data.get("interpretation", {})
            self.required_facts = data.get("required_facts", [])
            self.required_derived_facts = data.get("required_derived_facts", [])

    def compute(
        self,
        fact_values: dict[str, float],
        interpretation: bool = False,
    ) -> dict[str, Any]:
        try:
            derived_value = safe_eval_formula(self.formula, fact_values)
        except ZeroDivisionError:
            return {
                self.name: None,
                "level": "invalid",
                "error": "division_by_zero",
            }
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

        for level, expression in self.thresholds.items():
            try:
                matched = safe_eval_formula(expression, threshold_context)
            except Exception:
                continue

            if matched:
                result["level"] = level
                break

        if interpretation:
            result["interpretation"] = self.interpretation.get(
                result["level"],
                self.interpretation.get("unknown", "未知"),
            )

        return result


RULES = {}


def load_rules():
    for rule_file in RULES_DIR.glob("*.yaml"):
        rule = DerivedFactRule(rule_file)
        RULES[rule.name] = rule


if __name__ == "__main__":
    load_rules()
    print(RULES)
    asset_turnover = RULES["asset_turnover"]
    fact_values = {"revenue": 100000, "total_assets": 78888}
    print(asset_turnover.compute(fact_values))
