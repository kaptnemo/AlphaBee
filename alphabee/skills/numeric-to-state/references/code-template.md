# 代码模板与完整示例

生成代码时严格按此骨架：规格暴露为模块级 `SPEC`，证据函数只依赖 `scores` / `ctx`，
归一化 / softmax 交给 `Engine`，最后用 `validate_spec` 与不变量工具验收。

## 骨架

```python
"""<模式名>：把 <轴> 映射为 <状态> 的软状态分布。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from alphabee.pattern import (
    Engine,
    Params,
    PatternSpec,
    StateSpec,
    TransitionGraph,
    dn,
    neutral_band,
    up,
)

# ── 证据程序：每状态一个纯函数 (scores, ctx) -> 原始证据 ──────────────────────


def _state_a(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return up(scores.get("momentum"))


def _state_b(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return dn(scores.get("momentum"))


def _state_c(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return 0.8 * neutral_band(scores.get("momentum"), 0.3) * neutral_band(scores.get("volatility"), 0.3)


# ── 规格：所有业务名字 / 参数在这里集中定义 ──────────────────────────────────

SPEC = PatternSpec(
    name="<模式名>",
    factors=("momentum", "volatility"),
    states=(
        StateSpec(name="OFFENSE", cap=1.0, program=_state_a),
        StateSpec(name="DEFENSE", cap=1.0, program=_state_b),
        StateSpec(name="NEUTRAL", cap=0.8, program=_state_c),
    ),
    transitions=TransitionGraph.chain(("OFFENSE", "NEUTRAL", "DEFENSE")).with_backward(),
    params=Params(temperature=0.5, uncertain_p_top=0.6),
    trigger_threshold=0.3,
)


def classify(
    scores: Mapping[str, float | None],
    *,
    previous=None,
):
    """按输入方向分计算状态分布；previous 可选（用于迁移 / drift）。"""
    return Engine(SPEC).classify(scores, previous=previous)


if __name__ == "__main__":
    import json
    import sys

    payload = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    result = classify(payload)
    print(json.dumps(
        {
            "argmax_state": result.belief.argmax_state,
            "distribution": dict(result.belief.distribution),
            "p_top": result.p_top,
            "entropy_normalized": result.entropy_normalized,
            "uncertain": result.uncertain,
            "normalized_evidence": dict(result.normalized_evidence),
        },
        ensure_ascii=False,
        indent=2,
    ))
```

要点：

- `cap` 与公式严格对应（见 `ops-selection.md`）；改公式必须重算 `cap`。
- `factors` 的键名必须与用户会传入的 `scores` 键一致。
- 状态声明顺序决定 argmax 平局与 drift 键顺序，按用户确认的顺序写。
- 不要在证据函数里 `clip01` / `softmax` / 归一化；`Engine` 会统一处理。
- 不想每帧重复构造 `Engine` 时可缓存（`ENGINE = Engine(SPEC)`），但同一进程内 `SPEC` 不变。

## 完整示例（自然语言 → 代码）

用户描述：

> 动量高、波动低是进攻；动量低、波动高是防御；其他是中性。
> 明显信号时希望比较果断；低于 0.6 的置信度算不确定；防御可以经中性回到进攻。

生成（已确认 `momentum` / `volatility` 为 `[-1,1]` 方向分，中性带宽 0.3）：

```python
"""动量+波动三状态模型。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from alphabee.pattern import (
    Engine,
    Params,
    PatternSpec,
    StateSpec,
    TransitionGraph,
    dn,
    neutral_band,
    up,
)


def _offense(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return up(scores.get("momentum")) * dn(scores.get("volatility"))


def _defense(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return dn(scores.get("momentum")) * up(scores.get("volatility"))


def _neutral(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return 0.8 * neutral_band(scores.get("momentum"), 0.3) * neutral_band(scores.get("volatility"), 0.3)


SPEC = PatternSpec(
    name="momentum_volatility_regime",
    factors=("momentum", "volatility"),
    states=(
        StateSpec(name="OFFENSE", cap=1.0, program=_offense),
        StateSpec(name="DEFENSE", cap=1.0, program=_defense),
        StateSpec(name="NEUTRAL", cap=0.8, program=_neutral),
    ),
    transitions=TransitionGraph.chain(("OFFENSE", "NEUTRAL", "DEFENSE")).with_backward(),
    params=Params(temperature=0.2, uncertain_p_top=0.6),
    trigger_threshold=0.3,
)
```

验收脚本：

```python
from alphabee.pattern import assert_bounds, assert_reachable, validate_spec

assert validate_spec(SPEC).ok

axes = {
    "momentum": [-1.0, -0.3, 0.0, 0.3, 1.0],
    "volatility": [-1.0, -0.3, 0.0, 0.3, 1.0],
}
assert_bounds(SPEC, axes)
assert_reachable(SPEC, axes)
```

计算分布：

```python
engine = Engine(SPEC)
result = engine.classify({"momentum": 0.8, "volatility": -0.4})
print(result.belief.argmax_state)        # OFFENSE
print(dict(result.belief.distribution))  # {'OFFENSE': ..., 'DEFENSE': ..., 'NEUTRAL': ...}
print(result.p_top, result.uncertain)
```

## 输出格式（交付给用户时）

````
规格摘要
- 轴：momentum（越大越支持进攻）、volatility（越大越危险）
- 状态：OFFENSE / NEUTRAL / DEFENSE（声明顺序即平局优先级）
- 公式：
  OFFENSE = up(momentum) · dn(volatility)，cap=1.0
  DEFENSE = dn(momentum) · up(volatility)，cap=1.0
  NEUTRAL = 0.8 · neutral_band(momentum,0.3) · neutral_band(volatility,0.3)，cap=0.8
- 参数：temperature=0.2、uncertain_p_top=0.6、trigger_threshold=0.3
- 迁移：OFFENSE ↔ NEUTRAL ↔ DEFENSE（双向）
- 假设：<列出所有未经用户确认、由你建议的值>

校验
- validate_spec: ok
- assert_bounds: 通过
- assert_reachable: 通过

输入 {"momentum": 0.8, "volatility": -0.4} 的结果
- 状态：OFFENSE
- 分布：OFFENSE 0.71 / NEUTRAL 0.14 / DEFENSE 0.14（softmax 权重，非概率）
- P_top：0.71（不确定阈值 0.6 → 不标记 uncertain）
- 主要证据：OFFENSE 的归一化证据最高（0.32 = up(0.8)·dn(-0.4)），动量为正、波动为负
````

## 常见错误

- **cap 与公式不匹配**：例如乘积公式仍写 `cap=1.0`，会让该状态权重偏高。
- **键名不一致**：`factors=("momentum",)` 但用户传 `"momentum_21d"` → 全部缺失。
- **把缺失当 0**：`scores.get("x", 0.0)` 会污染分布；应传 `None` 并用算子口径处理。
- **在证据层做归一化**：重复归一化会与 `Engine` 口径冲突，校验工具也会对不上。
- **把 `distribution` 当概率**：对外表述必须是「模式权重 / 相对强弱」。
