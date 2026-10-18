# P0 缺陷修复实施设计（A 股评审落地 · 六项）

> **状态：设计文档（2026-10 完成，尚未实施）**。本文档是 `docs/design/A_SHARE_FRAMEWORK_REVIEW.md` §6「P0 — 缺陷级，先修」六项（P0-1…P0-6）的**代码级实施设计**：每项给出问题复现、方案签名、接线点（既有代码锚点）、序列化与兼容策略、测试钉与回滚。P0-1/P0-2 共用一套「证据增量与作废机制」（§1），P0-3/P0-4 共用「交易日历 + 板块制度映射」两件基础设施（§4/§5）。
> 关联文档：`docs/design/A_SHARE_FRAMEWORK_REVIEW.md`（立项依据）、`docs/midterm/MIDTERM_STATE_DIFF_DESIGN.md`（diff 契约）、`docs/midterm/MIDTERM_EVIDENCE_EXTRACTION.md`（证据规则）、`docs/design/DEVIATION_CONTROL_FRAMEWORK.md` §14.5-A（trigger 纪律）、`docs/roadmap/MARKET_REGIME_ROADMAP.md`（sentiment/policy）。
> 实施时：契约/字段改动走 `alphabee-pipeline-contract-steward`；canonical 字段与 adapter 映射改动（update_flag 等）走 `alphabee-schema-steward`；配置默认值变更按 ROADMAP 变更登记表登记。

---

## 0. 总览

| # | 标题 | 现状根因（已核实锚点） | 量级 | 依赖 |
|---|---|---|---|---|
| P0-1 | 无新信息下置信度爬升 | ① revision 事件 `date=as_of_date`（`evidence_rules.py:388`）⇒ id 跨帧不稳；② `evaluate` 把**全量证据**叠在 `prior=上帧后验` 上（`decision_model.py:360` + `bayes.py:172-188`）⇒ 所有 ≤1yr 事件每帧重复计证 | 中 | §1 基础 A |
| P0-2 | 重述/作废语义缺失 | log-odds 只加不减；预告修正（`update_flag`）未映射；差错更正使下帧 F 变化被当新信息归因 | 中 | §1 基础 A |
| P0-3 | 停牌/交易日历 + ST/退市退出 kind | `_elapsed_days` 按日历日（`diff.py:143-146`）；`ExitCondition` 全仓**只有模型无生产点**（grep 零命中）；ST/退市全代码零引用 | 中 | §4/§5 基础 |
| P0-4 | 触发阈值制度感知 + 财报日历触发器 | `DEFAULT_PRICE_MOVE_PCT=5.0` 全局一刀切（`triggers.py:82`），ST 涨停≈4.95% 被 `abs<=threshold` 漏判；无披露日历概念 | 中 | §4/§5 基础 |
| P0-5 | 政策维度入口 + 涨停情绪温度计激活 | `limit_up_count/limit_down_count` 已采集零消费（`collectors/market_regime/breadth.py:64-67`）；政策零建模入口 | 小 | 无 |
| P0-6 | E 引擎护栏与锚重调 | `_revision` 无 coverage 门槛（`score_engine.py:192-206`、`evidence_rules.py:360-397`）；预告只是 F 的 beat/miss 修正项，不是 E 主轴 | 小 | 与 P0-2 共享 update_flag 映射 |

**建议实施顺序**：PR-A（基础 A + P0-1 + P0-2）→ PR-B（基础 B/C + P0-3 + P0-4）→ PR-C（P0-5 + P0-6）。理由：P0-1 是评审复现验证过的机制缺陷，且基础 A 是 P0-2 的前置；P0-3/P0-4 共享日历与制度映射，一起做避免两遍接线。

---

## 1. 公共基础 A：证据增量与作废机制（P0-1/P0-2 共用）

### 1.1 双根因复现（比评审 §6 更完整的诊断）

- **根因 ①（id 不稳）**：`_revision_evidence` 以 `date=as_of_date`（快照日）构造事件（`evidence_rules.py:388`），`_event_id = hash(date|kind|subject|value)`（`evidence_rules.py:138-142`）⇒ 同一 revision 数值在不同日期换 id。`diff._new_evidence` 按 id 差集（`diff.py:412-417`）把它误判为新证据 ⇒ `evidence_arrival_rate` 虚高、误触 `MANUAL` 触发。
- **根因 ②（全量重算，系统性、不止 revision）**：`evaluate` 每次 `update_confidence(evidence, prior=上帧后验)`（`decision_model.py:360`），而 `update_confidence` 对**传入的全部事件**求和（`bayes.py:186-188`）。`collect_evidence` 每帧重建的数值事件含一年内全部预告/快报/修正（`_filter_fresh_evidence` 只滤 365 天外，`decision_model.py:169-186`）⇒ 即便事件 id 稳定，**每条历史证据每帧都重新贡献一次 log-odds**，置信度必然单调爬升到 clamp。主链一次性 run 无碍（prior=None），tracking 逐帧链路必中。

### 1.2 事件语义三分（本设计的核心口径）

| 语义 | 定义 | 代表 | 后验处理 |
|---|---|---|---|
| **flow 事件** | 一次性事实，永久计入 | 快报 beat/miss、预告发布 | 按 id 幂等，只加一次 |
| **level 事件** | 状态量，新值取代旧值 | revision、rating、coverage | 新值 `supersedes` 旧值：减去旧 log-odds、加上新 log-odds（净效果 = 水平更新） |
| **retraction 事件** | 作废旧证据 | 预告被修正（`update_flag`）、差错更正 | `supersedes` 旧事件且**不带方向贡献**（或带修正后的新事件） |

### 1.3 模型字段（additive，`models.py`）

`EvidenceEvent` 追加一个字段：

```python
class EvidenceEvent(BaseModel):
    ...
    supersedes: list[str] = Field(default_factory=list)  # 被本事件取代/作废的旧事件 id（P0 设计 §1.2）
```

旧帧 JSONL 反序列化自动回落空列表（additive，不升 `schema_version`）。`CompanyStateArtifact.evidence_log` 结构不变。

### 1.4 贝叶斯增量更新（`bayes.py`）

新增（不动 `update_confidence` 既有签名，向后兼容）：

```python
def update_confidence_delta(
    events: list[EvidenceEvent] | None,
    *,
    prior: float | None = None,
    prev_events: list[EvidenceEvent] | None = None,
) -> float | None:
    """增量更新：后验 = prior ⊕ (新事件 log-odds) ⊖ (被 supersede 的旧事件 log-odds)。

    - prev_events 为 None（主链首帧/无上帧日志）→ 退化为现有全量语义（调用 update_confidence）；
    - 新事件 = events 中 id 不在 prev_ids 者（flow 幂等 + level 新值）；
    - 作废项 = prev_events 中 id 出现在任一新事件 supersedes 者（retraction/level 取代）；
    - 净 log-odds = Σ _event_logodds(新) − Σ _event_logodds(旧)，逐条复用既有 _event_logodds（反证 1.2× 不对称保留）；
    - 无净变化 → 返回 prior（不重新夹紧）。
    """
```

### 1.5 `evaluate` 签名与语义（`decision_model.py`）

```python
def evaluate(
    snapshot, evidence=None, *,
    prior_confidence=None, thesis="", portfolio_adjustment=1.0,
    single_stock_cap=None, insight_materiality=None,
    prev_evidence_log: list[EvidenceEvent] | None = None,   # 新增（P0 §1.5）
    classifier_order: str | None = None,                    # 新增（TEF 设计 §4.5，若先落地则合并）
) -> CompanyStateArtifact:
    ...
    confidence = update_confidence_delta(evidence, prior=prior_confidence, prev_events=prev_evidence_log)
```

`get_decision` 同步追加 `prev_evidence_log` 透传。**调用方接线**：`tracking/scheduler.py::_reconcile_frame` 在 `decision_model.evaluate(...)` 处追加 `prev_evidence_log=getattr(prev, "evidence_log", None)`；主链 `nodes/midterm.py` 不传（`None` = 现有语义，一次性 run 行为零变化）。

### 1.6 diff 语义修正（`diff.py`）

- `_new_evidence`（`diff.py:412`）：id 稳定后差集语义自然成立，**代码不动**；
- `CompanyStateDiff` 追加 `superseded_evidence: list[EvidenceEvent] = Field(default_factory=list)`（additive）：本窗口被作废的旧事件（`prev.evidence_log` 中 id 命中 `curr` 事件的 `supersedes` 者）；`ChangeAttribution.note` 由调用侧拼接"作废 N 条、新增 M 条"（`_reconcile_frame` 反证入账段），**不改归因数值口径**。

### 1.7 兼容性与回滚

- 主链（`prev_evidence_log=None`）行为逐字节不变；tracking 路径语义变更（修复），ROADMAP 登记；
- 回滚 = 删除 `update_confidence_delta` 调用改回 `update_confidence`（`prev_evidence_log` 参数可留）；模型/ diff 新字段 additive 可留。

---

## 2. P0-1：revision 跨帧幂等（在基础 A 之上）

### 2.1 事件日期口径修正（`evidence_rules.py`）

- `_consensus_values(consensus)`（`evidence_rules.py:400` 起）追加两个键：`coverage_count`、`consensus_as_of_date`（来自 `ConsensusOutput.as_of_date`，`collectors/consensus/engine.py:71/232` 已有）；
- `_revision_evidence` 的 `_make_event(date=as_of_date)` 改为 `date=values["consensus_as_of_date"]`（缺失 → 回落 `as_of_date`，fail-open 不抛）。修正后：同一 revision 数值在跨帧产生**同 id** ⇒ 差集不再误报新证据（根因 ① 消除）。

### 2.2 level 语义接线（supersede）

`_revision_evidence` 生成的事件加 `supersedes`：由调用方（`evaluate` 增量路径）在 prev 日志中按 `subject == f"{symbol}:revision:{field}"` 匹配旧事件 id 自动填充——**生成器保持纯数据规则**（不知道 prev 日志），supersede 关系在 `update_confidence_delta` 内按 subject 前缀解析（level 语义通用规则：同 `subject` 的事件新值取代旧值，无需逐条手工指定）。因此 1.3 的 `supersedes` 字段主要由 retraction 显式填写，level 取代由增量函数按 subject 自动判定（设计取舍：避免生成器引入 prev 依赖）。

### 2.3 测试钉（`tests/midterm/test_evidence_rules.py` 增补）

- 同一 revision 数值、不同 `as_of_date` ⇒ 事件 id 逐字相等；
- `consensus_as_of_date` 缺失 ⇒ 回落 `as_of_date`（fail-open）；
- 增量路径：同 subject 新值 ⇒ 净 log-odds = logodds(新) − logodds(旧)；同值重复帧 ⇒ 净变化 0、后验不动（**P0-1 主钉**）；
- flow 事件（快报 beat）第二帧重复出现 ⇒ 不再贡献（幂等钉）。

---

## 3. P0-2：重述/作废语义

### 3.1 数据源接线：预告修正（`update_flag`）

- **事实采集**（`agents/facts/tools/expectation_fact.py:42-47`）：`forecast` 的 `fields=` 增加 `update_flag`；
- **adapter 映射**（`adapters/tushare/expectation_mapping.yaml`）：`forecast` 段增加 `update_flag: forecast_update_flag`（schema-steward 流程：`schemas/expectation.yaml` 注册 canonical 字段 `forecast_update_flag`，TEXT，描述 = 预告修正标志）；
- `build_numeric_evidence` 的 forecast 记录透传该字段。

### 3.2 证据生成规则（`evidence_rules.py`）

- **预告修正事件**：同一报告期出现 `update_flag` 标记的**末条**预告（`ann_date` 取末条，修复既有"取首条可能拿到被更正前旧预告"问题，`evidence_rules.py:203-210`）时：生成 retraction 事件（`effect=neutral`、`confidence_delta=0`、`supersedes=[旧预告事件 id]`，description 注明"预告修正：旧预告作废"），随后按**修正后区间**重新生成 beat/miss 基线事件（flow，新 id）。
- **同报告期取末条口径**：`_index_by_period` 由"取首条"改为"按 `ann_date` 升序取末条"（express 与 forecast 同口径）——修复基线错拿旧预告。
- 差错更正（定期报告重述）v1 不做数据源（Tushare 无结构化重述接口），**顺延登记 RC-RESTATE**：机制已就位（retraction 事件 + supersedes），将来接数据源即可，不动机制。

### 3.3 RESTATEMENT 触发（`tracking/triggers.py`）

- `TriggerKind` 增加 `RESTATEMENT = "restatement"`（枚举扩展，§7.3 一并登记"不扩枚举"纪律修订）；
- `detect_fact_triggers` 或 `detect_triggers` 在观察到 retraction 事件（`supersedes` 非空且 `effect=neutral`）时产出该触发，reason 含被作废事件 subject。

### 3.4 FactorSnapshot restated 标记（显式顺延）

评审建议的"受影响 FieldDelta 标 restated 不参与归因"：v1 落在 evidence 层（§3.2/§3.3），factor 层 `restated` 标记**顺延 RC-FACTOR-RESTATE**——理由：差错更正数据源未接，先不扩 FactorSnapshot 契约面；机制预留（FieldDelta 追加字段为 additive，届时可加）。

### 3.5 测试钉

- `update_flag` 预告修正 ⇒ 旧预告事件出现在 `superseded_evidence`、后验先减后加（净效果钉）；
- 同报告期多条预告取 `ann_date` 末条（基线钉）；
- RESTATEMENT 触发产出与 reason（触发钉）；
- 无 `update_flag` 的存量数据路径行为不变（回归钉）。

---

## 4. 公共基础 B：交易日历（P0-3/P0-4 共用）

### 4.1 新模块 `alphabee/midterm/trading_calendar.py`

```python
"""A 股交易日历（P0 设计 §4）：交易日计数 + 停牌检测，fail-open。

提供者链（按序尝试，全部失败 → 日历日兜底，绝不抛）：
  1. tushare trade_cal（token 可用时，缓存全量日历到模块级 lru）；
  2. akshare tool_trade_date_hist_sina（无 token）；
  3. 兜底：自然日（行为 = 现状）。
停牌检测用 tushare suspend_d（可选，缺失 → None = 未知）。

def trading_days_between(start: str, end: str) -> int | None: ...   # None = 日历日兜底
def last_trading_day(as_of: str) -> str | None: ...
def suspension_days(symbol: str, start: str, end: str) -> int | None: ...  # 停牌交易日数
def calendar_source() -> str: ...  # "tushare" / "akshare" / "fallback"（可观测）
```

纪律：函数内延迟 import（不拉 tushare 链）；模块级只缓存日期集合（无 IO 副作用）；`calendar_source` 供帧报告自证口径（fail-open 时不静默——报告标注 `trading_days=None` 即"按日历日"）。

### 4.2 消费方

- P0-3 diff：`_elapsed_days` 配套新增交易日口径（§6.1）；
- P0-4 日历触发器：窗口判定的"到期后第 N 个交易日"（§7.2）；
- 不接入 `evidence_rules`/`bayes`（证据日期口径不变）。

---

## 5. 公共基础 C：板块涨跌停制度映射（P0-3/P0-4 共用）

### 5.1 新模块 `alphabee/midterm/board_limits.py`

```python
"""A 股板块涨跌停幅度映射（P0 设计 §5，纯函数）。

BOARD_LIMITS = {
    "sh_main": 10.0,    # 600/601/603/605
    "sz_main": 10.0,    # 000/001/002/003
    "chinext": 20.0,    # 300/301
    "star": 20.0,       # 688/689
    "bse": 30.0,        # 北交所 8/4/92 开头
}
ST_LIMIT = 5.0

def board_limit_pct(symbol: str, *, is_st: bool = False) -> float:
    """按代码前缀判定；未知前缀 → 10.0（主板口径，保守默认）；is_st=True → 5.0。"""

def limit_ratio(change_pct: float, symbol: str, *, is_st: bool = False) -> float | None:
    """|change| / board_limit_pct（0-1+）；封板判定阈值由调用方定。"""
```

- `is_st` v1 由调用方显式传（缺省 False）；ST 名称自动识别依赖 `namechange` 数据源，**顺延 RC-ST-NAME**（P1）。
- 常量登记为"结构性示意，应回测"。

---

## 6. P0-3：停牌/交易日历 + 退出 kind

### 6.1 diff 交易日口径（`diff.py` + `models.py`）

- `CompanyStateDiff` 追加 `trading_days: int | None = None`、`suspended_days: int | None = None`（additive）；
- `diff(prev, curr, *, anchor=None, trading_days: int | None = None, suspended_days: int | None = None)`：`trading_days=None` ⇒ 维持现状（日历日）逐字节兼容；`elapsed_days` 字段**语义不变**（日历日），新字段承载交易日口径；
- 消费修正：`diff_consumers.monitor_triggers` 的 `evidence_arrival_rate = len(new_evidence)/elapsed_days` 改为**分母优先 `trading_days`**（None → 回落 `elapsed_days`，`diff_consumers.py:116-121`）；velocity/diff_series 同理（登记行为变更：仅当调用方传 `trading_days` 时生效）；
- 接线：`scheduler._reconcile_frame` 调 `diff(...)` 处传入 `trading_days=trading_calendar.trading_days_between(...)`、`suspended_days=trading_calendar.suspension_days(...)`；`TrackingReport` 追加 `calendar_source: str = ""`、`trading_days: int | None = None`（additive，帧报告自证口径）。

### 6.2 退出条件生产点（新建 `alphabee/midterm/exit_conditions.py`）

现状：`ExitCondition` 只有模型（`models.py:492-498`）**无任何生产点**（grep 全仓零命中）。本设计建立第一个生产点：

```python
"""退出条件生产（P0 设计 §6.2）：确定性规则，禁 LLM，fail-open。

def build_exit_conditions(
    symbol: str,
    snapshot: FactorSnapshot,
    *,
    as_of: str = "",
    daily_bars: list[dict[str, Any]] | None = None,   # 注入最近 N 个交易日日线（测试可替身）
) -> list[ExitCondition]:
    """产出新增两类条件（kind 扩展，models.py docstring 同步）：
    - price_risk_stop：最近 N=5 交易日累计跌幅 ≤ −(2 × board_limit) 或 连续 2 日跌停
      （|change| ≥ board_limit_pct − 0.5pp）；
    - delist_warning：最新收盘 < 1.0 元（面值退市观察线）或窗口内连续 20 日收盘 < 1 元
      （窗口不足 20 日时按可得窗口判定"已连续"）。
    数据失败（bars=None/空）→ []，绝不编造。阈值集中模块顶部常量（结构性示意）。
    """
```

- 日线来源：`tushare daily`（近 30 交易日，close/pct_chg），函数内延迟 import、fail-open；v1 **不接** `namechange`（ST 警示 kind 顺延 RC-ST-NAME，与 §5 同一登记项）；
- 接线：`scheduler._reconcile_frame` 在 `evaluate` 后 `curr.exit_conditions = build_exit_conditions(...) + 继承上一帧列表`（继承逻辑已存在，`scheduler.py:418-419`）；主链 `nodes/midterm.py` v1 不接（顺延登记，主链退出条件仍为继承语义）；
- 下游零改动：`check_exit` 读 `exit_conditions_met`（diff 已比较 met 转移），新 kind 自动进入退出信号路径（`MANUAL` 触发承接，`triggers.py:277-303`）。

### 6.3 测试钉（`tests/midterm/test_exit_conditions.py` + `test_trading_calendar.py`）

- 停牌日不产假"无变化"：注入停牌区间 ⇒ `suspended_days` 正确、`evidence_arrival_rate` 分母用交易日；
- 面值 <1 元 ⇒ `delist_warning.met=True`；连续跌停 2 日 ⇒ `price_risk_stop.met=True`；数据缺失 ⇒ `[]`；
- `trading_days=None` ⇒ `elapsed_days` 语义与现状逐字一致（回归钉）；
- 日历提供者链：tushare 失败 → akshare → fallback（monkeypatch 逐级钉）。

---

## 7. P0-4：触发阈值制度感知 + 财报日历触发器

### 7.1 价格阈值制度归一（`tracking/triggers.py`）

- `TriggerThresholds` 追加 `price_move_mode: str = "absolute"`（`absolute`|`limit_relative`）与 `limit_ratio: float = 0.5`（|change|/board_limit ≥ ratio 即触发；0.5 ⇒ 主板 5%、双创 10%、北交所 15%，与 `DEFAULT_PRICE_MOVE_PCT=5.0` 在主板口径一致）；
- `_price_move_triggers(symbol, change, threshold)` 增加 symbol 参数：`limit_relative` 模式按 `board_limits.board_limit_pct(symbol)` 归一，**封板即触发**（|change| ≥ limit−0.5pp 也触发，覆盖"涨停 9.99% 恰低于 10%"与 ST ≈4.95% 恰低于 5% 的边界漏判）；`absolute` 模式维持现状逐字不变（默认值不变 ⇒ 存量行为零变化，登记后由 `config.yaml` 切换）；
- 配置（`config.yaml` + `config.yaml.example` + `DeviationTrackingSettings`）：`deviation.tracking.price_move_mode: absolute`、`price_move_ratio: 0.5`（新默认值**显式登记**：absolute = 现状；ROADMAP 变更登记表登记本段）；
- `tv_distance=0.3` / `evidence_rate=0.5` 的**真实数据标定**：交付 `scripts/calibrate_tracking_thresholds.py`（离线回放 `data/tracking/alerts/*.jsonl` 帧序列，对照真实财报/公告日历与涨跌停事件日，输出 ROC/时滞统计；只读、不写库、不改阈值——标定结论走人工评审后再登记新默认值）。

### 7.2 财报日历触发器（新建 `alphabee/tracking/report_calendar.py`）

```python
"""A 股披露日历触发器（P0 设计 §7.2）：纯规则、零新数据源、禁 LLM。

强制披露窗口（常量表，结构性示意）：
  年报/一季报：4/30；半年报：8/31；三季报：10/31；
  业绩预告强制条件窗口：年报预告 1/31 前（亏损/扭亏/±50% 变动等强制情形）。

def detect_report_calendar_triggers(
    symbol: str, *, as_of: str,
    latest_period: str | None,          # 已观察到的最新报告期（YYYYMMDD，来自 forecast/express 记录的 end_date 最大者）
    expected_periods: list[str],        # as_of 时点应已披露的报告期列表（按窗口推导）
) -> list[Trigger]:
    """产出 kind=REPORT_CALENDAR 的触发：
    - report_overdue：窗口到期后第 3 个交易日仍未观察到预期报告期（"该出未出"）；
    - forecast_due：进入预告强制窗口（1/1–1/31、7/1–7/15）且无对应预告记录；
    - report_due：下一个披露窗口前 5 个交易日的"预告提醒"（低噪提示，payload 带窗口日）。
    """
```

- **零新数据源**：`latest_period` 由调用方从 `get_expectation_fact(symbol)` 的 forecast/express 记录取 `end_date` 最大值（`scheduler` 侧已有该数据通路，`decision_model.collect_evidence` 同一来源）；
- **TriggerKind 扩展**：`REPORT_CALENDAR = "report_calendar"`。`triggers.py` 模块 docstring 的"不扩枚举"纪律**在本设计授权下修订**（枚举扩展仅限：`RESTATEMENT`、`REPORT_CALENDAR` 两项，登记于 ROADMAP；其余事件通道仍走既有五类）；
- **接线**：`run_once` 在 `detect_fact_triggers` 之后追加第三来源 `detect_report_calendar_triggers(...)`，统一走 `_dedupe_triggers`（`scheduler.py:681-689` 处插入）；日历不可得（无法算交易日）→ 该来源返回 `[]`（fail-open）。

### 7.3 测试钉

- 板块归一：双创 +10% 触发 / 主板 +5% 触发 / ST +4.95% 在 `limit_relative` 下触发、在 `absolute` 下不触发（边界修复钉）；北交所 +15% 阈值；
- 财报日历：`as_of=2026-05-06`（4/30 后第 3 个交易日）且 latest_period 停在三季报 ⇒ `report_overdue`；窗口内提前 5 交易日 ⇒ `report_due`；日历失败 ⇒ `[]`；
- 默认值行为：`price_move_mode` 缺省 ⇒ 与现状逐字一致（回归钉）；
- 枚举扩展后 `detect_triggers` 五类旧行为全绿（回归钉）。

---

## 8. P0-5：政策维度入口 + 涨停情绪温度计激活

### 8.1 涨停情绪子分（`market_regime/`）

- 新规则文件 `alphabee/market_regime/rules/sentiment_heat.yaml`（结构照 `risk_preference.yaml`）：

```yaml
# 涨停情绪调整项（P0 设计 §8.1）：A 股情绪温度计，进 risk_preference 调整项
rules:
  sentiment_heat:
    weight: 0.20
    description: 涨停家数分档 + 涨跌停比；过热为正、冰点为负，±3 封顶
    required_facts: [limit_up_count, limit_down_count]
    formula: >
      (limit_up_count > 100) * 3 + (limit_up_count < 20) * -2 +
      (limit_up_count >= 20) * (limit_up_count <= 100) * (limit_up_count - 20) / 80 * 2 +
      ((limit_down_count > 0) * (-min(limit_down_count / max(limit_up_count, 1) * 3, 3)))
    thresholds:
      overheated: "value >= 2"
      neutral: "-2 <= value < 2"
      frozen: "value < -2"
```

- `RISK_DELTA_RULES`（`market_regime/score_engine.py:54`）追加 `"sentiment_heat"`；`risk_preference.yaml` 既有三项权重**重平衡**：`turnover_delta 0.40 / margin_delta 0.25 / etf_delta 0.15 / sentiment_heat 0.20`（**行为变更登记**：情绪总分仍 ±5 封顶，缺失子项按 0 中性——`_risk_delta` 既有口径不变，`score_engine.py:384-398`）；
- 数据已就位：`breadth.py:64-67` 已产出 `limit_up_count/limit_down_count`，schema 已注册（`schemas/market_regime.yaml:441/454`），**只差规则消费**。

### 8.2 政策维度入口

- 新 domain primitive `alphabee/domain_context/domain_primitives/policy_shock.yaml`（结构照 `cost_curve.yaml`：id/version/description/when_to_activate/key_variables/causal_paths/priority_questions/disconfirming_signals/preferred_sources/report_angles/assumptions；内容 = 政策敏感赛道判定：补贴退坡/审批/牌照/定价管制/国产替代依赖等，`when_to_activate` 与 track_label/申万标签匹配）；
- schema：`schemas/market_regime.yaml` 追加 `policy_regime: {category: market_regime, type: str, unit: TEXT, frequency: [event], description: 政策状态定性标签}`（v1 占位，数据源 = 政策事件通道，P1 顺延 RC-POLICY-DATA）；
- **Issue 发射点**（确定性、无 LLM）：`resolve_driver_profile` 或 domain router 在选中 `policy_shock` primitive 时产出 `Issue(category="policy_exposure", severity=MEDIUM, scope="data", message=...)`（`services/deviation.py::CLASS_BY_CATEGORY` 登记 `policy_exposure → D4_STATE`）；该 Issue 进入报告"系统问题"段与账本，**不进 regime 主权重**（评审 §6 P0-5 口径）。

### 8.3 测试钉

- sentiment_heat：涨停 200 家/跌停 0 ⇒ 正调整；涨停 10 家/跌停 30 ⇒ 负调整；缺失 ⇒ 0 中性（`_risk_delta` 回归）；
- 权重重平衡后三旧规则权重和 = 0.8（钉住新权重表，防静默漂移）；
- policy_shock primitive 被路由选中 ⇒ `policy_exposure` Issue 产出；未选中 ⇒ 零 Issue（无死码路径）；
- 涨停情绪对 regime 总分影响 ≤ ±5（封顶钉）。

---

## 9. P0-6：E 引擎护栏与锚重调

### 9.1 evidence 层护栏（`evidence_rules.py`）

`_revision_evidence` 开头增加 coverage 护栏（照抄 `factors.py:431-439` 的 C 因子单样本护栏模式）：

```python
coverage = _to_int(values.get("coverage_count"))
if coverage is None or coverage < _MIN_CONSENSUS_COVERAGE:  # 常量 = 3，结构性示意
    return []   # 覆盖不足 ⇒ 不产 revision 证据（防单份研报调价即 strong）
```

### 9.2 score_engine 层护栏与权重再平衡（`score_engine.py`）

- `_revision(e)`（`score_engine.py:192-206`）开头：`e.coverage_count is None or < 3` ⇒ 返回 `None` 并把 `"consensus_coverage_low"` 登记进 `FactorSnapshot.missing_facts`（复用 factors 的缺失登记通道，`factors.py:431-439` 同构）；
- **锚重调**：E 方向分 = 0.6 × 预告锚 + 0.4 × 研报 revision（当前研报 revision 权重 1.0）：

```python
def _revision(e: ExpectationFactor) -> float | None:
    """E 方向分（P0-6 重调）：预告锚 0.6 + 研报修正 0.4；coverage<3 → 预告锚单独承载。"""
    anchor = _forecast_revision_anchor(e)      # 新增：最新预告区间中点 vs 上期预告区间中点（同报告期相邻期）
    research = _research_revision(e)           # 既有逻辑（revision + breadth），降权 0.4
    # 两者合成；anchor 缺失 → research 单独；research 缺失 → anchor 单独；全缺 → None
```

- `_forecast_revision_anchor`：相邻两个报告期的预告区间中点差（PERCENT，上修为正），`_log1p_saturate` 同口径——**业绩预告成为 E 主轴**（评审 §6 P0-6 要求）；无相邻期数据 → `None`；
- 依赖 §3.1 的 `update_flag`/`ann_date` 映射（取修正后末条预告参与锚计算）。

### 9.3 consensus_missing issue

`nodes/midterm.py` 的 `resolve_midterm_decision` 在收到 `missing_facts` 含 `consensus_coverage_low` 且本 run 无 revision 证据时产出 `Issue(category="consensus_missing", severity=MEDIUM, scope="data", message=...)`（`services/deviation.py` 登记 `consensus_missing → D1_DATA`；与 `company_track_missing` 同构，`recovery.py:101` 邻位登记）。不阻断、不降级，只留痕。

### 9.4 测试钉

- coverage=2 ⇒ revision 证据 `[]`、`e_revision=None`、missing_facts 登记（护栏钉）；
- coverage≥3 ⇒ 行为与现状一致（回归钉）；
- 预告锚：相邻期预告中点上修 ⇒ E 正贡献；只有研报 ⇒ 0.4 权重路径；
- `consensus_missing` Issue 只在覆盖不足时产出（零死码钉）；
- 变异：删 coverage 护栏 ⇒ 单份研报 strong 事件回归（必红）。

---

## 10. 测试矩阵汇总（新增/增补）

| 文件（新增/增补） | 内容 | 目标量级 |
|---|---|---|
| `tests/midterm/test_evidence_delta.py`（新） | 基础 A：增量/幂等/level 取代/retraction | ~20 |
| `tests/midterm/test_evidence_rules.py`（增补） | P0-1 日期口径、P0-2 update_flag、P0-6 护栏 | +15 |
| `tests/midterm/test_trading_calendar.py`（新） | 提供者链/兜底/交易日计数 | ~12 |
| `tests/midterm/test_board_limits.py`（新） | 板块映射/ST/未知前缀 | ~8 |
| `tests/midterm/test_exit_conditions.py`（新） | P0-3 两类新 kind + 继承 | ~12 |
| `tests/tracking/test_triggers.py`（增补） | P0-4 归一阈值/RESTATEMENT/REPORT_CALENDAR | +18 |
| `tests/tracking/test_report_calendar.py`（新） | 窗口/该出未出/预告提醒 | ~15 |
| `tests/market_regime/test_sentiment.py`（新/增补） | P0-5 sentiment_heat + 权重钉 | +10 |
| `tests/midterm/test_score_engine.py`（增补） | P0-6 锚重调 + 护栏 | +10 |
| `tests/domain_context/`（增补） | policy_shock primitive 路由 + Issue | +6 |

**变异杀伤（每期门禁）**：删 coverage 护栏必红；`update_confidence_delta` 改回全量求和必红（P0-1 主钉）；日历兜底静默（不标 calendar_source）必红；sentiment 权重漂移必红。
**全量回归**：`pytest tests -q -m "not integration"` 离线口径（ROADMAP 工程实践第 10 条）；`tests/midterm`（400）/`tests/tracking`（150+）/`tests/market_regime` 全绿；`NODE_ORDER`、契约不变量不变。

---

## 11. 验收标准

1. **P0-1**：同一 revision 数值跨帧同 id；tracking 连续两帧无新信息 ⇒ 后验逐字节不变（主钉）；`evidence_arrival_rate` 不再虚高；
2. **P0-2**：预告修正 ⇒ 旧事件作废、后验先减后加、RESTATEMENT 触发产出；同报告期取 `ann_date` 末条；无 update_flag 存量数据行为不变；
3. **P0-3**：`trading_days`/`suspended_days` 落 diff 与报告；停牌不产假稳定；`price_risk_stop`/`delist_warning` 从新建生产点产出并进入退出信号路径（行动仍走 Tier 5 红线，零自动执行）；
4. **P0-4**：`limit_relative` 下双创 10%、ST 涨停、封板边界全部正确触发；`absolute` 缺省行为逐字不变；财报日历三类触发在真实日历样本上命中；
5. **P0-5**：涨停情绪进入 ±5 调整项（封顶钉）；`policy_shock` primitive 路由命中 ⇒ `policy_exposure` Issue，不进主权重；
6. **P0-6**：coverage<3 时 revision 证据与 E 分双护栏生效、`consensus_missing` 留痕；预告锚成为 E 主轴（0.6）；
7. **登记**：§13 全部完成；全量门禁离线口径通过；每项具备回滚路径。

---

## 12. 非目标与顺延登记（一次到位，防止范围膨胀）

| 登记号 | 内容 | 理由 |
|---|---|---|
| RC-RESTATE | 差错更正（定期报告重述）数据源接入 | Tushare 无结构化重述接口；retraction 机制已就位，届时只接数据源 |
| RC-FACTOR-RESTATE | `FieldDelta.restated` 标记 | 依赖 RC-RESTATE；FieldDelta 追加字段为 additive，可后补 |
| RC-ST-NAME | ST/*ST 自动识别（`namechange` 数据源）、ST 板块 5% 与 ST 警示 kind | 需新数据源 + schema-steward；v1 由 `is_st` 显式参数承载 |
| RC-POLICY-DATA | `policy_regime` 字段的数据源（政策事件通道） | v1 为占位字段 + primitive + Issue；LLM 政策分类属后续 |
| RC-CALIBRATE | `tv_distance=0.3`/`evidence_rate=0.5` 的标定结论落地为新默认值 | 依赖 §7.1 标定脚本跑出结论，人工评审后登记 |
| — | 主链 `nodes/midterm.py` 接 exit_conditions 生产点 | v1 只接 tracking 路径（帧持久化路径），主链保持继承语义 |

---

## 13. 文档与变更登记要求

1. **ROADMAP.md 变更登记表**新增行（每 PR 一行）：`deviation.tracking.price_move_mode/price_move_ratio` 新配置段默认值登记（absolute=现状）；`risk_preference` 权重重平衡（0.5/0.3/0.2 → 0.4/0.25/0.15/0.2）；TriggerKind 枚举扩展（RESTATEMENT/REPORT_CALENDAR，"不扩枚举"纪律修订授权）；`evaluate` 增量语义（tracking 路径）；E 锚重调（0.6/0.4）；
2. **schema-steward 条目**：`forecast_update_flag` canonical 字段 + adapter 映射（§3.1）；`policy_regime` 字段（§8.2）；
3. **模块 docstring 逐字披露**：`triggers.py`（枚举扩展口径）、`decision_model.evaluate`（增量语义与主链兼容口径）、`diff_consumers.monitor_triggers`（分母口径变更条件）、`score_engine._revision`（锚重调与 coverage 护栏）；
4. **设计文档回填**：`docs/design/A_SHARE_FRAMEWORK_REVIEW.md` §6 P0 各行追加「已实施」标注与提交号；
5. **配置文档**：`docs/guide/CONFIGURATION.md` 增补 `deviation.tracking.price_move_*` 说明。
