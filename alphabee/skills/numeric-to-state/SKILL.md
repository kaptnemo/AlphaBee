---
name: numeric-to-state
description: 当用户想用自然语言描述一套「数值 → 状态」规则（例如把几个指标映射为市场阶段、风险等级或行情状态），需要生成可运行的 alphabee.pattern 代码，在参数不全时追问补齐，并用给定输入值计算状态分布时使用本 Skill。负责需求澄清、PatternSpec/Engine 代码生成、规格校验与不变量测试、以及按用户输入输出分布与解读。
version: 1.0.0
---

# numeric-to-state

把**自然语言规则**落成**确定性的「数值 → 状态」模型**，再用用户给的输入值算出状态分布。

底层是 `alphabee.pattern`：包内不含任何业务语义，状态名、轴名、阈值、`cap`、温度全部由
调用方注入。本 Skill 的职责是帮 Agent 把这些参数从对话里问清楚、写对、验住。

---

## What this skill is for

典型请求：

- 「帮我做一个模型：当动量高、波动低时是进攻态；动量低、波动高时是防御态；其余是中性态。」
- 「把这套打分规则固化成代码，输入几个指标值，输出各状态的概率/权重。」
- 「我已经有这套规则，想改成四状态，并允许防御态直接回到进攻态。」
- 「给我算一下：动量 0.8、波动 -0.3、估值 0.1，现在更接近哪个状态？」

只要涉及「用若干指标的方向分判断处于哪个状态」，就用本 Skill。

## When NOT to use

- 只是做一次性的 if/else 判断、不要求分布/可测试性 → 直接写函数即可。
- 需要机器学习训练、概率校准、贝叶斯后验 → `alphabee.pattern` 的分布是**模式权重**，不是概率。
- 需要访问外部数据源取数 → 先用对应数据 Skill 取数，再把方向分交给本 Skill。

---

## 核心契约（必须先让用户知道）

1. **输入是每轴方向分**：`scores[axis] ∈ [-1, 1]` 或 `None`（显式缺失）。不做自动归一化。
2. **输出是 softmax 权重分布**：表示「归一化模式证据」的相对强弱，**不是频率概率、不是后验概率**。
3. **`P_top` 是确信度**，低于 `uncertain_p_top` 标记 `uncertain`；它仍不是概率。
4. **缺失用 `None`，禁止静默补 0**：补 0 等于把「不知道」当成「中性」。
5. **所有阈值来自用户**：没有「行业通用默认值」可以静默采用；未确认前必须追问。

---

## 工作流

### Step 0：判断任务类型

| 用户意图 | 走哪条路径 |
|---|---|
| 从零描述规则 | Step 1 → 2 → 3 → 4 → 5 |
| 修改已有规格（加状态/改公式/改参数） | 读现有代码 → Step 2（只问受影响项）→ 3 → 4 → 5 |
| 已有代码，只想算一次分布 | 跳到 Step 5，直接调用 `scripts/run_pattern.py` 或 `Engine.classify` |
| 只想要「哪个状态」不要分布 | 仍走完整分类，取 `argmax_state`（并说明 `uncertain` 情况） |

### Step 1：抽取规格六要素

| 要素 | 必须 | 从自然语言里找 | 缺失时 |
|---|---|---|---|
| `factors`（轴） | 是 | 「动量」「波动」「估值」等指标名，及其方向语义（`-1..1` 还是需要映射） | 必问：指标集合与各自方向 |
| 输入值域/单位 | 是 | 用户给的是原始量（如 PE=25）还是已归一化方向分 | 必问：是否需要先用 `saturate`/`log1p_saturate` 转成方向分 |
| `states`（状态） | 是 | 「进攻/中性/防御」等状态名与业务含义 | 必问：状态集合、声明顺序（决定平局）、是否有多空侧别 |
| 状态证据公式 | 是 | 「动量高、波动低才算进攻」等组合关系 | 可先给候选公式，再让用户确认 |
| `cap`（每状态可达上限） | 是 | 用户通常不会提 | 由公式推导（见 `references/ops-selection.md`），把推导结果给用户确认 |
| `Params` | 是 | 几乎不会提 | 必问 `temperature` 与 `uncertain_p_top`，或让用户明确授权「用你建议值」 |
| `TransitionGraph` | 否 | 「只能逐级降级」「允许直接跳回」 | 若用户提到状态流转再建图；否则留空图并在交付物中说明 |
| `trigger_threshold` | 否 | 只有需要迁移归因时才有意义 | 默认 `0.3` 仅作建议，需用户确认或显式声明为假设 |

状态证据函数签名固定：

```python
def program(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    ...
```

`ctx` 约定含 `"params"`；不要在证据函数里读配置、IO 或数据库。

### Step 2：缺口检查与追问

用 `references/elicitation-checklist.md` 的清单逐项核对。规则：

- **可推断**（轴名、状态名、方向、大致组合关系）→ 先给出你的理解，请用户确认。
- **必须问**（阈值/宽度/死区/饱和点、`temperature`、`uncertain_p_top`、`cap` 的取法、状态顺序、
  迁移策略、缺失轴的处理）→ 一次最多问 5 个，每个问题给 2–3 个候选并说明影响。
- 用户说「你决定 / 用合理默认」时才可用建议值，并**在交付物里列出全部假设**。
- 不要为了显得聪明而编造校准值；错误默认会直接改变分布。

询问模板：

> 我需要确认 3 个参数才能固化规则：
> 1. 中性带宽度 `w`：动量绝对值小于多少算「尚未成形」？（候选：0.2 / 0.3 / 0.5）
> 2. 温度 `temperature`：越小分布越尖锐。你希望「明显信号」时 P_top 大约 0.8 还是 0.6？
> 3. 不确定性阈值 `uncertain_p_top`：P_top 低于多少时标记「不确定」？（候选：0.5 / 0.6 / 0.7）
>
> 另外确认：某轴缺数据时，相关状态证据应按「0（无强度）」还是「0.5（信息不足给半信用）」处理？

### Step 3：生成代码

按 `references/code-template.md` 的骨架生成：

- 只使用 `alphabee.pattern` 的公开 API；证据公式只用 `references/ops-selection.md` 里的算子。
- 不在证据函数里手写 `clip` / `softmax` / 归一化——归一化由 `Engine` 统一做。
- 推导并写清每个状态的 `cap`；不确定时宁可算上界，也不要随手写 `1.0` 蒙混。
- 把规格暴露为模块级 `SPEC`，方便校验、测试与脚本复用。
- 默认输出到用户指定路径；未指定时给出完整可复制代码块。
- 生成后附带「规格摘要」（轴、状态、公式、参数、假设），让非技术用户也能核对。

### Step 4：校验与不变量

生成代码后必须执行（或让用户执行）：

```python
from alphabee.pattern import validate_spec, assert_bounds, assert_reachable
assert validate_spec(SPEC).ok
axes = {...}  # 每轴的代表性网格
assert_bounds(SPEC, axes)
assert_reachable(SPEC, axes)
```

- `assert_no_uniform`：若用户明确不要「全状态等概率」的假均分，加上。
- `assert_monotone`：需要用户声明 `long_states` / `short_states` 与轴恶化方向后再用。
- 至少跑 2 个 sanity 输入：一个应明显落在状态 A，一个落在状态 B；把结果给用户看。

### Step 5：用输入值计算状态分布

```python
from alphabee.pattern import Engine
engine = Engine(SPEC)
result = engine.classify(scores, previous=previous)   # previous 可选
```

输出并解读：

| 字段 | 含义 | 解读注意 |
|---|---|---|
| `belief.distribution` | 状态 → softmax 权重，和为 1 | **不是概率**，不要说「有 70% 概率」 |
| `belief.argmax_state` | 权重最大的状态 | 只是动作类型锚点 |
| `p_top` | 最大权重 | 与 `uncertain_p_top` 比较 |
| `uncertain` | 是否低于不确定性阈值 | 为 `True` 时不要强行下结论 |
| `belief.entropy` / `entropy_normalized` | 分布离散度 | 只表示分散程度，不是「不确定性概率」 |
| `normalized_evidence` | 每状态 `[0,1]` 归一化证据 | 用于解释「为什么是这个状态」 |
| `transition` | 迁移合法性/类型/触发轴 | `legal=False` 要提示「该跳跃不被迁移图允许」 |

已有规格、只想快速算一次时，用脚本（无需每次生成代码）：

```bash
python alphabee/skills/numeric-to-state/scripts/run_pattern.py \
  --spec ./my_spec.py \
  --scores '{"momentum": 0.8, "volatility": -0.3}'
```

脚本支持 `--scores-file` 与 `--previous-scores`（用于输出 transition）。

---

## 交付物清单

一次完整的「自然语言 → 代码」应包含：

- [ ] 规格摘要：轴、状态（含顺序与多空侧别）、每状态公式、`cap`、参数、迁移图
- [ ] 显式假设列表（凡是未由用户确认、由你推断/建议的都要列出）
- [ ] 可运行代码（含 `SPEC`，证据函数只依赖 `scores` 与 `ctx`）
- [ ] 校验结果：`validate_spec` + 至少 `assert_bounds` / `assert_reachable`
- [ ] 至少 2 个输入案例的分布结果与一句话解读
- [ ] 未决问题（如果还有）

---

## 反模式（出现即纠正）

- 把 softmax 权重说成「概率」「后验」。
- 缺失轴静默补 `0` 或直接跳过不告知。
- 证据函数里 `if state == "进攻": return ...` 式硬编码动作/仓位。
- `cap` 一律写 `1.0`，导致多因子乘积状态被系统性压低/抬高。
- 未确认就采用 `temperature` / `uncertain_p_top` / 阈值。
- 在证据层做 `clip` / `softmax` 二次归一化。
- 用 `Engine` 之外自己实现归一化与 argmax，导致与校验工具口径不一致。

---

## 参考

- `references/elicitation-checklist.md`：逐要素追问清单与问答示例。
- `references/ops-selection.md`：自然语言 → 算子映射、`cap` 推导与公式组合。
- `references/code-template.md`：代码骨架、完整示例与输出格式。
- `scripts/run_pattern.py`：加载 `SPEC` → 按输入 `scores` 输出分布（支持 previous）。
