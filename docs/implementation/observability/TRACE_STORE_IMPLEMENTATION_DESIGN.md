# Trace Store 实施设计（Core / Profile 分层，P0 编码设计）

> **状态**：实施设计稿（2026-11-16，未实施）
> **来源设计**：`docs/design/TRACE_STORE_DESIGN.md`（产品设计 v0.2，含 §4 非侵入约束、§5 Core、§6 Profile、§7 适配、§12 验收）
> **定位**：回答"怎么改代码"——文件清单、类/函数签名、接线锚点、存储与序列化、兼容与 fail-open、测试矩阵、PR 切分与回滚。
> **范围**：只覆盖 P0（Trace 身份/生命周期、span 树、L0/L1 全量、L2 采样、语义事件、AlphaBee Profile v1、Q1–Q5 查询、完整性、fail-open、保留策略、非侵入边界/未知安全/工具目录/多入口一致）。P1/P2（血缘查询、差分、多 profile、导出）在 §16 列出接口预留，不在本期实现。
> **纪律**：本设计不改变任何 pipeline 行为；所有采集 fail-open；领域模块不 import 本包（依赖方向见 §12）。

---

## 0. 一页摘要

- **新增包**：`alphabee/observability/`（trace core + profile + adapters + query），约 20 个模块。
- **存储**：每 trace 一个 append-only `trace.jsonl` + 内容寻址 `blobs/<sha256>` + 可重建的 SQLite 索引（`data/traces/index.db`）。
- **写入方**：`TraceRecorder`（contextvars + 线程锁 + 缓冲 flush + 全局 fail-open）。
- **采集点**：① 图节点包装器（覆盖全部 17 个节点）；② LangChain 回调（LLM/工具/MCP 自动覆盖）；③ 节点出口的语义事件 diff（artifact/issue/decision，业务零改动）；④ 数据获取 trace_id 注入；⑤ CLI/Web/入口统一生命周期。
- **查询**：`TraceReader`（Q1–Q5 + 完整性），面向 Scout/人/回归工具。
- **默认配置**：`observability.trace.enabled=true`、`level=L1`、`l2_sample_rate=0.1`、本地无网络、无需新基础设施。
- **行为不变**：关闭或写入失败时与现状逐字节一致；TaskRecord 只增 `trace_id` 一个 additive 字段。

---

## 1. 设计映射（产品要求 → 实现）

| 设计文档 | 实现位置 |
|---|---|
| FR-Core-01 trace 身份/生命周期 | `trace/recorder.py::TraceRecorder`、`adapters/graph.py::trace_run` |
| FR-Core-02 span 模型 | `trace/schema.py::SpanRecord`、`trace/recorder.py::span` |
| FR-Core-03 内容对象与 L0–L3 | `trace/content.py::ContentStore`、`trace/policy.py` |
| FR-Core-04 血缘原语（P0 只落表，查询 P1） | `trace/schema.py::LineageRecord`、`trace/index.py` |
| FR-Core-05 语义事件 | `trace/schema.py::EventRecord`、`adapters/semantic.py` |
| FR-Core-06 查询 Q1–Q5/Q12（Q6 预览） | `trace/reader.py::TraceReader` |
| FR-Core-08 保留/采样/容量 | `trace/policy.py`、`trace/gc.py`（P0 仅策略 + 手动 GC 命令） |
| FR-Core-09 访问控制（本地最小） | `trace/policy.py::ContentView`、`reader.get_content(view=...)` |
| FR-Core-10 自观测 | `trace/recorder.py::InternalStats`、manifest 内 `internal_errors` |
| FR-Core-11 非侵入边界 | `adapters/graph.py`、`adapters/callbacks.py`（业务零改动） |
| FR-Core-12 未知安全 | `profile/registry.py::validate_or_quarantine` |
| FR-Core-13 派生血缘（P1） | 预留 `adapters/semantic.py::derive_lineage` 接口 |
| FR-Core-14 预算与开关 | `trace/policy.py::TracePolicy.from_settings` |
| FR-Core-15 依赖方向 | §12 约束 + 依赖测试 |
| FR-Adapter-04 工具/代理目录（P1 基础） | `profile/alphabee_v1.py::TOOL_CATALOGUE` |
| FR-Adapter-05 覆盖审计（P1） | `trace/integrity.py::coverage_report` 预留 |
| FR-Adapter-06 多入口一致 | `adapters/graph.py::trace_run(entry=...)`，CLI/Web 共用 |

---

## 2. 模块与文件布局

```text
alphabee/observability/
  __init__.py                 # 导出 TraceRecorder / TraceReader / trace_run / with_trace_node
  trace/
    __init__.py
    schema.py                 # 数据模型：TraceMeta/SpanRecord/EventRecord/LineageRecord/Manifest/枚举
    ids.py                    # trace/span/event/edge id 生成与内容哈希工具
    content.py                # ContentStore：CAS blob、去重、原子写、读取、GC
    policy.py                 # TracePolicy：级别/采样/上限/脱敏/保留；从 Settings 解析
    redact.py                 # 脱敏规则（密钥/PII/代理凭据）
    writer.py                 # TraceWriter：append-only JSONL、缓冲、flush、fail-open
    recorder.py               # TraceRecorder：contextvars、span 上下文、事件/血缘/语义记录、内部统计
    index.py                  # TraceIndex：SQLite 建表、批量 upsert、查询基础设施、rebuild
    reader.py                 # TraceReader：Q1–Q5/Q12、内容读取、分页、错误语义
    diff.py                   # （P1）trace 差分
    integrity.py              # 完整性分数与覆盖审计（P0 只做分数）
    gc.py                     # （P0 手动命令）按保留期清理内容/索引
  profile/
    __init__.py
    base.py                   # ProfileDefinition / EntityType / RelationType / EventType / QueryTemplate
    alphabee_v1.py            # AlphaBee Profile v1 声明（实体/关系/事件/字段/保留覆盖/工具目录）
    registry.py               # register_profile / validate_or_quarantine / 兼容矩阵
  adapters/
    __init__.py
    graph.py                  # trace_run（入口生命周期）、with_trace_node（节点 span）
    callbacks.py              # TraceCallbackHandler（LLM/工具/agent/chain span）
    semantic.py               # 节点出口 diff → artifact/issue/decision 事件 + 血缘（P0 基础）
    data_fetch.py            # 把当前 trace_id/task_id/session_id 注入 data_fetch.record_failure
    mcp.py                    # （P1）MCP 服务器级 span 与 job task_id 关联
  query/
    __init__.py
    cli.py                    # （P1）trace_query 命令行封装；P0 仅提供函数
tests/observability/
  test_schema.py test_ids.py test_content.py test_policy.py test_redact.py
  test_writer.py test_recorder.py test_index.py test_reader.py test_integrity.py
  adapters/test_graph.py adapters/test_callbacks.py adapters/test_semantic.py
  adapters/test_data_fetch.py test_task_record_trace.py test_dependency_rule.py
scripts/
  trace_query.py              # （P1）查询/完整性/GC 的命令行入口；P0 可只读 JSONL
```

**依赖方向（硬约束）**

```text
alphabee.observability.trace.*      → 标准库 + pydantic（config）+ SQLite
alphabee.observability.profile.*    → trace.schema
alphabee.observability.adapters.*   → trace.* + profile.* + 具体框架（langchain/langgraph）
alphabee/agents/**、alphabee/orchestrator/**、alphabee/apps/** → ❌ 不得 import observability
（唯一例外：apps/cli/streaming.py、apps/web/stream.py、utils/llm.py 的 callbacks 参数、data_fetch 的 id 注入）
```

> 说明：入口/回调属于"适配器层"边缘，允许 import `observability.adapters`；业务节点函数、agents、midterm、reports 一律不 import。

---

## 3. 数据模型（`trace/schema.py`）

建议使用 `@dataclass(slots=True)`（写入热路径、无校验开销），配 `to_dict()`；枚举用 `enum.StrEnum`。

```python
TRACE_SCHEMA_VERSION = 1

class RecordType(StrEnum):
    TRACE = "trace"; SPAN = "span"; EVENT = "event"; LINEAGE = "lineage"

class SpanType(StrEnum):
    RUN = "run"; NODE = "node"; AGENT = "agent"; LLM = "llm"; TOOL = "tool"
    MCP = "mcp"; API = "api"; RETRIEVAL = "retrieval"; ARTIFACT = "artifact"
    DECISION = "decision"; ISSUE = "issue"; GUARD = "guard"; CACHE = "cache"
    BUDGET = "budget"; CUSTOM = "custom"

class SpanStatus(StrEnum):
    OK = "ok"; ERROR = "error"; TIMEOUT = "timeout"; CANCELLED = "cancelled"
    SKIPPED = "skipped"; PARTIAL = "partial"

class EventType(StrEnum):
    RUN_START = "run.start"; RUN_END = "run.end"
    NODE_START = "node.start"; NODE_END = "node.end"
    AGENT_START = "agent.start"; AGENT_END = "agent.end"
    LLM_ERROR = "llm.error"; TOOL_ERROR = "tool.error"
    ARTIFACT_PRODUCED = "artifact.produced"; ARTIFACT_CONSUMED = "artifact.consumed"
    ISSUE_EMITTED = "issue.emitted"; ISSUE_RESOLVED = "issue.resolved"
    DECISION_EMITTED = "decision.emitted"; CLAIM_EMITTED = "claim.emitted"
    FALLBACK = "fallback"; DEGRADED = "degraded"
    CACHE_HIT = "cache.hit"; CACHE_MISS = "cache.miss"
    EMPTY_RESULT = "empty_result"; RETRY = "retry"
    BUDGET_EXCEEDED = "budget.exceeded"; GUARD_BLOCK = "guard.block"
    LINEAGE_EDGE = "lineage.edge"; TASK_LINK = "task.link"
    INTERNAL_ERROR = "internal_error"

class RelationType(StrEnum):
    GENERATED_BY = "generated_by"; USED = "used"; DERIVED_FROM = "derived_from"
    VALIDATED_BY = "validated_by"; REFUTED_BY = "refuted_by"
    SUPERSEDES = "supersedes"; ATTRIBUTED_TO = "attributed_to"
    INFORMED_BY = "informed_by"; PART_OF = "part_of"

@dataclass(slots=True)
class ContentRef:
    hash: str                 # "sha256:<64hex>"
    media_type: str           # "application/json" | "text/plain" | "text/markdown" | ...
    size: int
    preview: str = ""         # ≤ preview_chars，列表视图直接可用
    redacted: bool = False
    tier: str = "L1"          # L0/L1/L2/L3
    expired: bool = False

@dataclass(slots=True)
class TraceMeta:
    schema_version: int
    trace_id: str
    entry: str                # cli | web | script | tracking
    profile_id: str; profile_version: int
    started_at: str           # ISO8601 + tz
    ended_at: str | None
    status: str               # running/succeeded/failed/partial/cancelled/aborted
    run_id: str = ""; task_id: str = ""; session_id: str = ""
    symbol: str = ""; as_of: str = ""
    code_sha: str = ""; config_hash: str = ""
    model: str = ""; prompt_versions: dict[str, str] = field(default_factory=dict)
    data_snapshot: str = ""   # 预留：快照/回放标识
    internal_errors: int = 0

@dataclass(slots=True)
class SpanRecord:
    record_type: str          # "span"
    span_id: str; trace_id: str; parent_span_id: str | None
    type: str; name: str
    node: str = ""; agent: str = ""
    started_at: str = ""; ended_at: str = ""; duration_ms: float | None = None
    status: str = "ok"; attempt: int = 1; retry_of: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)  # 允许清单内的字段
    content_refs: dict[str, str] = field(default_factory=dict)  # logical_name -> hash
    entity_refs: list[str] = field(default_factory=list)        # artifact/issue/decision id
    error: dict[str, Any] = field(default_factory=dict)         # type/message/retryable

@dataclass(slots=True)
class EventRecord:
    record_type: str          # "event"
    event_id: str; trace_id: str; span_id: str = ""; parent_span_id: str | None = None
    type: str; occurred_at: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    refs: list[str] = field(default_factory=list)
    content_ref: str = ""

@dataclass(slots=True)
class LineageRecord:
    record_type: str          # "lineage"
    edge_id: str; trace_id: str
    subject_kind: str; subject_id: str      # entity type + id（Profile 定义）
    relation: str
    object_kind: str; object_id: str
    asserted_by_span: str = ""
    evidence_ref: str = ""                   # 内容哈希
    origin: str = "asserted"                 # asserted | derived
    profile_version: int = 1
    at: str = ""

@dataclass(slots=True)
class Manifest:
    meta: TraceMeta
    counts: dict[str, int]                   # spans/events/lineage/blobs
    completeness: dict[str, float]           # node/llm/tool/event/lineage
    internal_errors: int = 0
    generated_at: str = ""
```

**序列化契约**

- JSONL 每行一个字典，必含 `record_type`、`schema_version`；
- 未知字段：读取时忽略（向前兼容）；写入只写白名单字段；
- 时间：ISO8601 带时区；时长用 monotonic 计算后落 `duration_ms`；
- 金额/数值不做单位转换（原样 + 在 attributes 中标注口径）。

---

## 4. ID、哈希与指纹（`trace/ids.py`）

```python
def new_trace_id() -> str:      return f"trc-{uuid.uuid4().hex[:12]}"
def new_span_id() -> str:       return f"spn-{uuid.uuid4().hex[:12]}"
def new_edge_id() -> str:       return f"edg-{uuid.uuid4().hex[:12]}"
def content_hash(data: bytes) -> str: return "sha256:" + hashlib.sha256(data).hexdigest()
def event_id(trace_id, span_id, type_, seq) -> str:
    return "evt-" + hashlib.sha1(f"{trace_id}|{span_id}|{type_}|{seq}".encode()).hexdigest()[:16]
def fingerprint(value: Any) -> str:
    """对任意可序列化值做稳定指纹（排序键、default=str），用于输入/输出指纹。"""
```

规则：

- `span_id` 由回调用 `run_id` 映射（`_run_span: dict[UUID, str]`），保证父子关系；
- 事件 id 由 `trace+span+type+seq` 派生 → 重复提交幂等（索引 `INSERT OR IGNORE`）；
- 输入/输出指纹使用 `fingerprint({k: update[k] for k in selected_keys})`，**不**对 messages/大对象直接 JSON（用投影 + 截断后再指纹）。

---

## 5. 内容寻址（`trace/content.py`）

```python
class ContentStore:
    def __init__(self, root: Path, *, max_content_bytes: int, preview_chars: int): ...

    def put(self, data: bytes, *, media_type: str, tier: str, redacted: bool) -> ContentRef:
        """写入 CAS blob（存在即去重）；返回 ContentRef。失败抛 OSError，由调用方 fail-open。"""

    def get(self, ref: str | ContentRef, *, view: ContentView = ContentView.PREVIEW) -> bytes | None:
        """view=preview 只返回预览（来自索引）；full 读取 blob；无权限/过期返回 None + 状态。"""

    def exists(self, digest: str) -> bool: ...
    def gc(self, *, older_than_days: int, tiers: set[str]) -> int: ...
```

布局与写入：

```text
data/traces/
  <trace_id>/
    trace.jsonl
    manifest.json
  blobs/
    ab/abcdef...        # 前两位分片
  index.db
```

- 原子写：`tmp` 文件 + `os.replace`；路径 `blobs/<hash[:2]>/<hash>`；
- 去重：若目标存在直接返回 ref（不重写）；
- 上限：超过 `max_content_bytes` 截断并标记 `truncated=True`（attributes 记录原大小）；
- 脱敏：**写入前**由 `redact.redact_bytes()` 处理，`redacted=True` 写入 ref；
- 预览：截取前 `preview_chars`（默认 512），列表查询直接返回，不下钻 blob；
- GC：按 blob 的 `created_at` 与引用情况（P0 不做引用计数，按保留期扫描 + 索引存在性判断）。

**分级策略（`trace/policy.py`）**

| 级别 | 存什么 | 默认 |
|---|---|---|
| L0 | 只存引用（id/hash），不写 blob | 对超大/无关内容 |
| L1 | 元数据 + 预览 + hash | **全量默认** |
| L2 | 全文（prompt/response/工具结果/结构化输出），按采样 | 采样/异常/显式 |
| L3 | 原始快照（原始 API/文档/OCR 产物） | 失败/审计（P2） |

---

## 6. 写入路径（`writer.py` + `recorder.py` + `policy.py`）

### 6.1 TracePolicy

```python
@dataclass(frozen=True)
class TracePolicy:
    enabled: bool = True
    level: str = "L1"                 # L0/L1/L2/L3 基准
    l2_sample_rate: float = 0.1
    l3_on_error: bool = True
    max_span_records: int = 20_000
    max_event_records: int = 20_000
    max_content_bytes: int = 256 * 1024
    max_trace_bytes: int = 200 * 1024 * 1024
    preview_chars: int = 512
    late_window_seconds: int = 300
    retention_days: dict[str, int] = {"L1": 90, "L2": 30, "L3": 7}
    storage_dir: str = "data/traces"
    profile_id: str = "alphabee"
    profile_version: int = 1

    @classmethod
    def from_settings(cls, settings: Any) -> "TracePolicy": ...
```

- 采样在 **trace 创建时**决定一次（`l2_enabled` 布尔），trace 内一致；
- 达到 `max_*` 后降级（不再写 span/事件，或 L2→L1），置 `truncated/over_budget` 标记并计数；
- 关闭时返回 `NoopRecorder`（零 IO）。

### 6.2 TraceWriter

```python
class TraceWriter:
    def __init__(self, trace_dir: Path, *, policy: TracePolicy): ...
    def append(self, record: dict[str, Any]) -> None:   # 单行 JSON（ensure_ascii=False）+ "\n"
    def flush(self) -> None: ...
    def close(self) -> None: ...
    @property
    def bytes_written(self) -> int: ...
```

- 打开时 `mkdir` + `O_APPEND`；父目录创建失败 → 抛给 Recorder 的 fail-open；
- 线程锁 + 每 64KB 或 1s flush（P0 可每次 append 后 flush，若压测显示开销再改）；
- 序列化失败（不可 JSON 化）：`default=str` 兜底 + 记 `internal_error`；
- 超 `max_trace_bytes`：写一条 `internal_error` 事件并停写（manifest 记录 `truncated`）。

### 6.3 TraceRecorder

```python
class TraceRecorder:
    trace_id: str
    def __init__(self, *, entry: str, policy: TracePolicy, content: ContentStore,
                 writer: TraceWriter, index: TraceIndex, meta: dict[str, Any]): ...

    # 生命周期
    def bind_run(self, run_id: str, *, symbol: str = "", as_of: str = "") -> None: ...
    def link_task(self, task_id: str, *, symbol: str = "") -> None: ...   # late event + manifest
    def close(self, status: str = "succeeded") -> None: ...               # 写 manifest、flush

    # span
    @contextmanager
    def span(self, type_: str, name: str, *, node: str = "", agent: str = "",
             attributes: dict | None = None, attempt: int = 1,
             retry_of: str = "") -> Iterator[SpanHandle]: ...
    def current_span_id(self) -> str | None: ...

    # 事件 / 语义 / 血缘
    def event(self, type_: str, *, attributes: dict | None = None,
              refs: list[str] | None = None, content: tuple[bytes, str] | None = None) -> None: ...
    def artifact_produced(self, artifact, *, node_span: str) -> None: ...
    def issue_emitted(self, issue, *, node_span: str) -> None: ...
    def decision_emitted(self, decision, *, node_span: str) -> None: ...
    def lineage(self, *, subject_kind, subject_id, relation, object_kind, object_id,
                evidence: bytes | None = None, origin="asserted") -> None: ...

    # 内容
    def put_content(self, data: bytes, *, media_type: str,
                    logical_name: str = "", tier: str | None = None) -> ContentRef | None: ...

    # 诊断
    def stats(self) -> dict[str, int]: ...   # internal_errors/dropped/truncated
```

**上下文与幂等**

```python
_current_trace: ContextVar[TraceRecorder | None]
_current_span: ContextVar[SpanHandle | None]
```

- 所有公共方法包在 `_safe()` 中：捕获一切异常 → `stats["internal_errors"] += 1` + 记一条内部事件（若写不进去则只计数），**绝不向上抛**；
- `event_id` 派生稳定；索引 upsert 用 `INSERT OR IGNORE`；
- `link_task` 在 `late_window_seconds` 内允许追加（trace 已 closed 则 reopen append 一行 + manifest 更新 + 索引更新）。

### 6.4 语义事件（`adapters/semantic.py`）

节点出口 diff（无需业务改动）：

```python
def emit_update_records(rec: TraceRecorder, node_span: SpanHandle,
                        before: OrchestratorState, update: dict[str, Any]) -> None:
    """对比 update["artifacts"/"issues"/"decisions"] 与 before 中已有 id，
    对新出现的实体发 artifact.produced / issue.emitted / decision.emitted，
    并写 generated_by 血缘边（artifact/issue/decision → node span）。
    run 出现在 update 中时调用 rec.bind_run(...)。
    """
```

- `artifactId`/`issue.id`/`decision.id` 作为 entity id；
- 新实体检测按 id 集合差（O(n)）；
- `issue.related_step` 与 `decision.based_on`（若存在）延迟到 P1 派生 `derived_from`；
- `finalize_message` 节点：额外记录 `final_report` 内容引用（L2 采样时才写全文）。

---

## 7. 索引（`trace/index.py`）

SQLite（WAL），路径 `data/traces/index.db`；**索引可重建**（`rebuild_from_traces()`），不承载唯一事实。

```sql
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS traces (
  trace_id TEXT PRIMARY KEY, run_id TEXT, task_id TEXT, session_id TEXT,
  symbol TEXT, as_of TEXT, status TEXT, entry TEXT,
  started_at TEXT, ended_at TEXT,
  profile_id TEXT, profile_version INTEGER, schema_version INTEGER,
  code_sha TEXT, config_hash TEXT,
  spans INTEGER DEFAULT 0, events INTEGER DEFAULT 0, lineage INTEGER DEFAULT 0,
  bytes INTEGER DEFAULT 0, internal_errors INTEGER DEFAULT 0,
  manifest_json TEXT
);
CREATE TABLE IF NOT EXISTS spans (
  span_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_span_id TEXT,
  type TEXT, name TEXT, node TEXT, agent TEXT, status TEXT,
  started_at TEXT, ended_at TEXT, duration_ms REAL,
  attributes_json TEXT, content_refs_json TEXT, entity_refs_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY, trace_id TEXT, span_id TEXT, type TEXT,
  occurred_at TEXT, attributes_json TEXT, refs_json TEXT, content_ref TEXT
);
CREATE TABLE IF NOT EXISTS lineage (
  edge_id TEXT PRIMARY KEY, trace_id TEXT, subject_kind TEXT, subject_id TEXT,
  relation TEXT, object_kind TEXT, object_id TEXT,
  asserted_by_span TEXT, evidence_ref TEXT, origin TEXT, at TEXT
);
CREATE TABLE IF NOT EXISTS blobs (
  content_hash TEXT PRIMARY KEY, media_type TEXT, size INTEGER,
  created_at TEXT, redacted INTEGER, tier TEXT, truncated INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_traces_symbol_date ON traces(symbol, started_at);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id, type);
CREATE INDEX IF NOT EXISTS idx_events_trace_type ON events(trace_id, type);
CREATE INDEX IF NOT EXISTS idx_lineage_subject ON lineage(subject_kind, subject_id);
```

- 写入：span/event/lineage 完成时同步 upsert（小事务批量）；失败只计数，不抛；
- 查询：`TraceReader` 直接 SQL；`rebuild` 命令扫描 `trace.jsonl` 全量重建；
- 一致性：单 trace 内写后读可见（同一线程/进程）；跨 trace 允许秒级延迟（`index_lag_ms` 记在 manifest）。

---

## 8. 查询（`trace/reader.py`）

```python
class TraceReader:
    def __init__(self, *, storage_dir: Path, index: TraceIndex, policy: TracePolicy): ...

    # Q1
    def find_traces(self, *, symbol: str | None = None, date: str | None = None,
                    task_id: str | None = None, run_id: str | None = None,
                    status: str | None = None, limit: int = 50,
                    cursor: str | None = None) -> TraceList: ...
    # Q2
    def get_manifest(self, trace_id: str) -> Manifest: ...
    # Q3
    def get_span_tree(self, trace_id: str, *, types: set[str] | None = None,
                      max_depth: int | None = None, include_attributes: bool = False) -> list[SpanNode]: ...
    # Q4
    def get_span(self, span_id: str) -> SpanDetail: ...
    # Q5
    def get_events(self, trace_id: str, *, types: set[str] | None = None,
                   span_id: str | None = None, limit: int = 500) -> list[EventRecord]: ...
    # Q6（P0 仅预览/受限全文）
    def get_content(self, content_hash: str, *, view: ContentView = ContentView.PREVIEW) -> ContentResult: ...
    # Q12
    def get_completeness(self, trace_id: str) -> CompletenessReport: ...
```

- 返回值均为 dataclass（`TraceList/Summary/SpanNode/SpanDetail/ContentResult/CompletenessReport`）；
- 分页：`limit/offset` 或 `cursor=last_id`（P0 用 offset）；
- 错误语义：`TraceNotFound` / `IndexNotReady` / `ContentExpired` / `ContentDenied` —— 区分"没有"与"读不到"；
- 访问视图：`ContentView.PREVIEW`（默认，来自索引）、`FULL`（读 blob，受 policy 与 redaction 标记约束）、`REDACTED`（返回脱敏副本标记）。
- Q7/Q8/Q9/Q10/Q11 在 P1：接口签名先定（见设计文档 §5 FR-Core-06），实现放后续 PR。

---

## 9. Profile（`profile/`）

```python
@dataclass(frozen=True)
class EntityType:
    name: str; id_rule: str; required: tuple[str, ...]; optional: tuple[str, ...] = ()

@dataclass(frozen=True)
class RelationType:
    name: str; subject_types: tuple[str, ...]; object_types: tuple[str, ...]

@dataclass(frozen=True)
class ProfileDefinition:
    id: str; version: int; entities: dict[str, EntityType]
    relations: dict[str, RelationType]; events: dict[str, tuple[str, ...]]
    attributes: dict[str, tuple[str, ...]]
    query_templates: dict[str, str]
    retention_overrides: dict[str, int]
    export_mappings: dict[str, str]
```

`alphabee_v1.py` 声明（P0 仅声明 + 校验，不要求业务改代码）：

- 实体：`fact / observation / artifact / decision / issue / claim / evaluation`；
- 关系：`generated_by / used / derived_from / validated_by / refuted_by / supersedes`；
- 领域字段允许清单：`symbol / period / as_of / caliber / source / revised / derived / deviation_class`；
- 保留覆盖：`claim/artifact` 内容更长（L2 90 天）、检索原文更短（30 天）；
- 工具目录 `TOOL_CATALOGUE`：内置 facts 工具、web_search、eastmoney、tushare_query、MCP 工具；未知工具走通用 span + 标记。

```python
def register_profile(defn: ProfileDefinition) -> None: ...
def validate_or_quarantine(kind: str, payload: dict[str, Any], *, profile: ProfileDefinition) -> tuple[bool, str]:
    """未知实体/关系/事件 → False + reason；调用方记录到隔离桶（事件 attributes.quarantined=true），不阻断。"""
```

---

## 10. 适配器与接线点（逐文件）

> 行号为实现时快照，落地以符号名+文件为准。

### 10.1 入口生命周期（CLI/Web 共用）

`alphabee/observability/adapters/graph.py`：

```python
@contextmanager
def trace_run(*, entry: str, meta: dict[str, Any], callbacks: list[Any]) -> Iterator[TraceRecorder]:
    """创建 recorder（或 Noop），把 TraceCallbackHandler 追加进 callbacks；
    finally 中 close(status) —— 异常时 status=failed；全程 fail-open。"""
```

接线（最小改动）：

- `alphabee/apps/cli/streaming.py:112`（callbacks 初始化处）→ 用 `trace_run(entry="cli", ...)` 包裹 `astream` 调用段（120–126），并把 recorder 暴露给后续 TaskRecord 段（284）。
- `alphabee/apps/web/stream.py:107`（callbacks 初始化处）→ 同样包裹 `astream`（114–119）；web 不建 TaskRecord，trace 仍可用。
- 未来脚本/定时入口复用同一 `trace_run`（P1）。

**TaskRecord 关联（additive）**

- `alphabee/task_records/models.py:63` `TaskRecord` 增加 `trace_id: str = ""`；
- `alphabee/task_records/recorder.py:31` `TaskRecorder.capture(..., trace_id: str = "")`；
- `apps/cli/streaming.py` 在 `recorder.capture(...)` 调用处传入当前 trace_id；保存后调用 `recorder.link_task(record.task_id)`（late event + index 更新）。

### 10.2 节点 span（覆盖全部 17 个节点）

`adapters/graph.py::with_trace_node(node_id, fn)`：

- 同步/异步各一套包装（参考 `orchestrator/services/detection.py:91` 的既有模式）；
- 节点开始时 `rec.span("node", node_id, node=node_id)`；记录输入指纹（state 投影）；
- 节点结束时：状态、耗时、输出指纹、`emit_update_records(...)`；
- 异常：`status=error` + `error` 字段 + `node.error` 事件，然后**原样抛出**。

接线：`alphabee/orchestrator/agent.py:489-507` 全部 `add_node` 改为 `with_trace_node(node_id, <existing wrapped fn>)`（与 `with_deviation_detection` 组合，顺序：trace 外层、deviation 内层；两者都 fail-open）。

### 10.3 LLM/工具/agent span（回调）

`adapters/callbacks.py::TraceCallbackHandler(BaseCallbackHandler)`：

```python
class TraceCallbackHandler(BaseCallbackHandler):
    def __init__(self, recorder: TraceRecorder): ...
    # 映射
    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, ...): ...
    def on_llm_end(self, response, *, run_id, parent_run_id=None, ...): ...
    def on_llm_error(self, error, *, run_id, parent_run_id=None, ...): ...
    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, ...): ...
    def on_tool_end(self, output, *, run_id, parent_run_id=None, ...): ...
    def on_tool_error(self, error, *, run_id, parent_run_id=None, ...): ...
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, metadata=None, ...): ...
    def on_chain_end(self, outputs, *, run_id, parent_run_id=None, ...): ...
```

- 父 span：`parent_run_id` 命中 `_run_spans` → 用它；否则用 `current_span_id()`；
- LLM 参数：`serialized["kwargs"]` 取 model/temperature/max_tokens；prompt 用 `messages` 序列化；response 用 `generations[0].message`；usage 复用 `utils/llm.py::_extract_usage_from_langchain_result`；
- 工具：`input_str`/`output` 做截断 + 内容哈希；`status=ok/error`；空结果 → `empty_result` 事件；
- 内容采集：按 policy 级别（L1 只存 hash+预览；L2 采样命中存全文）；
- 链：`on_chain_start` 只在 `metadata.get("langgraph_node")` 或名称指示 agent 时创建 `agent` span，避免噪声；
- **不改** `alphabee/utils/llm.py:147` 的 `TokenUsageCallbackHandler`（保持既有日志行为）；trace handler 通过 `RunnableConfig.callbacks` 注入，不塞进 `create_chat_model`。

### 10.4 语义事件（§6.4 已述）

接入点：`with_trace_node` 出口；`finalize_message` 节点出口记录最终报告引用。

### 10.5 数据获取

`alphabee/data_fetch/recorder.py:48` 已支持 `trace_id/session_id/task_id`；`adapters/data_fetch.py`：

```python
def current_trace_ids() -> tuple[str, str, str]:
    rec = current_trace(); return (rec.trace_id if rec else "", rec.task_id if rec else "", rec.session_id if rec else "")
```

接线：`data_fetch/recorder.py::record_failure` 内部在未显式传入时用 `current_trace_ids()` 填充（默认空 → 行为不变）。P0 只做"失败事件关联"；成功 API span 在 P1（包装 tushare/akshare 适配器）。

### 10.6 MCP（P1 预留）

- `agents/research_reports/agent.py` 的 MCP 工具经 LangChain adapter → 由 §10.3 tool 回调自动覆盖；
- `mcp/jobs.py` 的 OCR job：P1 在 `JobStore` 边界发 `mcp` span/事件，按 `task_id` 关联。

### 10.7 配置

`alphabee/config/__init__.py`：

```python
class TraceSettings(BaseModel):
    enabled: bool = True
    level: Literal["L0", "L1", "L2", "L3"] = "L1"
    l2_sample_rate: float = 0.1
    l3_on_error: bool = True
    max_span_records: int = 20_000
    max_event_records: int = 20_000
    max_content_bytes: int = 262_144
    max_trace_bytes: int = 209_715_200
    preview_chars: int = 512
    late_window_seconds: int = 300
    retention_days: dict[str, int] = Field(default_factory=lambda: {"L1": 90, "L2": 30, "L3": 7})
    storage_dir: str = "data/traces"
    profile_id: str = "alphabee"
    profile_version: int = 1

class ObservabilitySettings(BaseModel):
    trace: TraceSettings = Field(default_factory=TraceSettings)

# Settings 增加：
observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
```

`config.yaml.example` 增加 `observability.trace` 段（含注释与回滚说明：`enabled: false` 即零 IO）。

---

## 11. 写入时序（一次 run）

```text
entry (trace_run)
  → 创建 trace_id、TraceRecorder、TraceWriter、TraceCallbackHandler
  → 发 run.start（status=running）
  → 回调：node/agent/llm/tool spans（父子由 run_id/contextvar 决定）
  → with_trace_node 出口：node.end + artifact/issue/decision 事件 + generated_by 血缘
  → prepare_analysis_context 出口：bind_run(run_id, symbol, as_of)
  → finalize_message 出口：final artifact 内容引用
  → astream 结束：close(status) → 写 manifest（含 counts/completeness）
  → TaskRecorder.capture(..., trace_id) → TaskStore.save
  → recorder.link_task(task_id)（late window 内追加 task.link + 索引更新）
```

---

## 12. 行为不变与依赖约束（必须由测试钉住）

1. **零业务改动**：pipeline 节点函数、agents、reports、midterm 不改一行；仅 `agent.py` 注册处、入口文件、`utils/llm.py` 的 callbacks 参数、`data_fetch` 注入、`TaskRecord` 加字段。
2. **fail-open**：所有 recorder 公共方法内部 `try/except`；异常只增加 `internal_errors`，不改变节点返回值/异常/顺序。
3. **零 IO 关闭**：`enabled=false` 时返回 `NoopRecorder`，不建目录、不写索引、不初始化 SQLite。
4. **依赖方向**：领域模块不 import `alphabee.observability`；`trace/` 不 import 领域模块或框架（`adapters/` 例外）。测试用 AST/grep 扫描。
5. **旧数据兼容**：`TaskRecord` 新字段有默认值；旧 JSON 反序列化不变；旧报告/旧测试不受影响。
6. **golden 不变**：关闭 trace 与开启 trace（但业务输出相同）的两次 run，`report_raw`/artifacts/issues 逐字节一致（用冻结 payload 对比）。

---

## 13. 测试矩阵

| 测试文件 | 覆盖点 |
|---|---|
| `test_schema.py` | 记录 roundtrip、schema_version、未知字段忽略、时间格式 |
| `test_ids.py` | id 前缀/长度、事件幂等、指纹稳定性（同值同指纹） |
| `test_content.py` | 哈希/去重/原子写/分片/大小上限/预览/redacted 标记/读取过期 |
| `test_policy.py` | 默认值、级别/采样、上限降级、settings 解析、关闭→Noop |
| `test_redact.py` | key/token/bearer/密码/代理凭据；脱敏标记；不误伤普通文本 |
| `test_writer.py` | append/flush/close、超大行、序列化失败兜底、只读目录 fail-open |
| `test_recorder.py` | span 嵌套/父子、event、语义事件、lineage、bind_run/link_task、计数、异常不抛 |
| `test_index.py` | DDL、upsert 幂等、按 symbol/date 查、rebuild 一致性 |
| `test_reader.py` | Q1–Q6/Q12 签名与结果、分页、错误语义、预览/全文视图 |
| `test_integrity.py` | 完整性分数（期望 vs 实际）、缺失项、truncated 标记 |
| `adapters/test_graph.py` | 假图（3 节点）覆盖：node span、输入/输出指纹、artifact/issue diff、run bind、异常路径 |
| `adapters/test_callbacks.py` | 假 LLM/tool：父子 span、content ref、token、错误、空结果、MCP 工具（LangChain tool） |
| `adapters/test_semantic.py` | 新实体才发事件、重复 id 不重复、血缘指向节点 span |
| `adapters/test_data_fetch.py` | 默认 None 行为不变；在 trace 上下文中注入 trace_id/task_id |
| `test_task_record_trace.py` | 旧记录反序列化（无 trace_id）；capture 传 trace_id；保存后可回查 |
| `test_dependency_rule.py` | 领域模块不 import observability；trace 不 import 领域/框架 |
| `test_fail_open.py` | 磁盘只读/目录不可建/索引损坏：run 结果不变、有计数 |
| `test_golden_no_change.py` | 同一 payload 下 trace 开/关，report/artifacts/issues 哈希一致 |

---

## 14. PR 切分与回滚

| PR | 内容 | 关键文件 | 测试 | 回滚 |
|---|---|---|---|---|
| **PR-1** | 包骨架 + schema + ids + content + redact | `observability/trace/{schema,ids,content,redact}.py` | test_schema/ids/content/redact | 删包（未接线，行为不变） |
| **PR-2** | policy + writer + recorder + 内部统计 + config | `trace/{policy,writer,recorder}.py`、`config/__init__.py`、`config.yaml.example` | test_policy/writer/recorder | `observability.trace.enabled=false` 或删包 |
| **PR-3** | 索引 + reader（Q1–Q6/Q12） | `trace/{index,reader}.py`、`scripts/trace_query.py`（可选） | test_index/reader | 同 PR-2 |
| **PR-4** | 入口生命周期 + 节点包装 + 语义事件 + TaskRecord trace_id | `adapters/{graph,semantic}.py`、`orchestrator/agent.py`、`apps/cli/streaming.py`、`apps/web/stream.py`、`task_records/{models,recorder}.py` | adapters/test_graph、test_task_record_trace、test_golden_no_change | 移除 `with_trace_node` 包装与入口 context（trace 关闭即可） |
| **PR-5** | 回调 handler（LLM/工具/agent） | `adapters/callbacks.py`、入口 callbacks 列表 | adapters/test_callbacks | 从 callbacks 移除 handler |
| **PR-6** | Profile v1 + 注册/隔离 + 工具目录 | `profile/*` | profile 校验、未知隔离 | 停用 profile 校验（只记录不校验） |
| **PR-7** | 数据获取 trace_id 注入 + 覆盖审计/完整性 | `adapters/data_fetch.py`、`trace/integrity.py` | adapters/test_data_fetch、test_integrity | 还原注入默认空 |
| **PR-8** | dry-run 报告 + 文档 + ROADMAP 登记 | `docs/guide/CONFIGURATION.md`、ROADMAP | 文档链接/示例配置 | 文档 revert |

**总回滚开关**：`observability.trace.enabled: false`（零 IO、零行为变化）；删除包亦可（additive 字段可保留）。

---

## 15. 验收映射（设计 → 实现）

| 设计 AC | 实现验证 |
|---|---|
| AC-1 一次完整 run | PR-4/5 后跑真实/假图 run：manifest 计数、Q12 完整性 |
| AC-2 反查结论（P1） | Q7 血缘（P1）；P0 先验证 artifact/issue/decision → node span 的 `generated_by` |
| AC-4 静默降级 | PR-4/7：fallback/degraded/empty_result 事件落库可查 |
| AC-5 失败隔离 | test_fail_open + 只读目录实测 |
| AC-6 内容分级 | test_content + reader 预览/全文视图 |
| AC-7 脱敏 | test_redact + 内容对象 `redacted` 标记 |
| AC-8 保留 | policy + gc 命令（P0 手动） |
| AC-9 Profile 校验 | test_profile：未知实体/关系进隔离桶、不阻断 |
| AC-10 第二生产者（P1） | P1 用 tracking 调度接入验证 |
| AC-12 Scout 发现（P1） | P1 由 Scout 读 Q1–Q5 |
| AC-13/14 零侵入 | test_dependency_rule + 新增假节点/工具测试 |
| AC-16 未知工具 | test_callbacks：未知 tool 生成通用 span + `unregistered` 标记 |
| AC-17 依赖方向 | test_dependency_rule |
| AC-18 采集自限 | test_policy：关闭/上限/采样 |

---

## 16. 已知限制与后续（P1/P2）

- **P0 不做**：血缘查询 Q7/Q8、差分 Q9、聚合 Q10、导出 Q11、L3 原始快照、MCP 服务级 span、成功 API span、派生血缘、覆盖审计、多 Profile、Scout 查询模板。
- P1 预留：`reader.find_lineage/get_aggregates/get_content(FULL)`、`diff.diff_traces`、`integrity.coverage_report`、`adapters/mcp.py`、`profile/registry` 多域、`scripts/trace_query.py` 子命令。
- P2 预留：OTLP/Langfuse/OpenLineage 导出（`observability/export/`），不改变 core 契约。
- 跨进程/多写者不做；当前假设单进程单写者（CLI/Web 各一次 run）。
- 索引与 JSONL 可能短暂不一致：以 JSONL 为事实源，索引可重建。

---

## 17. 附录

### 附录 A：JSONL 记录示例（trace/span/event）

```json
{"record_type":"trace","schema_version":1,"trace_id":"trc-7ffa949d72aa","entry":"cli","profile_id":"alphabee","profile_version":1,"started_at":"2026-11-16T10:00:00+08:00","status":"running","code_sha":"5d1f3db","config_hash":"sha256:..."}
{"record_type":"span","schema_version":1,"span_id":"spn-1a2b","parent_span_id":null,"trace_id":"trc-7ffa949d72aa","type":"node","name":"collect_raw_facts","node":"collect_raw_facts","started_at":"...","ended_at":"...","duration_ms":12345.6,"status":"ok","attributes":{"input_fingerprint":"...","output_fingerprint":"..."},"content_refs":{},"entity_refs":["artifact-..."]}
{"record_type":"event","schema_version":1,"event_id":"evt-9f3c","trace_id":"trc-7ffa949d72aa","span_id":"spn-1a2b","type":"artifact.produced","occurred_at":"...","attributes":{"artifact_type":"fact_collection"},"refs":["artifact-..."]}
```

### 附录 B：SQLite DDL

见 §7。

### 附录 C：config 示例

```yaml
observability:
  trace:
    enabled: true          # false ⇒ 零 IO、零行为变化（回滚开关）
    level: L1              # L0/L1/L2/L3 默认级别
    l2_sample_rate: 0.1
    l3_on_error: true
    max_content_bytes: 262144
    max_trace_bytes: 209715200
    late_window_seconds: 300
    retention_days: {L1: 90, L2: 30, L3: 7}
    storage_dir: "data/traces"
    profile_id: alphabee
    profile_version: 1
```

### 附录 D：接线锚点速查（实现时以符号名为准）

| 事项 | 文件:行（快照） |
|---|---|
| 图节点注册 | `alphabee/orchestrator/agent.py:489-507` |
| `finalize_message` | `alphabee/orchestrator/agent.py:420` |
| finalize payload | `alphabee/orchestrator/agent.py:438` |
| CLI callbacks/astream | `alphabee/apps/cli/streaming.py:112-126` |
| CLI TaskRecord | `alphabee/apps/cli/streaming.py:284` |
| Web callbacks/astream | `alphabee/apps/web/stream.py:107-119` |
| LLM usage 回调 | `alphabee/utils/llm.py:147` |
| `create_chat_model` | `alphabee/utils/llm.py:218` |
| Run 创建 | `alphabee/orchestrator/nodes/prepare_analysis_context.py:77` |
| TaskRecord | `alphabee/task_records/models.py:63` |
| TaskRecorder.capture | `alphabee/task_records/recorder.py:31,38` |
| data_fetch trace_id 字段 | `alphabee/data_fetch/models.py:103-105` |
| `record_failure` | `alphabee/data_fetch/recorder.py:48` |
| 节点包装先例 | `alphabee/orchestrator/services/detection.py:91` |
| Settings | `alphabee/config/__init__.py:444` |
| 工具清单 | `alphabee/agents/facts/agent.py` |
| MCP 工具 | `alphabee/agents/research_reports/agent.py` |
