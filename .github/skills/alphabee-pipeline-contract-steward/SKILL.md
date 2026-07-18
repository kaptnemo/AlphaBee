---
name: alphabee-pipeline-contract-steward
description: 当 AlphaBee 中某个节点、Artifact、Observation、Pydantic 模型、OrchestratorState 或提示词载荷的输出结构发生变化，需要评估对下游节点的影响、调整消费逻辑并同步修改相关节点时使用本 Skill。它帮助 Agent 以“数据契约传播”的方式处理改动，避免上游结构变化后下游仍按旧结构读取。
argument-hint: "说明变更起点（文件/节点/模型）、结构变化类型（新增/重命名/删除/嵌套/类型/语义变化）、是否要求兼容旧结构"
---

# Skill: AlphaBee 管线数据契约变更治理

## 目标

当 AlphaBee 的某个上游改动导致数据结构发生变化时，不只修“产出端”，还要把这次变化沿着整条管线向下游传播，直到所有实际消费者都被审视并按新契约完成更新。

本 Skill 的核心任务是：

- 分析“这次修改到底改变了什么数据结构”
- 找出哪些后续节点、payload builder、review gate、reporter、finalizer、recorder 会受影响
- 判断每个下游节点应该如何消费新数据
- 直接修改这些节点，而不是只停留在影响分析

## 什么时候使用本 Skill

当任务涉及以下情况时，使用本 Skill：

- 修改某个 orchestrator 节点的返回结构
- 修改 `OrchestratorState` 字段或字段语义
- 修改 `Artifact.value` / `Observation.payload` / `Decision` / `Issue` 的 shape
- 修改 Pydantic 模型后，需要检查下游节点如何读取这些字段
- 修改 `payload_builders`、`reporter`、`gates`、`finalize_message` 的输入输出契约
- 修改 `fact_values`、`derived_facts`、`signal_analysis`、`conflicts_result`、`verification_results` 等中间数据结构
- 修改最终 JSON payload，需要同步更新 `task_records` 或其他读取方

典型触发语句：

- “我改了 collect_raw_facts 的输出，帮我把后续节点一起改掉”
- “这个 artifact 结构变了，检查 downstream consumers”
- “把这个字段从平铺改成嵌套，同时更新后续节点”
- “分析这次数据结构调整对后面 graph nodes 的影响并落地修改”

## 输入建议

- 变更起点：哪个文件、哪个节点、哪个模型
- 结构变化：新增 / 重命名 / 删除 / 嵌套重组 / 类型变化 / 语义变化
- 兼容策略：是否必须兼容旧结构，还是允许一次性切换到新结构
- 影响范围：只改 active orchestrator，还是连 legacy / monitor / recorder 一并处理

## 核心原则

### 1. 把“数据契约”当成真正的改动对象

不要只看某一行代码是否改通，而要明确：

- 旧结构是什么
- 新结构是什么
- 哪些字段名、层级、类型、单位、语义发生了变化
- 哪些调用方仍在按旧结构读取

如果说不清旧/新结构差异，就不要开始改 downstream。

### 2. 先追踪消费者，再决定怎么改

上游结构一变，下游不一定都要“同样改写”，但必须逐个判断：

- 继续透传
- 在共享 builder 中做一次转换
- 在该节点内改读取路径
- 改 prompt / report payload 组织方式
- 显式忽略该新字段
- 因数据缺失而降级或报 issue

不允许“猜下游应该没影响”而不检查。

### 3. 共享边界优先放在 state / schema / builder 层

如果多个节点都依赖同一结构：

- 优先更新 `state.py`、Pydantic schema、shared payload builder、shared helper
- 避免每个节点各自写一份临时兼容逻辑
- 避免把 reshape 逻辑散落在多个 report / gate / node 中

### 4. breaking change 要么整体迁移，要么显式兼容

如果字段重命名、层级重组或语义改变会破坏下游读取：

- 默认进行原子化迁移：同步改完所有消费者
- 只有在用户明确要求兼容旧结构时，才保留 dual-read / fallback
- 不要无声吞掉新旧结构差异

### 5. 持久化边界要关注版本与序列化

以下位置属于“耐久边界”，结构变化不能只改内存对象：

- `Artifact.value`
- `finalize_message` 产出的最终 JSON payload
- `task_records/recorder.py` 的读取逻辑
- 任何 `model_dump()` / `to_dict()` / prompt payload 组织层

若契约变化跨越这些边界，要同步评估 `schema_version`、序列化字段和读取端兼容性。

## Active Orchestrator 下游传播图

当前主流程为：

```text
collect_raw_facts
→ run_analysis_engines
→ explore_conflicts
→ verify_hypotheses
→ run_thesis
→ review_thesis
→ generate_report
→ review_report
→ finalize_message
```

处理结构改动时，至少按这个顺序检查后续消费者。

## 必查文件清单

遇到 active pipeline 的数据契约变化时，优先检查这些文件：

- `alphabee/orchestrator/state.py`
- `alphabee/core/schemas.py`
- `alphabee/orchestrator/collectors.py`
- `alphabee/orchestrator/nodes/analyze.py`
- `alphabee/orchestrator/nodes/conflicts.py`
- `alphabee/orchestrator/nodes/verification.py`
- `alphabee/orchestrator/nodes/thesis.py`
- `alphabee/orchestrator/services/payload_builders.py`
- `alphabee/orchestrator/reporter.py`
- `alphabee/orchestrator/gates.py`
- `alphabee/orchestrator/agent.py`
- `alphabee/task_records/recorder.py`

如果改动落在 canonical fields / adapter / facts 采集层，同时联动使用 `alphabee-schema-steward`。

## 常见结构变化 → 下游处理规则

| 变化类型 | 必做判断 | 推荐处理方式 |
| --- | --- | --- |
| 新增字段 | 下游谁需要它？谁不该看到它？ | 先补 schema / state，再把真正需要的节点接上 |
| 字段重命名 | 是否还有任何消费者按旧名字读取？ | 全量替换消费者；除非明确要求兼容，否则不要双读 |
| 字段删除 | 下游原本依赖什么语义？ | 用替代来源、降级逻辑或 issue 显式处理 |
| 平铺改嵌套 | 读取路径和 prompt payload 是否失效？ | 在共享 builder 或 producer 统一重组，不要散改 |
| 类型变化 | 比较、round、排序、阈值判断是否受影响？ | 同步改类型注解、判空、格式化和规则逻辑 |
| 单值改多值 / 多期 | 下游是否仍假设单对象、首元素或标量？ | 更新循环、截断、排序和 summary 选择逻辑 |
| 语义变化 | 字段名没变但含义变了？ | 同步改解释文字、阈值、review 规则和报告描述 |
| Artifact 类型或 payload 改动 | reporter / reviewer / finalizer / recorder 是否还能读？ | 搜索全部 artifact type 读取点，统一迁移 |

## 标准工作流

### 1. 先读变更，明确 old shape / new shape

必须先从 diff、producer 代码或模型定义中提取：

- 旧结构
- 新结构
- 变化点
- 是否 breaking

优先输出或在心中形成类似结构：

```yaml
contract_change:
  producer:
    file:
    node_or_model:
  old_shape:
  new_shape:
  changed_fields:
    - name:
      change: add|rename|delete|nest|type|semantic
```

### 2. 建立 downstream consumer map

沿着代码搜索所有读取点，尤其是：

- `state.get("...")`
- `_find_artifact(..., "<type>")`
- `artifact.value[...]`
- `.get("<field>")`
- `model_dump()` / `to_dict()` / `to_fact_values()`
- prompt payload 组装

输出时优先整理为：

```yaml
downstream_consumers:
  - node: run_analysis_engines
    file: alphabee/orchestrator/nodes/analyze.py
    reads:
      - fact_values
    action: update
```

### 3. 对每个下游节点做“消费策略判断”

不要机械地把所有新字段透传给每个节点，而要回答：

- 该节点是否真的需要新字段？
- 它需要原始结构，还是需要摘要/转换后的结构？
- 最适合在 producer、shared builder 还是 consumer 侧改？
- 缺少该字段时，该节点应继续、跳过还是报 issue？

### 4. 修改所有受影响节点

修改时遵循：

- 先改类型定义 / schema / shared helper
- 再改直接消费者
- 再改 prompt payload / report payload / final payload
- 最后改 recorder、review、task record 等末端读取方

### 5. 检查“隐藏消费者”

特别留意这些容易漏掉的读取方：

- `review_thesis` 对中间 artifact 的读取
- `generate_report` 对 payload 的压缩与裁剪
- `review_report` / gates 对 evidence、issues、report sections 的读取
- `finalize_message` 最终 JSON 输出
- `task_records/recorder.py` 对最终 artifacts 的离线抽取

### 6. 验证不是“只有编译通过”，而是“数据路径真的通了”

至少确认：

- 上游产出的新结构能被下游正确读取
- 不存在仍按旧路径读取的节点
- 报告、review、final payload 中没有残留旧字段假设
- 若该改动影响 durable payload，读取端也已经同步

## Node 级处理提示

### `collect_raw_facts`

- 重点看 `fact_values`、`financial_facts`、`market_facts`、`fact_collection artifact`
- 若这里结构变了，优先检查 `run_analysis_engines`、`build_company_context`、`reporter`

### `run_analysis_engines`

- 重点看 `derived_facts`、`signal_analysis`、`anomaly_report`
- 若这些 payload 改了，继续追 `payload_builders`、`run_thesis`、`review_thesis`、`reporter`

### `explore_conflicts` / `verify_hypotheses`

- 重点看 prompt payload 的 key、冲突结果结构、verification item shape
- 若结构改了，必须同步检查 `run_thesis`、`review_thesis`、`reporter`、`gates`

### `run_thesis`

- 重点看 `thesis_analysis` artifact 内的 `thesis`、`enhanced`、`industry_context`、`anomaly_data`、`conflict_data`
- 若这里改动，后面通常至少影响 `review_thesis`、`generate_report`、`task_records`

### `generate_report`

- 它消费的是聚合后的结构化 payload，不是原始节点输出
- 若上游 shape 变化，应先判断是改 reporter payload builder，还是改上游 artifact shape

### `review_report` / `finalize_message`

- 若 final payload 结构变化，要把 `task_records/recorder.py` 一并看作强制更新项

## 禁止事项

- 不要只修 producer，不查 downstream
- 不要在多个 consumer 里复制粘贴相同 reshape 逻辑
- 不要为了“先跑通”添加无注释、无边界说明的 silent fallback
- 不要把语义变化伪装成“只是字段名改一下”
- 不要漏掉最终 payload、review gate、task record 这类末端消费者

## 完成标准

- [ ] 结构变化已被明确描述，而不是凭感觉修改
- [ ] 所有直接下游消费者都已检查
- [ ] 需要修改的节点、payload builder、reporter、gates、finalizer、recorder 都已同步
- [ ] 没有残留旧字段路径或旧 shape 假设
- [ ] 对 breaking change 的处理策略明确（整体迁移或显式兼容）

## 推荐对话指令

- “我刚改了一个节点的输出结构，按 downstream contract 把后续节点一起改完”
- “检查这次数据 shape 调整对 active orchestrator 的影响并直接落地修改”
- “把这个 artifact 从平铺改成嵌套，并同步更新 reporter、review、final payload”
- “分析这个 schema 变化后后续 nodes 该怎么消费，并修改所有受影响文件”
