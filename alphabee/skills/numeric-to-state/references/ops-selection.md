# 算子选择与 cap 推导

所有算子来自 `alphabee.pattern`（`from alphabee.pattern import ...`）。它们是纯函数，宽度/饱和点/
死区都由调用方注入；`w <= 0`、`scale <= 0`、`ref <= 0`、`lo < 0`、`hi <= lo` 会 raise `ValueError`。

## 自然语言 → 算子

| 用户说法 | 算子 | 签名 | 值域 / 缺失 |
|---|---|---|---|
| 「越高越支持」「正向强度」 | `up` | `up(x)` | `[0,1]`；`None→0` |
| 「越低越支持」「负向强度」 | `dn` | `dn(x)` | `[0,1]`；`None→0` |
| 「接近 0 才算中性 / 信号尚未成形」 | `neutral_band` | `neutral_band(x, w)` | `[0,1]`；`None→0.5` |
| 「还没到阈值才算好 / 单侧尚未发生」 | `not_yet` | `not_yet(x, w)` | `(-∞,1]`；`None→0.5`；负值是反证据 |
| 「只有明显为负才算恶化」 | `severe` | `severe(x, lo, hi)` | `[0,1]`；`None→0` |
| 「超过某点就没有额外信息」 | `saturate` | `saturate(v, scale)` | `[-1,1]`；原始量纲 |
| 「长尾压缩、小值近似线性」 | `log1p_saturate` | `log1p_saturate(v, ref)` | `[-1,1]`；`v=ref→±1` |
| 多轴同等重要、允许缺失 | `mean_missing` | `mean_missing(values)` | 空/全缺→`None`；不补 0 |
| 乘性链（类型 × 力度 × 门槛） | `layered_scale` | `layered_scale(*factors)` | `Π factors`，不裁剪 |
| 夹到 `[0,1]` | `clip01` | `clip01(x)` | `[0,1]`；不接受 `None` |

补充：

- 只需要方向分的正/负部分时用 `up` / `dn`；不要自己写 `max(0, x)`。
- 归一化与 softmax **不要手写**——`Engine` 会做 `clip01(raw/cap)` 与 `softmax(norm/temperature)`。
- `not_yet` 的负值刻意保留为反证据；归一化入口会统一收口为 0，不要提前 clip。
- `severe` 的 `lo` 是死区、`hi` 是饱和点，都是「负方向幅度」的原始量纲。

## 组合模式

### 1. 多轴加权和（综合打分）

```python
def program(scores, ctx):
    m = scores.get("momentum")
    v = scores.get("volatility")
    return 0.6 * up(m) + 0.4 * dn(v)
```

`cap = 0.6 + 0.4 = 1.0`（权重已归一时）。

### 2. 多轴乘积（必须同时满足）

```python
def program(scores, ctx):
    return up(scores.get("momentum")) * dn(scores.get("volatility"))
```

`cap = 1.0 * 1.0 = 1.0`。

```python
def program(scores, ctx):
    return 0.8 * neutral_band(scores.get("momentum"), 0.3) * neutral_band(scores.get("volatility"), 0.3)
```

`cap = 0.8 * 1.0 * 1.0 = 0.8`。

### 3. 缺失感知的组合

```python
def program(scores, ctx):
    parts = [up(scores.get("momentum")), dn(scores.get("volatility"))]
    value = mean_missing(parts)          # 全缺 → None
    return 0.0 if value is None else value
```

缺一个轴时不会把整体按 0 稀释；全缺时显式给 0（或按用户约定的缺失策略）。

### 4. 原始量 → 方向分

```python
def program(scores, ctx):
    pe = scores.get("pe")                # 若这里放的是原始 PE，先转方向分
    if pe is None:
        return 0.0
    cheapness = -log1p_saturate(pe, 40.0)  # PE 越低越便宜；40 为参考点
    return up(cheapness)
```

> 注意：`Engine` 期望输入是 `[-1,1]` 方向分。若用户给的是原始量，建议在**进 `classify` 之前**
> 用一层 `to_scores(raw)` 统一转换，证据函数只吃方向分，职责更清晰。

## cap 推导规则

`cap` 必须等于该状态公式在输入域上的**最大值**（结构常数）：

1. 单因子 `up`/`dn`/`neutral_band`/`severe` → 上界 1.0。
2. 线性加权和 `Σ w_i · e_i` → `Σ w_i`（前提每个 `e_i ∈ [0,1]`）。
3. 乘积 `Π c_i · e_i` → `Π c_i`（每个 `e_i ∈ [0,1]`）。
4. 含 `not_yet` 的公式：上界通常取 1.0；负值在归一化时被收口为 0。
5. 含 `saturate`/`log1p_saturate` 的因子：上界 1.0（它们已夹在 `[-1,1]`）。
6. 无法确定上界时：先给保守上界（偏大），再用 `assert_bounds` + 网格检查实际最大值，
   不要用 `1.0` 掩盖公式溢出。

> 经验：`cap` 偏小会让该状态在 softmax 里被高估，偏大会被低估。改公式就要重算 `cap`。

## 参数建议（仅在用户授权时使用，并标注为假设）

- `temperature`：越小分布越尖锐。明显信号希望 `P_top≈0.8` 时通常落在 `0.2–0.5`；
  越接近 1 越平缓。必须先和用户确认「果断程度」。
- `uncertain_p_top`：常见 `0.5–0.7`；状态越多，允许的 `P_top` 通常越低。
- `trigger_threshold`：迁移归因用，常见 `0.3`；`|score|` 达到它才记为触发轴。

以上都**不是**可以直接套用的行业标准，只是提问时的候选值。
