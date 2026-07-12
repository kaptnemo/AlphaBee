# Data Collection Failed Auto-Fix Design

## 1. 背景

AlphaBee 在任务执行过程中经常会遇到数据获取失败，常见原因包括：

- Tushare 权限不足
- 接口返回字段缺失
- Tushare / AkShare / Eastmoney / Baostock / 爬虫接口调用失败
- 返回空数据、解析失败、限流、网络错误

这些失败目前会分散出现在日志、Issue、Langfuse trace 和各个工具层返回值中，但还没有形成一个可被自动修复流程持续消费的稳定数据源。

## 2. 目标

建立一套“数据获取失败 → 聚合 → 修复 → 验证 → 关闭”的闭环系统，用于：

1. 收集数据获取失败事件
2. 自动聚合同类问题
3. 定期交给 Claude agent 尝试修复
4. 支持换数据源、更新接口、更新爬虫、补字段映射
5. 保留 Langfuse 作为追踪证据，但不依赖它承担工单职责

## 3. 核心原则

### 3.1 Langfuse 只做 trace，不做工单主存

Langfuse 适合保存：

- 调用链
- prompt / response
- token 用量
- session / trace 上下文
- 原始异常证据

但它不适合作为修复任务系统的唯一数据源，因为它不擅长：

- 去重
- 状态流转
- 问题聚合
- 重试调度
- 修复闭环管理

### 3.2 失败事件与问题单分层

建议分三层：

1. **原始失败事件**：每次失败都记录，append-only
2. **聚合问题单**：对同类失败去重、统计、分派
3. **修复任务**：Claude agent 读取问题单后执行修复

## 4. 数据模型

### 4.0 技术栈约束

本设计建议落到以下实现栈：

- **数据库**：SQLite
- **ORM**：SQLAlchemy
- **迁移**：Alembic

原因是这套栈足够轻量，适合先把失败事件闭环跑通，同时也便于后续扩展到更强的查询、聚合和迁移管理。

### 4.1 原始失败事件 `data_fetch_events`

每次数据获取失败都记录一条，尽量保留原始信息，不在写入时过度归类。

建议字段：

| 字段 | 说明 |
|---|---|
| `event_id` | 唯一 ID |
| `occurred_at` | 发生时间 |
| `provider` | tushare / akshare / eastmoney / baostock / crawler |
| `api_name` | 具体接口或抓取任务名 |
| `symbol` | 标的代码 |
| `error_type` | permission / missing_field / timeout / parse_error / network / rate_limit / empty_response / unknown |
| `error_message` | 原始异常信息 |
| `missing_fields` | 缺失字段列表 |
| `request_payload` | 入参摘要 |
| `response_snippet` | 返回内容截断 |
| `severity` | low / medium / high |
| `trace_id` | Langfuse trace id |
| `session_id` | 会话 id |
| `task_id` | 任务 id |
| `fingerprint` | 去重签名 |

### 4.2 聚合问题单 `data_fetch_issues`

同类事件聚合后形成可修复的问题单。

建议字段：

| 字段 | 说明 |
|---|---|
| `issue_id` | 问题单 ID |
| `fingerprint` | 聚合签名 |
| `title` | 简短描述 |
| `status` | new / active / investigating / fixed / wont_fix / ignored |
| `provider` | 主要来源 |
| `api_name` | 主要接口 |
| `error_type` | 主要错误类型 |
| `occurrence_count` | 累计次数 |
| `first_seen_at` | 首次出现时间 |
| `last_seen_at` | 最近出现时间 |
| `sample_event_id` | 代表性事件 |
| `owner_agent` | 负责修复的 agent |
| `fix_strategy` | 换源 / 补字段 / 改接口 / 改爬虫 / 降级 |
| `resolution_note` | 修复说明 |
| `verification_status` | pending / passed / failed |

### 4.3 修复任务 `data_fix_tasks`

为 Claude agent 提供可执行任务单。

建议字段：

| 字段 | 说明 |
|---|---|
| `task_id` | 任务 ID |
| `issue_id` | 关联问题单 |
| `status` | pending / running / done / failed |
| `prompt_context` | 任务上下文摘要 |
| `patch_target` | 目标模块或文件 |
| `result_summary` | 修复结果摘要 |
| `verification_result` | 验证结果 |

## 5. Fingerprint 设计

fingerprint 用于判断“是不是同一个问题”。

建议组成：

- `provider`
- `api_name`
- `error_type`
- `missing_fields`（排序后拼接）
- `关键报错前缀`

是否把 `symbol` 纳入 fingerprint 取决于问题类型：

- **接口级问题**：不要纳入 symbol
- **标的特异问题**：可以纳入 symbol

## 6. 处理流程

### 6.1 采集阶段

在以下层统一捕获并写入失败事件：

- `alphabee/collectors/tushare/helper.py`
- `alphabee/collectors/akshare/helper.py`
- `alphabee/tools/common.py`
- `alphabee/agents/facts/tools/*`
- `alphabee/orchestrator/collectors.py`

推荐做法：

1. 捕获异常或检测到空结果
2. 生成 `data_fetch_events`
3. 同步写入 `trace_id` / `session_id` / `task_id`
4. 生成 fingerprint
5. 更新或创建对应 `data_fetch_issues`

### 6.2 聚合阶段

将相同 fingerprint 的事件合并，更新：

- `occurrence_count`
- `last_seen_at`
- `sample_event_id`
- `severity`

### 6.3 修复阶段

定时任务扫描 `status in (new, active)` 的问题单，交给 Claude agent：

1. 读取问题单与样本事件
2. 读取 Langfuse trace
3. 分析失败模式
4. 通过 **Claude Agent SDK（Python）** 直接读取最新项目代码和数据库中的任务
5. 尝试修复代码 / 换源 / 更新字段映射 / 改爬虫
6. 运行测试并验证修复结果
7. 提交 git merge request

### 6.4 验证阶段

修复完成后做最小验证：

- 重新调用同一接口
- 用同一标的验证返回字段
- 检查是否仍然报错
- 若成功，状态置为 `fixed`
- 若失败，保留样本并继续积累

## 7. 为什么不只靠 Langfuse

Langfuse 适合“看见问题”，但不适合“管理问题”。

单独记录失败事件的优势是：

- 稳定可查询
- 可去重
- 可统计频次
- 可自动分派
- 可追踪修复结果
- 可支持定期批处理

Langfuse 的优势是：

- 保留详细上下文
- 方便回看一次调用链
- 方便定位 prompt / trace / 上下游输入

因此最佳实践是：

**Langfuse 作为证据层，失败记录库作为工单层。**

## 8. 与现有 AlphaBee 架构的衔接

当前项目已经有：

- `Issue`：可用于运行时问题
- `TaskRecorder`：可从最终 payload 统计任务信息
- `orchestrator/collectors.py`：统一采集入口
- `collectors/*/helper.py`：外部数据源封装层

建议新增专门的失败记录模块，不要把它混进普通 `Issue` 里：

- 普通 `Issue` 面向一次任务执行
- 数据失败记录面向长期修复闭环

## 9. 最小可行落地方案

### Phase 1

- 新增 `data_fetch_events`
- 新增 `data_fetch_issues`
- 在数据源 helper 中统一上报失败

### Phase 2

- 增加 fingerprint 去重
- 增加定时扫描任务
- 生成 Claude 修复任务

### Phase 3

- 自动验证修复结果
- 支持换源策略
- 支持字段映射更新
- 支持爬虫修复

## 10. 建议结论

建议**不要只依赖 Langfuse**，而是建立单独的数据获取失败记录系统。

最合理的方案是：

1. Langfuse 保存 trace 和证据
2. 独立失败库保存可修复事件
3. 聚合成问题单
4. 定期交给 Claude agent 自动修复
5. 验证后关闭问题单

Claude agent 的实现建议直接使用 **Python Claude Agent SDK**，让 agent 读取：

- 最新项目代码
- SQLite 里的问题单和失败事件
- Langfuse trace

然后自动完成：

- 修复实现
- 测试验证
- Git merge request 提交
