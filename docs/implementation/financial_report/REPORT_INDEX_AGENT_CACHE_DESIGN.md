# 财报检索 Index / FetchAgent 缓存设计（降低重复探索与 read_file 误读）

> **状态：已实施（设计稿 2026-10-24；P0 index 缓存 + 压缩目录树、P1 backend 路径守卫 + 相似路径提示、P2 fetch agent 缓存 + 并发复用均已落地；P3（可选）确定性预选 / evidence 缓存未实施、本期不做）**。本文是 `alphabee/financial_report/` 与 `alphabee/tools/financial_report.py` 的代码级实施设计：给出问题复现、缓存分层、数据契约、并发模型、失效策略、接线锚点、测试矩阵、分阶段落地与回滚。
>
> **关联链路**：`query_financial_report` ← `verify_hypotheses` 并行冲突验证 / `resolve_driver_profile` / `report_pipeline`。
>
> **非目标**：不改写财报内容、不改 `TaskRecord`/`OrchestratorState` 顶层契约、不引入跨进程缓存服务、不让缓存承担投资结论。

---

## 0. 摘要

当前每次调用 `query_financial_report` 都会重新做三件事：

1. 定位报告目录（已有 10 分钟 TTL 缓存）；
2. 重新扫描报告目录并生成 system prompt 里的目录树；
3. 重新编译一个 `create_deep_agent` 实例。

随后 LLM 在一个**全新的 conversation** 里从目录树重新探索。由于目录树只给“缩进的文件名”、大型报告在 500 项处截断，模型经常跳过 grep 直接拼路径，产生 `read_file ... not found`；错误又以普通 `ToolMessage(status="error")` 返回，没有给出正确路径提示，模型只能再猜或补 grep。

本设计把“缓存”拆成三层，分别解决不同问题：

| 层 | 缓存对象 | 主要收益 | 是否减少 LLM 探索 |
|---|---|---|---|
| L1 | `ReportIndex`（文件清单 + 压缩目录树 + 版本） | 省目录扫描；为路径校验/候选预选提供数据 | 间接（配合 §7/§8） |
| L2 | `CompiledStateGraph`（fetch agent） | 省 graph 编译、prompt 构建、模型构造 | 否（每次仍是新 conversation） |
| L3（可选） | `(root, version, normalized_query) → answer/snippets` | 完全相同的查询去重 | 是，但只对精确重复有效 |

**核心结论**：只缓存 fetch agent 不会让模型记住上次探索；真正降低 `read_file` 误读的是把 `ReportIndex` 用在 **backend 级路径校验 + 相似路径提示**，以及 **压缩目录树 + 候选文件预选**。agent 缓存是性能优化，index 缓存才是正确性基础设施。

---

## 1. 问题定义与证据

### 1.1 现象

用户日志中的典型错误：

```text
[3715] 🔧 调用工具 [verify_hypotheses > 3 > tools]
       ▶ read_file
          入参: {"file_path": "/企业精神/五、_合并财务报表主要项目注释－续/营业收入和营业成本/营业收入和营业成本情况.md"}

[3716] ❌ 工具结果 [verify_hypotheses > 3 > tools]
       ◀ read_file
       Error: File '/企业精神/.../营业收入和营业成本情况.md' not found
```

真实文件是：

```text
reports/紫金矿业(601899)/财报/紫金矿业集团股份有限公司2026年半年度报告/
  企业精神/五、_合并财务报表主要项目注释－续/营业收入和营业成本/
    (1)_营业收入和营业成本情况.md
```

模型把文件名前的 `(1)_` 前缀猜掉了，属于“根据目录名拼路径”，而不是“先 grep 再 read”。

同一日志中也能看到模型在报错后补 grep、再读到正确文件的轨迹：

```text
16:59:33  read_file  error    result_length=72   （猜错文件名）
16:59:33  grep       ...                         （补搜索）
16:59:33  read_file  success  result_length=12371（读到正确文件）
```

即错误本身可自愈，但会多消耗 1–2 个 LLM/工具步；在并行冲突验证里被放大。

### 1.2 量化证据

对 `logs/alpha_arena.log.2026-10-22` 与 `logs/alpha_arena.log` 中 `read_file` 的 `tool_result.status` 做统计（按 agent 顶层分组）：

| 日志 | read_file 结果数 | error | 错误率 | verify_hypotheses 内 error |
|---|---:|---:|---:|---:|
| 2026-10-22 | 1,407 | 47 | 3.3% | 45 / 1,149 = 3.9% |
| 2026-10-24 | 1,269 | 86 | 6.8% | 82 / 1,148 = 7.1% |

结论：**绝对错误数和错误率都在上升**，且错误几乎全部发生在 `verify_hypotheses` 的财报检索链路上。

报告规模（本机 `reports/` 实测）：

| 指标 | 数值 |
|---|---:|
| 报告根目录数 | 277 |
| markdown 文件总数 | 83,856 |
| 单报告文件数 p50 / p90 / max | 390 / 615 / 766 |
| 带 `.report_manifest.json` 的报告 | 277 / 277 |

构建成本（本机实测，未含 LLM）：

| 操作 | 耗时 |
|---|---:|
| `build_file_tree()`（735–766 文件，截断到 500 项） | 18–20 ms |
| `create_report_fetch_agent()` 首次（含 import） | 约 619 ms |
| `create_report_fetch_agent()` 热态 | 约 74–75 ms |

单次构建成本不算大，但一次分析运行中 `query_financial_report` 可被调用几十次（每个 conflict 并行），累计构建与重复探索不可忽略。

### 1.3 调用链还原

```text
verify_hypotheses（Orchestrator 节点，asyncio.gather 并行处理 conflicts）
  └─ _verify_single_conflict(...)                         verification.py:38-118
       └─ verify_hypotheses_agent_factory()               verify_hypotheses/agent.py:19-59
            ├─ 主 agent 可用工具：write_todos / task / web_search /
            │  query_tushare / query_financial_report
            │  （read_file/ls/glob/grep 被 _ToolExclusionMiddleware 移除，agent.py:38-40）
            └─ LLM 调用 query_financial_report
                 └─ query_financial_report(...)            tools/financial_report.py:451-515
                      ├─ _REPORT_LOCATE_CACHE（已有，TTL 600s）    :496-499
                      ├─ create_report_fetch_agent(root_path)     :502
                      │    ├─ build_fetch_prompt() → build_file_tree()  fetch_deepagents.py:45 / fetch.py:29-55
                      │    ├─ FilesystemBackend(root=报告目录, virtual_mode=True)  fetch_deepagents.py:46
                      │    └─ create_deep_agent(...)              fetch_deepagents.py:47-59
                      └─ agent.ainvoke(...)                       :503-506
```

关键点：

- 报错不是 `verify_hypotheses` 主 agent 直接 `read_file`。实测主 agent 的模型可见工具集只有 `write_todos / task / web_search / query_tushare / query_financial_report`。
- 日志 namespace（如 `verify_hypotheses > 3 > tools > 2`）是并行 conflict 的嵌套子图/工具节点编号；`read_file` 发生在 `query_financial_report` 内部的 **report fetch agent**。
- 同样的 `read_file` 也出现在 `resolve_driver_profile > tools > N` 下，进一步证明它是财报检索工具的内层行为，与具体上层 agent 无关。

### 1.4 三层根因

**根因 A：缺少 index/agent 级缓存**

- 只有“请求 → 根目录”的定位缓存（`_REPORT_LOCATE_CACHE`），没有“根目录 → 文件清单/树/agent”的缓存；
- 每次 `query_financial_report` 都重新 `build_file_tree()` + `create_deep_agent()`；
- 每次都是全新 conversation，模型没有任何跨调用的“这份报告上次怎么找”的记忆。

**根因 B：prompt 里的目录树不适合直接拼路径**

- `build_file_tree()` 只输出缩进的 `child.name`，不是完整相对路径（`fetch.py:47`），模型必须自己把目录层级拼回去；
- 默认 `max_entries=500`，大型报告（p90 已 615 文件）会被截断（`fetch.py:29/54`），截断后的章节模型更容易靠猜；
- prompt 虽然要求“1. Grep → 2. Read”（`fetch.py:95-98`），但这是软约束：没有工具层强制，模型看到树后会走“直接 read”的捷径；
- 文件名包含 `(1)_`、`－续`、`-续` 等规范化差异，拼路径时容易漏字符。

**根因 C：not-found 返回没有纠正信号**

- `FilesystemBackend.read()` 找不到时返回 `ReadResult(error="File '...' not found")`（deepagents `filesystem.py:433`），中间件转成 `ToolMessage(status="error")`（`filesystem.py:1091-1097`）；
- 错误文本只有“没找到”，没有“你可能想找的是这 5 个真实路径之一”；
- 模型只能重新 grep 或继续猜，错误被记进日志但不会触发任何缓存/检索策略修正。

### 1.5 为什么错误在增加

1. **并行冲突验证放大了调用量**：`verify_hypotheses` 对每个 conflict 并行调用 `query_financial_report`（`verification.py:163-168`），每个调用都会新建一个无记忆的 fetch agent。
2. **research agenda 增加了财报取证密度**：`driver_profile` / `verify_hypotheses` 的 prompt 明确要求用 `query_financial_report` 查本地财报，单次运行内的调用数上升。
3. **报告规模上升**：p90 已超过 500 项截断阈值，猜路径的失败面变大。
4. **错误可“自愈”**：模型补 grep 后往往能读对，所以任务不会失败，但工具步数和错误日志持续累积，表现为“错误越来越多”。

---

## 2. 设计原则

| 编号 | 原则 | 说明 |
|---|---|---|
| P1 | **index 与 agent 分开缓存** | index 是正确性基础设施；agent 是性能优化，可独立回滚 |
| P2 | **以 canonical root path 为一级 key** | 公司名/代码/报告类型只是定位维度；同一报告不应因写法不同重复缓存 |
| P3 | **缓存 compiled graph，不缓存 conversation** | 每次 `ainvoke` 仍用全新 messages；禁止跨 query 复用消息历史 |
| P4 | **版本化失效，TTL + capacity 兜底** | `.report_manifest.json` 提供版本；缓存必须有 max entries，避免 277 份报告累积 |
| P5 | **路径校验放在 backend 层，而不是 middleware 层** | deepagents 的 `middleware=` 只作用于主 agent，GP subagent 不继承；backend 被所有子 agent 共享 |
| P6 | **fail-open** | 缓存构建失败时回退到现有“每次新建”路径，不能因为缓存让查询失败 |

---

## 3. 缓存对象与数据契约

新增模块：`alphabee/financial_report/report_cache.py`。只负责“目录 → index/agent 的缓存”，不 import `alphabee/tools`，避免循环依赖。

### 3.1 `ReportIndex`

```python
@dataclass(frozen=True)
class ReportIndex:
    root: Path                    # resolve() 后的绝对路径
    version: str                  # 版本指纹，见 §3.2
    rel_files: tuple[str, ...]    # 全部 .md 相对路径（posix，无前导 /）
    rel_dirs: tuple[str, ...]     # 全部目录相对路径（posix）
    tree_compact: str             # 给 prompt 的压缩目录树（章节目录 + 文件数）
    total_files: int
    built_at: float               # time.monotonic()，仅诊断用
```

构建步骤：

1. `root = Path(root).resolve()`；
2. 一次 `rglob("*.md")` 收集文件，`rglob("*")` 收集目录（可合并为一次 `os.walk`，避免两次遍历）；
3. 每条路径存 `p.relative_to(root).as_posix()`，统一使用 posix 分隔符；
4. `tree_compact` 只渲染目录层级 + 每个目录的直接 `.md` 数量，不渲染 500 个文件名；
5. 计算 `version`（§3.2）。

不变量：

- `rel_files` 排序稳定（便于测试与快照）；
- 所有路径都不带前导 `/`，消费方统一用 `posixpath.normpath` 归一化；
- index 是不可变对象，可安全被多个并发 invocation 共享。

规模与内存估算：

- 单报告 766 文件 × 平均路径 60 字符 ≈ 46k 字符（UTF-8 约 80–120 KB）；
- 全部 277 份报告、83,856 个文件 ≈ 500 万字符，单进程不建议全量常驻；
- 因此仍建议 `max_entries=32/64`，只保留一次运行热点报告。

### 3.2 版本指纹 `version`

优先使用报告写入时生成的 manifest（`report_parser.py:346-363`）：

```python
def report_version(root: Path) -> str:
    manifest = root / ".report_manifest.json"
    if manifest.is_file():
        st = manifest.stat()
        # 报告重写会整体替换 manifest，mtime_ns + size 足以区分版本
        return f"manifest:{st.st_mtime_ns}:{st.st_size}"
    st = root.stat()
    return f"dir:{st.st_mtime_ns}"
```

选择理由：

- 277/277 报告当前都有 manifest，覆盖率高；
- 比每次解析 JSON 更便宜（一次 `stat`）；
- 报告重写 → manifest 替换 → `mtime_ns/size` 变化 → 自动换 key；
- 手工编辑 markdown 但未改 manifest 的情况，由 TTL 兜底（见 §6）。

可选增强：把 manifest 里的 `created_at + file_count` 读出来做诊断日志，但 key 仍用 `stat` 结果即可。

### 3.3 Key 设计

**定位缓存（已有）**

```python
locator_key = (company_name_or_resolved_name, year, tuple(report_types))
# value = root path str | None
```

保持现状（`financial_report.py:388-400`），只补失效入口。

**Index key**

```python
index_key = (str(root.resolve()), version)
```

**Agent key**

```python
agent_key = (
    str(root.resolve()),
    version,
    _FETCH_PROMPT_VERSION,   # 手动常量；build_fetch_prompt 结构变更时 +1
    "financial_report",      # model component
)
```

为什么不用 `(company, year, report_type)` 直接做 artifact key：

1. 同一个 root 可能由多个定位 key 命中（公司名 vs 代码、类型列表），用 root 去重；
2. 报告目录可能被重新解析替换，定位 key 不变但内容变了，必须靠 `version` 区分；
3. root path 天然支持 `report_pipeline.answer_report_question(report_dir)` 这种直接传目录的入口（`pipeline.py:322-347`），不必反向构造公司/年份。

### 3.4 缓存配置

```python
@dataclass(frozen=True)
class ReportCacheConfig:
    enabled: bool = True
    index_enabled: bool = True        # P0 开关
    agent_enabled: bool = True        # P2 开关（并发压测不通过时置 false）
    path_guard_enabled: bool = True   # P1 开关
    index_ttl_s: float = 1800.0       # 30 min
    agent_ttl_s: float = 1800.0       # 30 min
    max_index_entries: int = 64
    max_agent_entries: int = 16
    small_report_full_paths: int = 80 # <=80 文件时 prompt 给完整路径
```

初期可用模块常量；确认稳定后再考虑接入 `config.yaml`：

```yaml
financial_report:
  cache:
    enabled: true
    index_enabled: true
    agent_enabled: true
    path_guard_enabled: true
    index_ttl_s: 1800
    agent_ttl_s: 1800
    max_index_entries: 64
    max_agent_entries: 16
    small_report_full_paths: 80
```

对应 `Settings` 新增 `FinancialReportSettings`（参照 `DomainContextSettings` 的缺段默认模式，`config/__init__.py:357`）。缺段时全部取默认值，旧 config 行为不变。

### 3.5 对外 API（建议签名）

```python
def build_report_index(root: str | Path) -> ReportIndex: ...

def report_version(root: str | Path) -> str: ...

def get_report_index(root: str | Path) -> ReportIndex: ...
    # 同步入口；供同步测试 / pipeline 直接使用
    # 内部：命中 index cache → 返回；否则 single-flight 构建

async def aget_report_agent(root: str | Path) -> CompiledStateGraph: ...
    # 异步入口；index + agent 都在 AsyncTTLCache 中 single-flight
    # compute 内部 await asyncio.to_thread(_build_agent_with_index, ...)

def invalidate_report_caches(root: str | Path | None = None) -> None: ...
def clear_report_caches() -> None: ...   # 测试专用
def suggest_paths(index: ReportIndex, requested: str, *, n: int = 5) -> list[str]: ...
```

---

## 4. 运行流程

### 4.1 冷启动（首次查询某份报告）

```text
query_financial_report(request)
  │
  ├─ locator cache: (company, year, types) → root
  │
  ├─ index cache: (root, version) miss
  │     └─ to_thread(build_report_index)   # 一次 rglob，几十 ms
  │
  ├─ agent cache: (root, version, prompt_ver) miss
  │     └─ to_thread(create_report_fetch_agent(root, index))
  │
  ├─ 构造 ReportFilesystemBackend(root, index)   # §7
  │
  └─ agent.ainvoke(messages=[HumanMessage(query)])
```

### 4.2 热启动（同一报告的后续查询）

```text
query_financial_report(request)
  ├─ locator cache hit
  ├─ index cache hit（一次 manifest stat 确认 version）
  ├─ agent cache hit
  └─ agent.ainvoke(全新 messages)
```

注意：**热启动不会复用上次的 messages/tool results**，只复用编译产物与索引。不同 query 之间没有上下文污染。

### 4.3 并行查询同一报告

`verify_hypotheses` 并行发起 N 个 conflict 查询时：

```text
call_1 ─┐
call_2 ─┼─ index cache single-flight：只有 call_1 真正 build，其余 await 同一个 future
call_3 ─┤
...     │
        └─ agent cache single-flight：只有第一个真正 compile
```

完成后所有 call 各自 `ainvoke` 自己的 messages。需要验证 compiled graph 是否可安全并发（§5）。

### 4.4 缓存失败回退

```text
try:
    agent = await aget_report_agent(root)
except FileNotFoundError:
    raise                                      # 报告目录不存在：保持原有异常语义
except Exception:
    logger.warning("report_cache_build_failed", root=root, exc_info=True)
    agent = create_report_fetch_agent(root)    # 现有路径，保证可用
```

只对“缓存构建失败”回退；`FileNotFoundError`（报告目录不存在）保持原有异常语义，不吞掉。

---

## 5. 并发模型

### 5.1 Single-flight

- index 与 agent 都应“每个 key 只构建一次”，避免并行冲突时重复 `rglob` / `create_deep_agent`；
- 推荐使用现有 `AsyncTTLCache`（`alphabee/tools/cache.py:52`）：
  - `compute` 是 coroutine，内部 `await asyncio.to_thread(build_fn)`；
  - 同时到达的调用共享同一个 `asyncio.Task`，不会重复执行；
- 如果入口是同步函数，用 `SyncTTLCache`（`cache.py:11`）即可，但不要在事件循环线程里直接调用耗时构建（会阻塞 loop）。
- `AsyncTTLCache` 的 `_inflight` 是 `asyncio.Task`，跨事件循环（例如多次 `asyncio.run`、pytest 每个用例一个 loop）不要共享未完成的 in-flight 项；测试需 `clear_report_caches()`，生产入口应保证同一 loop 内调用。

### 5.2 Thread pool 注意事项

- `create_report_fetch_agent` 是同步 CPU/IO 混合构建（约 75 ms 热态），放到 `asyncio.to_thread`；
- `build_report_index` 同样放线程池；
- 不要在 async 入口里直接 `SyncTTLCache.get_or_compute`：`threading.Event.wait()` 会阻塞事件循环；
- 若并发冲突数可能超过默认线程池上限（`min(32, cpu+4)`），优先用 `AsyncTTLCache` 的单 future 模式，而不是让大量线程等待。

### 5.3 复用 compiled graph 的安全性

**理论依据**

- 无 `checkpointer` 时，LangGraph 的 invocation state 由每次 `ainvoke` 传入，compiled graph 本身不保存对话；
- deepagents 的 middleware 在构建期完成配置，运行期状态主要落在 `state["messages"]` / `state["todos"]`；
- `FilesystemBackend` 无内部可变状态；
- Chat model 可并发调用（OpenAI 客户端支持 async 并发）。

**必须验证的风险点**

1. `SummarizationMiddleware` / `TodoListMiddleware` 是否在实例上保存跨 invocation 状态；
2. deepagents `SubAgentMiddleware` 的 GP subagent 是否共享可变 closure；
3. `TokenUsageCallbackHandler` 在并发下是否按 `run_id` 正确记账；
4. LangGraph 版本对同一 compiled graph 并发 `ainvoke` 的保证。

**验证方法**

- 用 fake model（不发真实请求）并发 `asyncio.gather` 20 次同 root 不同 query；
- 每个 invocation 的最终答案必须对应自己的 query；
- 记录 builder 调用次数必须为 1；
- 若任一断言失败 → 关闭 agent 缓存，仅保留 index 缓存（P6 fail-open）。

### 5.4 降级开关

- `financial_report.cache.agent_enabled`（或模块常量）默认开启；
- 压测不通过时置 false：`aget_report_agent` 每次 `create_report_fetch_agent`，但 index 仍复用；
- 回滚粒度：index 缓存、agent 缓存、backend 路径守卫三个开关互相独立。

---

## 6. 失效、容量与一致性

### 6.1 失效来源

| 来源 | 检测方式 | 处理 |
|---|---|---|
| 报告被重新解析 | manifest `mtime_ns/size` 变化 | version 变化 → 新 key |
| 报告目录删除/改名 | `root.is_dir()` 失败 | 不缓存负结果；按原逻辑抛错 |
| 手工编辑 markdown | manifest 不变 | TTL 兜底（30 min） |
| 进程重启 | - | 缓存自然清空 |
| 多进程/多 worker | - | 每进程独立缓存，可接受 |

### 6.2 定位缓存的一致性

- `_REPORT_LOCATE_CACHE` 目前 TTL 600s，且会缓存 `None`（负缓存）；
- 新报告写入后，同一组合可能 10 分钟内仍返回 `None`；
- 建议在 `write_markdown_report_folder()` 成功后调用 `invalidate_report_caches(target)`，同时清掉定位负缓存（按公司/root 匹配）；
- 若不想让 parser 依赖 cache 模块，可只清 artifact 缓存，定位负缓存继续靠 TTL；但“新报告查不到”的用户体验会保留 10 分钟。

### 6.3 容量与淘汰

现有 `SyncTTLCache` / `AsyncTTLCache` **没有 max entries**，只按 TTL 惰性覆盖。新增需求：

- `ReportCache` 内部使用 `OrderedDict` + `expire_at`；
- 读取时先剔除过期项；
- 写入后若 `len > max_entries`，淘汰最旧（LRU）；
- 提供 `stats()` 返回 `hits/misses/entries/evictions/builds`，供日志与测试。

推荐上限：

- index：64（覆盖一次运行涉及的股票 + 余量）；
- agent：16（agent 体积大，且一次运行通常聚焦少数标的）。

### 6.4 观测字段

每次缓存访问记录：

```text
event=report_cache
kind=index|agent
result=hit|miss|build|evict|error
root=...
version=...
duration_ms=...
entries=...
```

用于回答“缓存是否真的命中”“构建耗时是否下降”“内存是否失控”。

---

## 7. 与 `read_file` 误读治理的结合（核心）

### 7.1 为什么放在 backend 层

`create_report_fetch_agent` 的 `middleware=[...]` 只作用于主 agent；deepagents 自动添加的 general-purpose subagent 有自己的 middleware 栈，不继承调用方传入的 `middleware`。因此：

- 在 middleware 里拦截 `read_file` → 只保护主 agent，GP subagent 仍可能猜路径；
- 在 `FilesystemBackend` 子类里拦截 → 主 agent、GP subagent、任何嵌套 agent 共用同一个 backend，全部受保护。

这与 `verify_hypotheses` 主 agent 的排除中间件是两类问题：排除中间件解决“不该有这个工具”，backend 守卫解决“有工具但不该读错路径”。

### 7.2 `ReportFilesystemBackend`

```python
class ReportFilesystemBackend(FilesystemBackend):
    def __init__(self, root_dir: str | Path, index: ReportIndex):
        super().__init__(root_dir=str(root_dir), virtual_mode=True)
        self._index = index
        self._rel_set = frozenset(index.rel_files)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        result = super().read(file_path, offset=offset, limit=limit)
        if result.error and "not found" in result.error:
            hints = suggest_paths(self._index, file_path, n=5)
            if hints:
                result = ReadResult(
                    error=result.error + "\n\n可能的真实路径（请直接使用其一）：\n"
                    + "\n".join(f"- /{p}" for p in hints)
                )
        return result
        # 可选严格模式：file_path 归一化后不在 _rel_set → 直接返回 error + hints
```

设计取舍：

- **仅提示、不强制拦截**：目录可能新增文件而 index 尚未刷新，严格 allowlist 会误伤；先做“不存在 → 提示”，风险最低；
- **严格模式可选**：若希望彻底禁止拼路径，可对 `read` 增加 `if rel not in _rel_set: return error+hints`；需要接受“index 未包含新文件”的短暂不可读；
- **路径归一化**：比较前统一 `posixpath.normpath`，并去掉前导 `/`；同时兼容 Windows 分隔符；
- **建议算法**：
  1. 先按 basename 精确匹配（忽略目录，处理“目录对、文件名错”）；
  2. 再 `difflib.get_close_matches(requested_rel, index.rel_files, n=5, cutoff=0.5)`；
  3. 最后按目录前缀过滤，优先同目录候选；
  4. 返回路径统一带前导 `/`，与虚拟文件系统风格一致。

### 7.3 对模型的提示词配合

- system prompt 明确：`read_file` 只允许使用 `grep/glob` 返回值或 prompt 中 `<candidate_files>` 给出的路径；
- 若 `read_file` 返回“可能的真实路径”，下一次必须直接使用其中一条，禁止继续猜；
- 结论引用仍保留“文件名即可”（`fetch.py:119`），但引用格式改为**完整相对路径**，避免上游 agent 拿到半截路径后再次误读。

### 7.4 可观测性

- backend 记录 `read_file_miss` 日志：`root/version/requested/hints_count`；
- 指标目标：`read_file` error rate 从当前 6.8% 降到 <1%；
- 不改变 `ToolMessage(status="error")` 语义：仍然让模型看到“失败”，只是多给纠正线索。

---

## 8. Prompt / 检索改造

### 8.1 压缩目录树

当前：500 个文件名 + 缩进，约 18 KB 字符（大型报告），且截断。

改为：

```text
<report_stats>
文件总数: 766
目录数: 84
</report_stats>

<chapter_map>
/企业精神 (12 files)
/前瞻性陈述的风险声明 (37 files)
/五、_合并财务报表主要项目注释－续 (142 files)
...
</chapter_map>

<candidate_files>
（可选）根据 query 预选出的 1–5 个完整相对路径
</candidate_files>
```

规则：

- `total_files <= small_report_full_paths`（默认 80）：直接给完整路径列表，省掉 grep；
- 大报告：只给章节目录 + 文件数，完整路径通过 grep/候选列表获得；
- 不再依赖 `max_entries=500` 截断；压缩树按目录层级渲染，天然可控。

### 8.2 强制 grep-first 的软硬结合

- prompt 保留“先 grep 再 read”；
- 硬约束由 backend 的“路径不存在 → 相似路径提示”承担；
- 可选 P3：在调用 LLM 前程序化 grep，直接把 `<candidate_files>` 填好，模型不需要从零探索。

### 8.3 引用格式

- 现状：`最后给出结论时，注明引用自哪个文件（文件名即可）`（`fetch.py:119`）；
- 改为：`引用完整相对路径`，例如 `企业精神/五、.../(1)_营业收入和营业成本情况.md`；
- 上游 agent 拿到的引用是可读的、可验证的；若它想二次读取，也更容易走 `query_financial_report` 而不是自己拼路径。

---

## 9. 可选增强（P3，不在首期）

### 9.1 确定性候选预选

在 `query_financial_report` 调用 LLM 前：

1. 从 query 提取关键词（引号/书名号中的短语、连续中文 2–6 字、英文/数字 token）；
2. 用 index 的文件名/目录名做第一轮过滤；
3. 对候选文件做轻量 grep（优先调用 ripgrep，`FilesystemBackend` 已有 Python fallback）；
4. 取 top 3–5 个文件作为 `<candidate_files>` 注入 prompt。

收益：减少 LLM 的 grep 步数与盲目 read；风险：关键词抽取对中文 query 不稳定，需要离线评测命中率。建议先做 A/B，不默认开启。

### 9.2 Evidence / answer 缓存

key：`(root, version, normalize(query))`。

- 只对**完全相同**的 query 生效；
- TTL 5–10 分钟；
- 返回内容同时记录来源文件路径；
- 风险：LLM 输出非确定性、不同 conflict 需要不同证据口径；**首期不做**，等 index/agent 缓存稳定后再评估。

---

## 10. 文件清单、签名与接线锚点

### 10.1 新增

| 文件 | 内容 |
|---|---|
| `alphabee/financial_report/report_cache.py` | `ReportIndex` / `report_version` / `ReportCacheConfig` / `get_report_index` / `aget_report_agent` / `invalidate_report_caches` / `clear_report_caches` / `suggest_paths` / `ReportFilesystemBackend`（或放在 `fetch_deepagents.py`） |
| `tests/financial_report/test_report_cache.py` | key、version、TTL、LRU、single-flight、失效 |
| `tests/financial_report/test_report_path_guard.py` | 不存在的路径 → 相似路径提示；并发安全 |

### 10.2 改动

| 文件 | 改动 |
|---|---|
| `alphabee/financial_report/fetch.py` | `build_file_tree()` 保留兼容；新增 `build_compact_tree(index)`；`build_fetch_prompt(..., index: ReportIndex \| None = None)`，优先使用 index 的压缩树/完整路径 |
| `alphabee/financial_report/fetch_deepagents.py` | `create_report_fetch_agent(report_dir, *, index=None)`；backend 换成 `ReportFilesystemBackend`；新增 `aget_report_agent(root)` 缓存入口；保留同步工厂供测试/旧调用 |
| `alphabee/tools/financial_report.py` | `query_financial_report` 改为 `agent = await aget_report_agent(root_path)`；失败回退 `create_report_fetch_agent`；`_REPORT_LOCATE_CACHE` 失效入口 |
| `alphabee/financial_report/pipeline.py` | `answer_report_question` 复用 `aget_report_agent`，保持现有返回值语义 |
| `alphabee/financial_report/report_parser.py` | `write_markdown_report_folder()` 返回前调用 `invalidate_report_caches(target)`（可选，见 §6.2） |
| `alphabee/config/__init__.py` / `config.yaml(.example)` | 新增 `financial_report.cache` 段（可选，P2 再接） |
| `docs/implementation/README.md` | 登记本文档 |

### 10.3 关键签名

```python
# report_cache.py
def build_report_index(root: str | Path) -> ReportIndex: ...
def report_version(root: str | Path) -> str: ...
def get_report_index(root: str | Path) -> ReportIndex: ...
async def aget_report_agent(root: str | Path) -> CompiledStateGraph: ...
def invalidate_report_caches(root: str | Path | None = None) -> None: ...
def clear_report_caches() -> None: ...
def suggest_paths(index: ReportIndex, requested: str, *, n: int = 5) -> list[str]: ...

# fetch_deepagents.py
def create_report_fetch_agent(
    report_dir: str | Path,
    *,
    index: ReportIndex | None = None,
) -> CompiledStateGraph: ...

# fetch.py
def build_fetch_prompt(
    report_name: str,
    query: str,
    report_dir: Path | None = None,
    *,
    index: ReportIndex | None = None,
) -> str: ...
```

### 10.4 接线点（既有代码锚点）

| 锚点 | 改动 |
|---|---|
| `tools/financial_report.py:496-499` | 保留定位缓存 |
| `tools/financial_report.py:502` | `create_report_fetch_agent(root_path)` → `await aget_report_agent(root_path)` |
| `fetch_deepagents.py:45` | `build_fetch_prompt(...)` 传入 index |
| `fetch_deepagents.py:46` | `FilesystemBackend` → `ReportFilesystemBackend` |
| `fetch_deepagents.py:47-59` | `create_deep_agent` 不变，只换 backend/ prompt |
| `pipeline.py:337` | 同步工厂 → 异步缓存入口 |
| `report_parser.py:346-363` | manifest 已是版本来源；可选加失效钩子 |

---

## 11. 测试矩阵

| 文件 | 用例 | 断言 |
|---|---|---|
| `tests/financial_report/test_report_cache.py` | index 构建 | `rel_files` 排序、posix、无前导 `/`；`total_files` 与磁盘一致 |
| 同上 | version 稳定性 | 同一目录连续两次 `report_version` 相同 |
| 同上 | manifest 变化 | 重写 manifest（改 mtime/size）→ version 变化 → 新 cache key |
| 同上 | TTL 过期 | monkeypatch `time.monotonic` → 过期后重建 |
| 同上 | LRU 上限 | 写入 N+1 个 key → entries 不超过 max，最旧被淘汰 |
| 同上 | single-flight | 10 个并发 `aget_report_agent` 同 key → builder 只调用 1 次 |
| 同上 | 并发隔离 | 同 agent 20 并发不同 query（fake model）→ 每个结果对应自己的 query |
| 同上 | 失败回退 | builder 抛异常 → 回退 `create_report_fetch_agent`，查询仍可用 |
| `tests/financial_report/test_report_path_guard.py` | 精确路径 | 存在路径 → 正常读取 |
| 同上 | 拼错文件名 | 不存在 → error 中包含 top-5 候选，且包含真实文件 |
| 同上 | 漏 `(1)_` 前缀 | 能用 basename 匹配到 `(1)_...md` |
| 同上 | 目录对、文件名错 | 候选优先同目录 |
| 同上 | GP subagent 覆盖 | 通过 backend 调用（模拟 subagent）同样返回 hints |
| `tests/financial_report/test_fetch_deepagents.py` | 工厂兼容 | `create_report_fetch_agent(dir)` 旧签名仍可用 |
| 同上 | 缺目录 | 仍然抛 `FileNotFoundError`，不被缓存吞掉 |
| `tests/tools/test_financial_report.py` | 定位缓存失效 | 新增报告后 `invalidate_report_caches` 生效 |
| `tests/financial_report/test_pipeline.py` | 问答接线 | `answer_report_question` 复用缓存，结果语义不变 |

**全量回归**：`poetry run pytest tests -q -m "not integration"`。

**并发压测（手动/集成分支）**：

```python
async def test_same_report_parallel():
    results = await asyncio.gather(*[
        query_financial_report(FinancialReportRequest(
            company_code="601899", year=2026, report_type="semiannual",
            query=f"问题 {i}",
        ))
        for i in range(10)
    ])
    assert len(results) == 10
    # builder 计数 == 1；无串话；无异常
```

---

## 12. 分阶段落地

按“正确性优先、性能其次”的顺序：

### P0：index 缓存（低风险，基础）

- 新增 `report_cache.py`、`ReportIndex`、version、TTL/LRU；
- `build_fetch_prompt` 消费 index 的压缩树；
- 行为差异：prompt 文本变化，但工具/模型/输出契约不变；
- 验收：同 root 连续查询，目录扫描只发生 1 次；golden 报告回答无回归。

### P1：backend 路径守卫（收益最大）

- `ReportFilesystemBackend` + `suggest_paths`；
- 验收：日志 `read_file` error rate 从 6.8% 降到 <1%；
- 单独 PR，便于回滚。

### P2：agent 缓存（性能）

- `aget_report_agent` + 并发压测 + 降级开关；
- 验收：同 key builder 只调用 1 次；热态查询不再 `create_deep_agent`；
- 若并发不安全 → 只保留 index，agent 每次新建。

### P3（可选）：确定性预选 / evidence 缓存

- 先离线评测关键词命中率，再决定是否默认开启。

### PR 切分建议

| PR | 内容 | 可独立回滚 |
|---|---|---|
| PR-A | `report_cache.py` + index 缓存 + 压缩 prompt + 单测 | 是（关 index 开关） |
| PR-B | backend 路径守卫 + 候选提示 + 指标 | 是（换回 `FilesystemBackend`） |
| PR-C | agent 缓存 + single-flight + 并发压测 + 配置 | 是（关 agent 开关） |
| PR-D（可选） | 候选预选 / evidence 缓存 | 是 |

---

## 13. 验收标准

### 13.1 正确性

1. 同一报告的不同 query 结果与改造前语义一致（golden 用例）；
2. 不同报告之间不串 index/agent（key 隔离）；
3. 报告重写后 1 次 `stat` 内换新 version，不读旧 index；
4. 缓存构建异常时查询仍可用（fail-open）；
5. `read_file` 拼错路径时返回真实候选，模型下一次可直接读对。

### 13.2 性能

1. 同报告连续 20 次查询：index 构建 1 次、agent 构建 1 次；
2. 10 并发同报告：builder 1 次、无异常、无串话；
3. 单次查询构建耗时（不含 LLM）下降 ≥80%（热态）；
4. 日志 `read_file` error rate <1%（P1 后）。

### 13.3 观测

- `report_cache` 日志包含 `kind/result/root/version/duration_ms/entries`；
- 能区分 index hit、agent hit、build、evict、fallback；
- 不记录完整报告内容，不泄露 API key。

---

## 14. 风险与回滚

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| compiled graph 并发复用不安全 | 中 | 结果串话 | P2 前压测；不通过则关闭 agent 缓存 |
| 缓存陈旧（手工改 md） | 低 | 回答旧内容 | TTL 30 min + manifest version；可选显式失效 |
| 内存增长 | 中 | 进程占用上升 | max_entries + LRU；index 64 / agent 16 |
| 全局缓存污染单测 | 高 | 单测互扰 | `clear_report_caches()` + autouse fixture |
| backend 守卫误伤新文件 | 低 | 临时读不到 | 仅提示不拦截；严格 allowlist 默认关 |
| 代理环境/路径分隔符差异 | 低 | 匹配失败 | `posixpath.normpath` + 去前导 `/` |
| 多进程各自建缓存 | 中 | 内存重复 | 可接受；后续可落磁盘索引 |

**回滚方式**

- index 缓存：`cache.index_enabled=false`（无 agent 缓存时等价旧行为）；
- agent 缓存：`cache.agent_enabled=false`，保留 index；
- backend 守卫：换回 `FilesystemBackend`，保留 index/agent；
- 所有缓存模块删除不影响现有调用（fail-open 回退）。

---

## 15. 开放问题

1. **compiled graph 并发复用**：当前 LangGraph/deepagents 版本是否保证同一 graph 多 `ainvoke` 并发安全？需要 P2 压测给出结论。
2. **GP subagent 是否应保留**：fetch agent 的 general-purpose subagent 会扩大工具面；本设计的 backend 守卫可覆盖它，但是否直接禁用（减少无意义探索）另评估。
3. **TTL 默认值**：30 分钟是否合适？取决于报告解析频率与运行时长。
4. **严格 allowlist**：是否要对 `read_file` 强制只允许 index 内路径？需要评估“解析过程中新文件写入”的竞态。
5. **候选预选的中文分词**：是否需要引入轻量分词，还是只用引号短语 + 文件名匹配。
6. **跨进程缓存**：web 多 worker 部署时是否需要磁盘版 index（json + mtime 校验）。

---

## 16. 决策记录

| 编号 | 决策 | 理由 |
|---|---|---|
| D1 | artifact 缓存以 canonical root 为一级 key，`(company, year, type)` 只用于定位 | 同一报告多种写法；直接传目录的 pipeline 入口也需要复用 |
| D2 | 缓存 compiled graph，不缓存 conversation | 禁止跨 query 复用消息历史；避免上下文污染 |
| D3 | 路径守卫放在 `FilesystemBackend` 子类 | `middleware=` 不覆盖自动添加的 GP subagent；backend 被所有子 agent 共享 |
| D4 | 版本用 manifest `stat`，不解析内容 | 277/277 有 manifest；一次 stat 足够便宜 |
| D5 | 首期不做 answer/evidence 缓存 | 语义去重难、LLM 非确定性、冲突验证需要不同证据口径 |
| D6 | index 先行、agent 后置 | 正确性收益主要来自 index + 路径守卫；agent 缓存是性能优化且并发风险更高 |

---

## 17. 文档登记要求

1. 本文登记到 `docs/implementation/README.md` 的 `financial_report/` 行；
2. 新增/修改文档后运行 `python scripts/gen_docs_index.py` 更新 `docs/_sidebar.md` 与 `docs/README.md`（生成文件勿手改）；
3. 若实现涉及 `config.yaml` 默认值变更，按项目变更登记惯例在 `docs/roadmap/ROADMAP.md` 记录一行；
4. 实现时若改动 prompt/工具返回契约，同步检查 `tests/apps`、`tests/financial_report`、`tests/tools` 的 golden 断言。
