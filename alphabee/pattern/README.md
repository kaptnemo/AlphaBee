# alphabee.pattern

与业务无关的「数值 → 状态」建模模式实现：把一组方向分（每轴 `[-1, 1]`）确定性地映射为一个
**软状态分布**、一个 argmax 锚点、分布离散度与（可选）相对上一帧的迁移 / drift。

包内**不含任何业务语义**：状态名、轴名、宽度、阈值、`cap`、温度、仓位标签全部由调用方注入；
也不 import 任何业务模块。同样的契约被不同业务复用（例如市场状态、行情阶段、研究框架），
这样公式、归一化、校验与不变量测试只需要维护一份。

## 设计原则

- **纯函数 / 确定**：同一 `PatternSpec` + 同一输入 + 同一 `previous` 必得同一结果，无 IO、
  无全局状态、不读配置、不调 LLM。
- **单一来源**：上游算好的中间量（`raw_evidence` / `normalized_evidence` / `logits` /
  `distribution`）全部透出，下游只读不重算。
- **显式缺失**：缺失一律用 `None`，禁止静默补 `0`；`0` 等于把「不知道」伪装成「中性」。
- **不静默兜底**：非法参数、缺失的 `cap`、未知状态、证据程序抛错一律显式失败，不降级成默认值。

## 模块结构

| 模块 | 职责 |
|---|---|
| `spec.py` | 规格契约：`PatternSpec` / `StateSpec` / `AxisSpec` / `TransitionGraph` / `Params` / `EvidenceProgram` |
| `ops.py` | 无状态纯函数算子：`up` / `dn` / `neutral_band` / `not_yet` / `severe` / `saturate` / `log1p_saturate` / `mean_missing` / `normalize_evidence` / `softmax` / `entropy` / `entropy_normalized` / `clip01` |
| `evidence.py` | 按 `spec.states` 顺序调用每状态证据程序；`build_context` 构造 `{"params": ...}` 上下文 |
| `engine.py` | `Engine`：证据 → 归一化 → softmax → 软状态 → drift / 迁移 |
| `validation.py` | `validate_spec` 一次性收集结构问题；`grid_points` / `find_witness` / `reachable_states` 网格工具 |
| `testing.py` | 不变量测试工具：`assert_bounds` / `assert_reachable` / `assert_no_uniform` / `assert_monotone` / `iter_grid_results` |
| `decision.py` | 决策映射：`weighted_expectation` / `band_from_weight` / `layered_scale` / `apply_ev_gate` |
| `types.py` | 结果契约：`Belief` / `Transition` / `PatternResult` / `Scores` |

## 核心概念

- **轴（factor）**：一个方向分 `scores[axis] ∈ [-1, 1]` 或 `None`（显式缺失）。
- **状态（state）**：一个证据程序 `program(scores, ctx) -> 原始证据`，外加可达上限 `cap`。
  `cap` 是该公式在输入域上的**结构常数**（不是超参），用于把原始证据归一化到 `[0, 1]` 使状态间可比。
- **归一化**：`normalized = clip01(raw / cap)`，逐状态、互不干扰。
- **分布**：`softmax(normalized / temperature)`。它是「归一化模式证据的 softmax 权重」，
  **不是频率概率、不是贝叶斯后验**，只能用于 argmax 锚点、期望计算与不确定性判定。
- **`P_top`**：分布最大值 = 状态确信度；低于 `params.uncertain_p_top` 时标记 `uncertain`。
- **离散度**：`entropy = -Σ p·ln p`，`entropy_normalized = H / ln K ∈ [0, 1]`。
- **迁移**：给出 `previous` 时产出 `Transition(from_state, to_state, legal, kind, trigger_factors)`；
  是否合法由 `TransitionGraph` 决定，同状态恒合法（`kind="same"`），未知边 `kind="illegal"`。
- **平局规则**：`argmax` 取 `spec.state_names` 声明顺序中的第一个，保证确定性。

## 快速上手

```python
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


def program_up(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return up(scores.get("x"))


def program_down(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return dn(scores.get("x"))


def program_neutral(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return 0.6 * neutral_band(scores.get("x"), 1.0) * neutral_band(scores.get("y"), 1.0)


spec = PatternSpec(
    name="toy",
    factors=("x", "y"),
    states=(
        StateSpec(name="UP", cap=1.0, program=program_up),
        StateSpec(name="DOWN", cap=1.0, program=program_down),
        StateSpec(name="NEUTRAL", cap=0.6, program=program_neutral),
    ),
    transitions=TransitionGraph.chain(("UP", "NEUTRAL", "DOWN")).with_backward(),
    params=Params(temperature=0.5, uncertain_p_top=0.7),
    trigger_threshold=0.3,
)

engine = Engine(spec)
result = engine.classify({"x": 0.8, "y": None})

print(result.belief.argmax_state)          # UP
print(result.belief.distribution)          # {"UP": ..., "DOWN": ..., "NEUTRAL": ...}
print(result.p_top, result.uncertain)      # 确信度 + 是否不确定
print(result.normalized_evidence)          # 每状态 [0, 1] 归一化证据

# 带上一帧时额外得到迁移与 drift
next_result = engine.classify({"x": 0.0, "y": 0.0}, previous=result)
print(next_result.transition)              # Transition(from_state="UP", to_state="NEUTRAL", legal=True, kind="forward", ...)
```

`spec` 构造后建议先校验（`Engine` 构造时也会自动校验，不通过直接 `raise ValueError`）：

```python
from alphabee.pattern import validate_spec

report = validate_spec(spec)
if not report.ok:
    for issue in report.issues:
        print(issue.code, issue.message)
```

## 缺失值口径

算子对 `None` 的处理是刻意的，避免「没数据」被当成「中性」或「已发生」：

| 算子 | `None` 输入 | 语义 |
|---|---|---|
| `up` / `dn` | `0` | 没有强度；给 0.5 会凭空伪造强度证据 |
| `neutral_band` | `0.5` | 信息不足，给半分信用（双侧） |
| `not_yet` | `0.5` | 信息不足；不能因缺失被当成「单侧已发生」 |
| `severe` | `0` | 不制造证伪证据 |
| `mean_missing` | 空 / 全缺失 → `None` | 缺失不补 0、不稀释 |

`not_yet` 在 `x > w` 时返回负值作为**反证据**（刻意不 clip），由归一化入口 `clip01` 统一收口，
这样「反证据」与「零证据」仍可区分。

## 校验与不变量测试

结构校验（`validation.validate_spec`）覆盖：状态为空 / 少于 2 / 重名、`cap` 非有限或非正、
证据程序不可调用、轴为空 / 重名、迁移端点未知、温度非法、`uncertain_p_top` 越界、
`trigger_threshold` 为负等，全部一次性收集为 `ValidationReport`。

不变量工具（`testing.py`，断言失败 raise `AssertionError`，不依赖 pytest）在笛卡尔网格上验证：

- `assert_bounds`：原始证据 `>= 0`、归一化证据 `∈ [0, 1]`、分布和 `≈ 1`、熵值域；
- `assert_reachable`：每个状态都能成为 argmax（可加 `P_top` 下限）；
- `assert_no_uniform`：不出现全状态等概率的假均分；
- `assert_monotone`：轴恶化时多头证据不增、空头证据不降（侧别由调用方声明）。

```python
from alphabee.pattern import assert_bounds, assert_reachable

axes = {"x": [-1.0, 0.0, 1.0], "y": [-1.0, 0.0, 1.0]}
assert_bounds(spec, axes)
assert_reachable(spec, axes)
```

## 决策映射

`decision.py` 只做数学映射，标签与阈值由调用方注入：

- `weighted_expectation(distribution, weights)`：`Σ p·w`，分布 → 动作力度的唯一通道；
- `band_from_weight(weight, thresholds, fallback=...)`：由折减后仓位反推标签（阈值须严格降序）；
- `layered_scale(*factors)`：乘性链 `Π factors`（类型 × 力度 × 门槛）；
- `apply_ev_gate(...)`：赔率门槛，返回 `(weight, reason)`，`reason ∈ {no_gate, pass, soft_reduce, hard_zero}`。

## 测试

```bash
poetry run pytest tests/pattern -q
```
