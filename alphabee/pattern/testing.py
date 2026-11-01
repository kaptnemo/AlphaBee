"""通用不变量测试工具（与业务无关，不依赖 pytest）。

所有断言失败都 raise ``AssertionError``，消息包含触发帧与状态，便于定位。工具只消费
``PatternSpec`` / ``Engine`` 产生的确定性中间量，不读取真实语料，也不推断多空侧别——
「哪些状态属于多头侧 / 空头侧」是调用方的业务知识，通过参数显式声明。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Literal

from alphabee.pattern.engine import Engine
from alphabee.pattern.spec import PatternSpec
from alphabee.pattern.types import PatternResult
from alphabee.pattern.validation import find_witness, grid_points

__all__ = [
    "assert_bounds",
    "assert_monotone",
    "assert_no_uniform",
    "assert_reachable",
    "iter_grid_results",
]


def iter_grid_results(
    spec: PatternSpec,
    axes: Mapping[str, Sequence[float | None]],
) -> Iterator[tuple[dict[str, float | None], PatternResult]]:
    """按 :func:`alphabee.pattern.validation.grid_points` 的确定顺序逐帧产出分类结果。

    每个元素为 ``(frame, PatternResult)``；``Engine`` 只构造一次，保证同一 spec 的
    参数 / cap / 迁移图在所有帧上完全一致。
    """
    engine = Engine(spec)
    for frame in grid_points(axes):
        yield frame, engine.classify(frame)


def assert_bounds(spec: PatternSpec, axes: Mapping[str, Sequence[float | None]]) -> None:
    """断言每帧的结构边界都成立：

    - 原始证据 ``raw_evidence >= 0``（不制造负证据；负值应在公式内先由调用方解释）；
    - 归一化证据落在 ``[0, 1]``；
    - 分布和 ``≈ 1``（绝对容差 ``1e-9``）；
    - ``entropy >= 0``、``entropy_normalized ∈ [0, 1]``（不允许 ``None``）。

    失败时 ``AssertionError`` 消息包含帧、状态与违规值。
    """
    for frame, result in iter_grid_results(spec, axes):
        for state, value in result.raw_evidence.items():
            if value < 0.0:
                raise AssertionError(f"raw evidence 必须 >= 0：state={state!r}, value={value!r}, frame={frame!r}")
        for state, value in result.normalized_evidence.items():
            if not 0.0 <= value <= 1.0:
                raise AssertionError(
                    f"normalized evidence 必须落在 [0, 1]：state={state!r}, value={value!r}, frame={frame!r}"
                )

        distribution_total = sum(result.belief.distribution.values())
        if abs(distribution_total - 1.0) > 1e-9:
            raise AssertionError(f"distribution 和必须 ≈ 1（容差 1e-9）：sum={distribution_total!r}, frame={frame!r}")
        if result.belief.entropy < 0.0:
            raise AssertionError(f"entropy 必须 >= 0：entropy={result.belief.entropy!r}, frame={frame!r}")
        entropy_normalized = result.entropy_normalized
        if entropy_normalized is None:
            raise AssertionError(f"entropy_normalized 不能为 None：frame={frame!r}")
        if not 0.0 <= entropy_normalized <= 1.0:
            raise AssertionError(f"entropy_normalized 必须落在 [0, 1]：value={entropy_normalized!r}, frame={frame!r}")


def assert_reachable(
    spec: PatternSpec,
    axes: Mapping[str, Sequence[float | None]],
    *,
    min_p_top: float | None = None,
) -> None:
    """断言每个状态都能在网格上成为 `argmax`（可附带 ``P_top`` 下限）。

    用 :func:`alphabee.pattern.validation.find_witness` 逐状态搜索；不可达状态收集后
    一次性 raise，消息列出全部不可达状态。
    """
    unreachable = [
        state for state in spec.state_names if find_witness(spec, state, axes=axes, min_p_top=min_p_top) is None
    ]
    if unreachable:
        raise AssertionError(f"不可达状态：{unreachable}（min_p_top={min_p_top!r}）；axes={dict(axes)!r}")


def assert_no_uniform(
    spec: PatternSpec,
    axes: Mapping[str, Sequence[float | None]],
    *,
    tol: float = 1e-12,
) -> None:
    """断言任何帧都不出现「全状态等概率」的假均分。

    判据：``max(P) - min(P) <= tol``（默认 ``1e-12``，仅吸收浮点噪声）。失败消息给出帧与
    完整分布。底噪 / 基线是否足以避免假均分由调用方设计，本工具只负责检验。
    """
    for frame, result in iter_grid_results(spec, axes):
        probabilities = list(result.belief.distribution.values())
        if max(probabilities) - min(probabilities) <= tol:
            raise AssertionError(
                f"出现全状态等概率（假均分）：frame={frame!r}, "
                f"distribution={dict(result.belief.distribution)!r}, tol={tol!r}"
            )


def assert_monotone(
    spec: PatternSpec,
    *,
    axis: str,
    worsening: Literal["lower", "higher"],
    long_states: Sequence[str],
    short_states: Sequence[str],
    axes: Mapping[str, Sequence[float | None]],
    tol: float = 1e-12,
) -> None:
    """断言「轴恶化时，声明为多头的状态证据不增、空头状态证据不降」。

    侧别（``long_states`` / ``short_states``）是调用方的业务知识，本包不推断。``worsening``
    声明轴的恶化方向：``"lower"`` = 取值越低越糟，``"higher"`` = 取值越高越糟。

    过程：固定其余轴，遍历 ``axis`` 序列的相邻取值对，取「更差帧」与「更好帧」，比较
    **归一化证据**：

    - 多头侧：``worse[state] <= better[state] + tol``；
    - 空头侧：``worse[state] + tol >= better[state]``。

    含 ``None`` 的相邻对没有「更高/更低」的方向语义，直接跳过（显式缺失不参与有序比较）。
    失败消息包含恶化方向、轴名、状态、两个帧与两侧证据值。``worsening`` 非法值 raise
    ``ValueError``；``axis`` 不在 ``axes`` 中由 ``KeyError`` 显式失败。
    """
    if worsening not in ("lower", "higher"):
        raise ValueError(f"worsening 仅支持 'lower' / 'higher'，实际 {worsening!r}")

    engine = Engine(spec)
    axis_values = list(axes[axis])
    other_axes = {name: values for name, values in axes.items() if name != axis}

    for base in grid_points(other_axes):
        for first, second in zip(axis_values, axis_values[1:], strict=False):
            # None 表示显式缺失，没有「更高/更低」的方向语义，含 None 的相邻对跳过
            if first is None or second is None or first == second:
                continue
            if worsening == "lower":
                worse, better = (first, second) if first < second else (second, first)
            else:
                worse, better = (first, second) if first > second else (second, first)

            worse_frame = {**base, axis: worse}
            better_frame = {**base, axis: better}
            worse_evidence = engine.normalize(engine.evidence(worse_frame))
            better_evidence = engine.normalize(engine.evidence(better_frame))

            for state in long_states:
                if worse_evidence[state] > better_evidence[state] + tol:
                    raise AssertionError(
                        f"单调性失败（worsening={worsening!r}, axis={axis!r}）："
                        f"多头状态 {state!r} 在更差帧的证据反而更高；"
                        f"worse_frame={worse_frame!r}, worse={worse_evidence[state]!r}, "
                        f"better_frame={better_frame!r}, better={better_evidence[state]!r}, tol={tol!r}"
                    )
            for state in short_states:
                if worse_evidence[state] + tol < better_evidence[state]:
                    raise AssertionError(
                        f"单调性失败（worsening={worsening!r}, axis={axis!r}）："
                        f"空头状态 {state!r} 在更差帧的证据反而更低；"
                        f"worse_frame={worse_frame!r}, worse={worse_evidence[state]!r}, "
                        f"better_frame={better_frame!r}, better={better_evidence[state]!r}, tol={tol!r}"
                    )
