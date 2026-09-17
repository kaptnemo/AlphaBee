"""F4 调度器测试（§9.3 / §9.4 / §14.5-A）。

覆盖：**复用面**（reconcile 与 run_once 同一内核 + 真的调用 midterm 引擎）、
**反证强制入账**（每帧 diff 同时入账支持与反对证据）、**CLI smoke**（`main(argv)`）、
**★ 安全红线**（`require_human_confirm` 恒 False，行动类输出永不自动执行）。
"""

from __future__ import annotations

import hashlib
import inspect
import json

import pytest

from alphabee.midterm.models import (
    ArtifactRef,
    ChangeAttribution,
    CompanyStateArtifact,
    CompanyStateDiff,
    EvidenceEvent,
    ExitCondition,
    FactorSnapshot,
    FundamentalFactor,
    PositionDecision,
    PositionDiff,
    RiskFactor,
    StateBelief,
    StateShift,
    StateTransition,
    TrendFactor,
)
from alphabee.midterm.persistence import append_artifact, latest_artifact
from alphabee.tracking import scheduler as scheduler_module
from alphabee.tracking.scheduler import (
    ACTION_CLASS_GATE_TIERS,
    DEFAULT_STALE_AFTER_DAYS,
    _action_class_outputs,
    _dedupe_triggers,
    _gate_actions,
    enforce_attribution_accounting,
    main,
    reconcile,
    require_human_confirm,
    run_once,
    run_watchlist,
)
from alphabee.tracking.triggers import Trigger, TriggerKind, TriggerThresholds

SYMBOL = "600519.SH"
D1 = "2026-09-01"
D2 = "2026-09-20"


# ── 构造器（全部离线：快照与证据都由测试注入） ──────────────────────────────


def _snapshot(
    as_of: str,
    *,
    revenue_yoy: float = 12.0,
    price_change_pct: float = 1.0,
    news: str = "",
    symbol: str = SYMBOL,
) -> FactorSnapshot:
    return FactorSnapshot(
        symbol=symbol,
        as_of_date=as_of,
        fundamental=FundamentalFactor(revenue_yoy=revenue_yoy, net_profit_yoy=8.0, roe=15.0),
        trend=TrendFactor(price_change_pct=price_change_pct),
        risk=RiskFactor(news_title=news),
    )


def _evidence(date: str, *, symbol: str = SYMBOL) -> list[EvidenceEvent]:
    """两向证据（支持 + 反对）——id 带日期，保证跨帧是"新证据"。"""
    return [
        EvidenceEvent(
            id=f"{symbol}:support:{date}",
            date=date,
            kind="expectation",
            description="分析师上修盈利预测",
            effect_on_thesis="confirming",
            confidence_delta=0.2,
        ),
        EvidenceEvent(
            id=f"{symbol}:oppose:{date}",
            date=date,
            kind="fundamental",
            description="经营现金流弱于净利润",
            effect_on_thesis="refuting",
            confidence_delta=0.25,
        ),
    ]


def _providers(date: str, *, revenue_yoy: float = 12.0, price_change_pct: float = 1.0, news: str = ""):
    return {
        "snapshot_provider": lambda symbol, _d=date, _r=revenue_yoy, _p=price_change_pct, _n=news: _snapshot(
            _d, revenue_yoy=_r, price_change_pct=_p, news=_n
        ),
        "evidence_provider": lambda symbol, _d=date, **kwargs: _evidence(_d),
    }


def _prev_artifact_with_state(state: str, as_of: str = D1) -> CompanyStateArtifact:
    """造一帧"上一帧"：state 由测试钉住（用于构造状态迁移 → 行动类输出）。"""
    artifact = CompanyStateArtifact(symbol=SYMBOL, thesis="核心假设", as_of_date=as_of)
    artifact.state = StateBelief(distribution={state: 1.0}, argmax_state=state, entropy=0.0)
    artifact.factor_snapshot = _snapshot(as_of)
    return artifact


# ── ① ★ 安全红线（§9.4） ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "action",
    [
        None,
        "state_transition:downgrade:S5→S2",
        {"kind": "position_change", "target": 0.1},
        object(),
        0,
        "",
    ],
)
def test_require_human_confirm_always_false(action):
    """★ v1 **恒返回 False**：任何行动类输出都不可自动执行（分析系统的红线）。

    放开条件写在 docstring 里：**未来接入人工确认 UI 才可放开**，且届时也只改这一个函数。
    """
    assert require_human_confirm(action) is False


def test_require_human_confirm_docstring_states_the_red_line():
    """红线的"为什么"必须留在代码里（防被后人当成"没实现的 TODO"顺手改成 True）。"""
    doc = inspect.getdoc(require_human_confirm) or ""
    assert "恒返回" in doc and "永不自动执行" in doc
    assert "人工确认 UI" in doc  # 放开条件
    assert "Tier 0" in doc and "Tier 5" in doc  # §9.4 只允许的两档


def test_action_gate_only_allows_tier_0_or_5():
    """行动类 gate：允许档位恰为 {0, 5}；被拦下 → Tier 5（升级人工），无动作 → Tier 0。"""
    assert ACTION_CLASS_GATE_TIERS == (0, 5)
    blocked, tier = _gate_actions(["state_transition:downgrade:S5→S2"])
    assert blocked == ["state_transition:downgrade:S5→S2"] and tier == 5
    blocked_empty, tier_empty = _gate_actions([])
    assert blocked_empty == [] and tier_empty == 0


def test_action_class_outputs_from_diff():
    """行动类输出识别：状态迁移与仓位带背离；``diff=None``（首帧/未推进）→ 无。"""
    assert _action_class_outputs(None) == []
    shift = StateShift(argmax_from="S5", argmax_to="S2", kind="downgrade")
    frame = CompanyStateDiff(
        symbol=SYMBOL,
        prev=None,
        curr=ArtifactRef(id=f"{SYMBOL}:{D2}", date=D2, symbol=SYMBOL),
        elapsed_days=19,
        state_shift=shift,
    )
    actions = _action_class_outputs(frame)
    assert actions == ["state_transition:downgrade:S5→S2"]
    assert _action_class_outputs(frame) == actions  # 纯投影，可重复调用
    # 首帧 = 基线登记，不是动作（否则"建仓登记"会被误判成触发人工确认的行动类输出）
    first = CompanyStateDiff(
        symbol=SYMBOL,
        prev=None,
        curr=ArtifactRef(id=f"{SYMBOL}:{D1}", date=D1, symbol=SYMBOL),
        elapsed_days=0,
        is_first=True,
        state_shift=StateShift(argmax_from=None, argmax_to="S1", kind="upgrade"),
        position=PositionDiff(band_from="", band_to="S1"),
    )
    assert _action_class_outputs(first) == []
    # 仓位带变化（§9.4 的"仓位带变化"分支）
    band = CompanyStateDiff(
        symbol=SYMBOL,
        prev=None,
        curr=ArtifactRef(id=f"{SYMBOL}:{D2}", date=D2, symbol=SYMBOL),
        elapsed_days=19,
        position=PositionDiff(band_from="S1", band_to="S2"),
    )
    assert _action_class_outputs(band) == ["position_band_change:S1→S2"]


# ── ② 复用面（reconcile 与 run_once 同一内核 + 真调 midterm 引擎） ──────────


def test_reconcile_reuses_midterm_engines(tmp_path, monkeypatch):
    """复用面：`diff` / `evaluate` / `check_exit` / `monitor_triggers` 都被**真的调用**（spy 计数）。"""
    from alphabee.midterm import decision_model as md
    from alphabee.midterm import diff_consumers

    calls: dict[str, int] = {"diff": 0, "evaluate": 0, "check_exit": 0, "monitor_triggers": 0}
    real = {
        "diff": scheduler_module.midterm_diff,
        "evaluate": md.evaluate,
        "check_exit": diff_consumers.check_exit,
        "monitor_triggers": diff_consumers.monitor_triggers,
    }

    def _spy(name):
        def _wrapper(*args, **kwargs):
            calls[name] += 1
            return real[name](*args, **kwargs)

        return _wrapper

    # ★ 一律 patch **midterm 侧**：能生效本身就证明调用真的路由到 midterm（复用而非重写）
    monkeypatch.setattr(scheduler_module, "midterm_diff", _spy("diff"))
    monkeypatch.setattr(md, "evaluate", _spy("evaluate"))
    monkeypatch.setattr(diff_consumers, "check_exit", _spy("check_exit"))
    monkeypatch.setattr(diff_consumers, "monitor_triggers", _spy("monitor_triggers"))

    state_dir = tmp_path / "state"
    first = reconcile(SYMBOL, as_of=D1, state_dir=state_dir, persist=False, **_providers(D1))
    assert first.as_of_date == D1
    assert calls["evaluate"] == 1 and calls["diff"] == 1  # 首帧也差分（基线登记）
    assert calls["check_exit"] == 1 and calls["monitor_triggers"] == 1
    assert calls["diff"] == 1  # 单帧只差分一次

    second = reconcile(SYMBOL, as_of=D2, state_dir=state_dir, persist=True, **_providers(D2, revenue_yoy=20.0))
    assert second.as_of_date == D2
    assert calls["diff"] == 2 and calls["evaluate"] == 2
    assert latest_artifact(SYMBOL, state_dir) is not None  # 真落了盘


def test_reconcile_and_run_once_share_one_kernel(tmp_path):
    """`reconcile` 是内核的契约薄壳：**同输入 → 同帧**（两个全新 state_dir 都是首帧）。

    （第二个 state_dir 刻意留空：否则 run_once 会把第一帧当 prev、用它的后验当先验，
    ``thesis_confidence`` 自然不同 —— 那是 bayes 的正确行为，不是内核分叉。）
    """
    artifact = reconcile(SYMBOL, as_of=D1, state_dir=tmp_path / "state_a", persist=True, **_providers(D1))
    report = run_once(SYMBOL, as_of=D1, state_dir=tmp_path / "state_b", alert_dir=tmp_path / "alerts", **_providers(D1))
    assert report.persisted is True
    assert report.state == artifact.state.argmax_state
    assert report.thesis_confidence == artifact.thesis_confidence
    persisted = latest_artifact(SYMBOL, tmp_path / "state_b")
    assert persisted is not None
    assert persisted.model_dump(mode="json") == artifact.model_dump(mode="json")
    assert report.escalation_tier == 0 and report.blocked_actions == []


def test_same_day_rerun_is_idempotent_on_disk(tmp_path):
    """同日重跑：append-only 持久化**幂等**（不产生重复行），差分被显式跳过。"""
    state_dir = tmp_path / "state"
    reconcile(SYMBOL, as_of=D1, state_dir=state_dir, persist=True, **_providers(D1))
    reconcile(SYMBOL, as_of=D1, state_dir=state_dir, persist=True, **_providers(D1))
    rows = (state_dir / f"{SYMBOL}.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1


def test_reconcile_wires_stale_after_ttl(tmp_path):
    """`stale_after` 写入口：健康帧推进为 as_of+保鲜期；降级帧保留旧值（保持"陈旧"告警）。"""
    state_dir = tmp_path / "state"
    first = reconcile(SYMBOL, as_of=D1, state_dir=state_dir, persist=True, **_providers(D1))
    assert first.stale_after == "2026-09-08"  # D1 + 7

    healthy = reconcile(SYMBOL, as_of=D2, state_dir=state_dir, persist=True, **_providers(D2))
    assert healthy.stale_after == "2026-09-27"  # D2 + 7（重新采集后保鲜期推进）

    degraded_provider = {
        "snapshot_provider": lambda symbol: _snapshot(D2, revenue_yoy=1.0),
        "evidence_provider": lambda symbol, **kwargs: [],
    }
    frame = scheduler_module._reconcile_frame(
        SYMBOL, as_of="2026-09-25", state_dir=state_dir, persist=False, **degraded_provider
    )
    assert frame.artifact.degraded is False  # 该 provider 不标降级 → 仍推进
    frame2 = scheduler_module._reconcile_frame(
        SYMBOL,
        as_of="2026-09-26",
        state_dir=state_dir,
        persist=False,
        snapshot_provider=lambda symbol: _snapshot(D2, revenue_yoy=1.0),
        evidence_provider=lambda symbol, **kwargs: [],
        thresholds=TriggerThresholds(stale_after_days=5),
    )
    assert frame2.artifact.stale_after == "2026-10-01"


def test_exit_conditions_are_carried_forward_and_trigger_manual(tmp_path):
    """§9.2：`exit_conditions` 字段已有但无调度 —— 本层**继承**它们，并驱动 MANUAL 触发。"""
    state_dir = tmp_path / "state"
    seeded = _prev_artifact_with_state("S5", D1)
    seeded.exit_conditions = [ExitCondition(kind="thesis_broken", condition="现金流连续两季低于净利", met=True)]
    append_artifact(seeded, state_dir)

    report = run_once(SYMBOL, as_of=D2, state_dir=state_dir, alert_dir=tmp_path / "alerts", **_providers(D2))
    kinds = [t.kind for t in report.triggers]
    assert TriggerKind.MANUAL in kinds
    manual = next(t for t in report.triggers if t.kind is TriggerKind.MANUAL)
    assert "artifact_exit_conditions" in manual.payload["sources"]
    assert "thesis_broken" in str(manual.payload)
    assert latest_artifact(SYMBOL, state_dir).exit_conditions[0].met is True  # 继承落地


def test_state_transition_blocks_action_and_escalates(tmp_path):
    """★ 端到端红线：状态迁移 → 行动类输出 → 被 §9.4 闸拦下（Tier 5）且写进告警。"""
    state_dir = tmp_path / "state"
    append_artifact(_prev_artifact_with_state("S5", D1), state_dir)  # 上一帧 S5
    report = run_once(
        SYMBOL, as_of=D2, state_dir=state_dir, alert_dir=tmp_path / "alerts", **_providers(D2, revenue_yoy=40.0)
    )
    assert report.escalation_tier == 5
    assert report.blocked_actions and report.blocked_actions[0].startswith("state_transition:")
    gate = [t for t in report.triggers if t.payload.get("source") == "human_confirm_gate"]
    assert gate and gate[0].kind is TriggerKind.MANUAL
    assert gate[0].payload["allowed_tiers"] == [0, 5]
    # 告警已落盘（含被拦下的动作）
    lines = (tmp_path / "alerts" / f"{SYMBOL}.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[-1])["escalation_tier"] == 5


def test_no_action_output_means_no_block_and_no_alert_noise(tmp_path):
    """无行动类输出（且无触发）→ 不拦、不落警（避免噪声）；报告仍是完整结构。"""
    state_dir = tmp_path / "state"
    provider = _providers(D1)
    report = run_once(SYMBOL, as_of=D1, state_dir=state_dir, alert_dir=tmp_path / "alerts", **provider)
    assert report.escalation_tier == 0 and report.blocked_actions == []
    assert not (tmp_path / "alerts" / f"{SYMBOL}.jsonl").exists()


# ── ③ 反证强制入账（§9.2） ──────────────────────────────────────────────────


def test_both_evidence_sides_are_accounted_every_frame(tmp_path):
    """★ 每帧同时入账支持与反对证据（两向都在 diff 的 `new_evidence` + 归因覆盖）。"""
    state_dir = tmp_path / "state"
    reconcile(SYMBOL, as_of=D1, state_dir=state_dir, persist=True, **_providers(D1))
    frame = scheduler_module._reconcile_frame(
        SYMBOL, as_of=D2, state_dir=state_dir, persist=False, **_providers(D2, revenue_yoy=20.0)
    )
    assert frame.frame_diff is not None
    effects = {e.effect_on_thesis for e in frame.frame_diff.new_evidence}
    assert effects == {"confirming", "refuting"}  # 两向都进了本帧证据面
    assert frame.accounting.supporting_new == 1 and frame.accounting.opposing_new == 1
    assert frame.accounting.forced_sides == []  # 归因已覆盖 → 无需强制补记账
    positive = frame.artifact.evidence_log
    assert {e.effect_on_thesis for e in positive} == {"confirming", "refuting"}


def test_missing_attribution_for_opposing_side_is_forced_accounted():
    """★ 反证侧有新证据却没有归因条目 → **强制**追加一条显式记账（不静默吸收）。"""
    opposing = EvidenceEvent(
        id="e-oppose",
        date=D2,
        kind="fundamental",
        description="现金流弱",
        effect_on_thesis="refuting",
        confidence_delta=0.3,
    )
    curr = CompanyStateArtifact(symbol=SYMBOL, as_of_date=D2, evidence_log=[opposing])
    frame_diff = CompanyStateDiff(
        symbol=SYMBOL,
        prev=None,
        curr=ArtifactRef(id=f"{SYMBOL}:{D2}", date=D2, symbol=SYMBOL),
        elapsed_days=19,
        new_evidence=[opposing],
        attribution=[],
    )
    accounting = enforce_attribution_accounting(frame_diff, curr=curr, pool=[opposing])
    assert accounting.forced_sides == ["opposing"]
    assert len(frame_diff.attribution) == 1
    entry = frame_diff.attribution[0]
    assert entry.evidence_ids == ["e-oppose"]
    assert entry.decision_effects == ["contradiction_accounting:opposing"]
    assert "不猜测因果" in entry.note
    assert "强制补记账侧别：opposing" in accounting.note


def test_partial_coverage_is_accounted_per_event():
    """★ F4-L1 加固：**部分覆盖**（某侧只有一部分证据被引用）⇒ 其余证据**逐条**入账。

    构造：`new_evidence = [支持1, 支持2, 反对1]` + `attribution = [只引支持1]`。
    旧实现（按侧守卫 `any(...)` ⇒ 整侧跳过）会**放过 支持2 与 反对1**；逐条实现在此必须
    为两者各追加一条显式记账，且**不得**重复引用已被引用的 支持1。
    """
    support_1 = EvidenceEvent(
        id="e-sup-1",
        date=D2,
        kind="expectation",
        description="上修",
        effect_on_thesis="confirming",
        confidence_delta=0.2,
    )
    support_2 = EvidenceEvent(
        id="e-sup-2",
        date=D2,
        kind="trend",
        description="相对强度新高",
        effect_on_thesis="confirming",
        confidence_delta=0.2,
    )
    opposing = EvidenceEvent(
        id="e-opp",
        date=D2,
        kind="fundamental",
        description="现金流弱",
        effect_on_thesis="refuting",
        confidence_delta=0.3,
    )
    curr = CompanyStateArtifact(symbol=SYMBOL, as_of_date=D2, evidence_log=[support_1, support_2, opposing])
    frame_diff = CompanyStateDiff(
        symbol=SYMBOL,
        prev=None,
        curr=ArtifactRef(id=f"{SYMBOL}:{D2}", date=D2, symbol=SYMBOL),
        elapsed_days=19,
        new_evidence=[support_1, support_2, opposing],
        attribution=[ChangeAttribution(evidence_ids=["e-sup-1"], factor_deltas=["E"], note="只引了支持1")],
    )
    accounting = enforce_attribution_accounting(frame_diff, curr=curr, pool=[support_1, support_2, opposing])
    forced_ids = [eid for entry in frame_diff.attribution[1:] for eid in entry.evidence_ids]
    assert sorted(forced_ids) == ["e-opp", "e-sup-2"]  # 未引用的两条各自入账
    assert "e-sup-1" not in forced_ids  # 已引用的一条不重复记账
    assert accounting.forced_sides == ["supporting", "opposing"]  # 同侧去重
    assert len(accounting.forced_sides) == len(set(accounting.forced_sides))
    for entry in frame_diff.attribution[1:]:
        assert entry.factor_deltas == []  # 不伪造因果
        assert "不猜测因果" in entry.note


def test_pool_evidence_is_completed_into_log_idempotently():
    """采集到但没进帧的证据按 id 幂等补全；重复调用不产生重复条目。"""
    support = EvidenceEvent(
        id="e-support",
        date=D2,
        kind="expectation",
        description="上修",
        effect_on_thesis="confirming",
        confidence_delta=0.2,
    )
    extra = EvidenceEvent(
        id="e-extra",
        date=D2,
        kind="trend",
        description="相对强度新高",
        effect_on_thesis="neutral",
        confidence_delta=0.0,
    )
    curr = CompanyStateArtifact(symbol=SYMBOL, as_of_date=D2, evidence_log=[support])
    first = enforce_attribution_accounting(None, curr=curr, pool=[support, extra])
    assert [e.id for e in curr.evidence_log] == ["e-support", "e-extra"]
    enforce_attribution_accounting(None, curr=curr, pool=[support, extra])
    assert [e.id for e in curr.evidence_log] == ["e-support", "e-extra"]  # 幂等
    assert first.opposing_in_log == 0


def test_trigger_dedupe_keeps_distinct_payloads():
    """同帧合并两条来源时按 (kind,payload) 去重；payload 不同的一律保留。"""
    price = Trigger(
        kind=TriggerKind.PRICE_MOVE, symbol=SYMBOL, reason="r", payload={"source": "price_snapshot", "x": 1}
    )
    other = Trigger(kind=TriggerKind.PRICE_MOVE, symbol=SYMBOL, reason="r", payload={"source": "belief_drift"})
    assert _dedupe_triggers([price, price, other]) == [price, other]


# ── ④ CLI smoke + 看板 fail-open ────────────────────────────────────────────


def _patch_offline(monkeypatch, *, revenue_yoy: float = 12.0):
    """把默认 provider 替换为离线桩（CLI 路径不含网络/LLM 依赖）——patch **midterm 侧**。"""
    from alphabee.midterm import decision_model as md
    from alphabee.midterm import factors

    monkeypatch.setattr(
        factors,
        "get_factor_snapshot",
        lambda symbol, include_market=True: _snapshot(D2, revenue_yoy=revenue_yoy),
    )
    monkeypatch.setattr(md, "collect_evidence", lambda *a, **k: _evidence(D2))


def test_cli_smoke_json(tmp_path, monkeypatch, capsys):
    """CLI smoke：`main([...])` 返回 0、输出可解析 JSON、告警落盘路径指向 tmp。"""
    _patch_offline(monkeypatch)
    code = main(
        [
            "--symbol",
            SYMBOL,
            "--once",
            "--as-of",
            D2,
            "--state-dir",
            str(tmp_path / "state"),
            "--alert-dir",
            str(tmp_path / "alerts"),
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["symbol"] == SYMBOL and payload[0]["as_of"] == D2


def test_cli_text_mode_and_manual_trigger(tmp_path, monkeypatch, capsys):
    """文本模式 + `--trigger manual`：人工触发被显式记录（含 §9.4 说明）。"""
    _patch_offline(monkeypatch)
    code = main(
        [
            "--watchlist",
            SYMBOL,
            "--as-of",
            D2,
            "--state-dir",
            str(tmp_path / "state"),
            "--no-persist",
            "--trigger",
            "manual",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "[manual]" in out and SYMBOL in out


def test_cli_rejects_loop_as_named_non_goal(capsys):
    """★ 常驻循环是**具名非目标**：`--loop` 明确报错退出（2），不偷偷起后台进程。"""
    assert main(["--symbol", SYMBOL, "--loop"]) == 2
    out = capsys.readouterr().out
    assert "常驻循环是具名非目标" in out and "外部调度器" in out


def test_cli_requires_a_symbol(capsys):
    """无标的 → 用法错误退出码 2（不静默跑空）。"""
    assert main(["--once"]) == 2
    assert "至少需要一个 --symbol" in capsys.readouterr().out


def test_cli_no_persist_writes_nothing(tmp_path, monkeypatch):
    """`--no-persist`：只读预演 —— 帧与告警都不落盘，**含默认告警目录**也不写。

    （回归用例：早期版本在 `--no-persist` 下把 `alert_dir` 传成 None，于是落到了
    `data/tracking/alerts` 默认目录 —— "预演"偷偷写了东西，真实 CLI 冒烟当场暴露。）
    """
    _patch_offline(monkeypatch)
    default_alerts = tmp_path / "default_alerts"
    monkeypatch.setattr(scheduler_module, "default_alert_dir", lambda: default_alerts)
    assert main(["--symbol", SYMBOL, "--as-of", D2, "--state-dir", str(tmp_path / "state"), "--no-persist"]) == 0
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "alerts").exists()
    assert not default_alerts.exists(), "只读预演不得写默认告警目录"


def test_run_watchlist_is_fail_open_per_symbol(tmp_path):
    """看板：单个标的失败不影响其余（逐标的独立报告 + degraded 说明）。"""

    def _snapshot_provider(symbol: str) -> FactorSnapshot:
        if symbol == "000001.SZ":
            raise RuntimeError("数据源不可用")
        return _snapshot(D1, symbol=symbol)

    reports = run_watchlist(
        [SYMBOL, "000001.SZ"],
        as_of=D1,
        snapshot_provider=_snapshot_provider,
        evidence_provider=lambda symbol, **kwargs: [],
        state_dir=tmp_path / "state",
        alert_dir=tmp_path / "alerts",
        persist=False,
    )
    assert [r.symbol for r in reports] == [SYMBOL, "000001.SZ"]
    assert reports[0].degraded is False
    assert reports[1].degraded is True and "数据源不可用" in reports[1].degraded_reason


def test_report_kinds_property_is_stable():
    """报告 kinds 去重且保序（CLI/下游画像用）。"""
    report = run_once(SYMBOL, as_of=D1, persist=False, **_providers(D1, price_change_pct=9.0))
    assert report.kinds == list(dict.fromkeys(report.kinds))
    assert TriggerKind.PRICE_MOVE.value in report.kinds


def test_default_stale_after_days_constant_is_documented():
    """保鲜期缺省值有具名常量（可被 config 覆盖），且 > 0。"""
    assert DEFAULT_STALE_AFTER_DAYS > 0
    assert isinstance(DEFAULT_STALE_AFTER_DAYS, int)


def test_degraded_frame_is_alerted(tmp_path):
    """降级帧（reconcile 失败）**也要落警** —— 失败本身就是要被告知的事，不是噪声。"""

    def _boom(symbol: str) -> FactorSnapshot:
        raise RuntimeError("数据源不可用")

    report = run_once(
        SYMBOL,
        as_of=D2,
        state_dir=tmp_path / "state",
        alert_dir=tmp_path / "alerts",
        snapshot_provider=_boom,
        evidence_provider=lambda symbol, **kwargs: [],
    )
    assert report.degraded is True and "数据源不可用" in report.degraded_reason
    assert report.alerts_path
    lines = (tmp_path / "alerts" / f"{SYMBOL}.jsonl").read_text(encoding="utf-8").strip().splitlines()
    payload = json.loads(lines[-1])
    assert payload["degraded"] is True and payload["symbol"] == SYMBOL


# ── ⑤ ★ 复用而非重写：midterm 侧哨兵 / 必抛传导（调度器层） ─────────────────


def test_sentinel_monitor_triggers_changes_report(tmp_path, monkeypatch):
    """★ 行为探针（调度器层）：patch midterm 的 `monitor_triggers` ⇒ 报告随之变化。"""
    from alphabee.midterm import diff_consumers

    sentinel = type("_S", (), {"triggered": True, "triggers": ["tv_distance=0.777>0.001"]})()
    monkeypatch.setattr(diff_consumers, "monitor_triggers", lambda d, **kwargs: sentinel)
    reconcile(SYMBOL, as_of=D1, state_dir=tmp_path / "s", persist=True, **_providers(D1))
    report = run_once(
        SYMBOL, as_of=D2, state_dir=tmp_path / "s", alert_dir=tmp_path / "a", **_providers(D2, revenue_yoy=20.0)
    )
    assert report.monitor_reasons == ["tv_distance=0.777>0.001"]
    assert any(t.payload.get("reason") == "tv_distance=0.777>0.001" for t in report.triggers)


def test_sentinel_check_exit_changes_report(tmp_path, monkeypatch):
    """★ 行为探针（调度器层）：patch midterm 的 `check_exit` ⇒ 报告随之变化。"""
    from alphabee.midterm import diff_consumers

    sentinel = type("_S", (), {"should_exit": True, "reasons": ["SENTINEL_EXIT"]})()
    monkeypatch.setattr(diff_consumers, "check_exit", lambda d: sentinel)
    reconcile(SYMBOL, as_of=D1, state_dir=tmp_path / "s", persist=True, **_providers(D1))
    report = run_once(
        SYMBOL, as_of=D2, state_dir=tmp_path / "s", alert_dir=tmp_path / "a", **_providers(D2, revenue_yoy=20.0)
    )
    assert report.exit_reasons == ["SENTINEL_EXIT"]
    assert any(t.payload.get("sources") == ["check_exit"] for t in report.triggers)


def test_raising_midterm_probe_two_level_semantics(tmp_path, monkeypatch):
    """★ 反向探针（必抛）：`reconcile` **不吞异常**（库层立即失败）；`run_once` 层 fail-open 降级。

    两层语义是刻意的：库函数不做容错决策，调度/CLI 入口必须"数据源抖动也不炸"（§14.0）。
    """
    from alphabee.midterm import diff_consumers

    def _boom(d, **kwargs):
        raise RuntimeError("midterm monitor exploded")

    reconcile(SYMBOL, as_of=D1, state_dir=tmp_path / "s", persist=True, **_providers(D1))
    monkeypatch.setattr(diff_consumers, "monitor_triggers", _boom)
    with pytest.raises(RuntimeError, match="midterm monitor exploded"):
        reconcile(SYMBOL, as_of=D2, state_dir=tmp_path / "s", persist=False, **_providers(D2))
    report = run_once(
        SYMBOL, as_of=D2, state_dir=tmp_path / "s", alert_dir=tmp_path / "a", persist=False, **_providers(D2)
    )
    assert report.degraded is True and "midterm monitor exploded" in report.degraded_reason
    assert report.blocked_actions == [] and report.escalation_tier == 0  # fail-open ≠ 越权执行


# ── ⑥ ★ 安全红线：真实 action 实例 + 对抗输入 + 变异非真空 + 无执行路径 ─────


def test_require_human_confirm_false_for_real_action_types():
    """★ 对两类**真实**行动类实例严格 `is False`（`0`/`None`/`""` 过不了这条断言）。"""

    transition = StateTransition(from_state="S2", to_state="S1", kind="downgrade")
    position = PositionDecision(
        portfolio_exposure=0.6,
        stock_weight=0.05,
        actual_weight=0.08,
        position_band="S2",
        rationale=["状态降级 → 降仓"],
    )
    for action in (transition, position):
        assert require_human_confirm(action) is False


@pytest.mark.parametrize(
    "action",
    [
        None,
        "state_transition:downgrade:S5→S2",
        {"kind": "position_change", "target": 0.1},
        {"confirmed": True, "confirmed_by": "human"},  # 伪造"UI 已确认"
        object(),
        0,
        "",
        False,
    ],
)
def test_require_human_confirm_false_against_adversarial_inputs(action):
    """对抗性输入（含伪造"UI 已确认"字典 / 类 Tier 0 描述）仍然 `is False`。"""
    assert require_human_confirm(action) is False


def test_require_human_confirm_ignores_environment_overrides(monkeypatch):
    """不给"环境变量/伪造字段"留后门：设了自动执行类环境变量也仍然 `is False`。"""
    for name in ("ALPHABEE_AUTO_EXECUTE", "ALPHABEE_HUMAN_CONFIRMED", "AUTO_EXECUTE"):
        monkeypatch.setenv(name, "1")
    assert require_human_confirm("state_transition:downgrade:S5→S2") is False


def test_gate_is_actually_consulted_mutation(monkeypatch):
    """★ 变异非真空：把放行口猴补成 `True` ⇒ `blocked_actions` 必须变空、`escalation_tier` 必须变 0。

    若这条不成立，说明 §9.4 的 gate 只是"没被接线"的摆设（红线失效却无人发现）。
    """
    actions = ["state_transition:downgrade:S5→S2"]
    blocked, tier = _gate_actions(actions)
    assert blocked == actions and tier == 5  # v1：全部拦下（Tier 5）

    monkeypatch.setattr(scheduler_module, "require_human_confirm", lambda action: True)
    blocked2, tier2 = _gate_actions(actions)
    assert blocked2 == [] and tier2 == 0  # 放行后：无拦下、Tier 0


def test_no_auto_execution_path_in_tracking():
    """★ 全域 AST 扫描：v1 不存在"自动执行行动类输出"的代码路径（只允许产出/告警/待人工确认）。"""
    import ast as _ast
    import pathlib as _pathlib

    forbidden_calls = {
        "submit_order",
        "place_order",
        "execute",
        "execute_action",
        "trade",
        "buy",
        "sell",
        "apply_position",
        "auto_execute",
    }
    paths = sorted(_pathlib.Path(scheduler_module.__file__).parent.glob("*.py"))
    assert paths, "未扫到 tracking 源文件"
    for path in paths:
        source = path.read_text(encoding="utf-8")
        tree = _ast.parse(source)
        called: set[str] = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Call):
                func = node.func
                called.add(func.attr if isinstance(func, _ast.Attribute) else getattr(func, "id", ""))
        assert not (called & forbidden_calls), f"{path.name} 出现行动执行类调用：{sorted(called & forbidden_calls)}"
    # 红线放行口必须在包内真的被用（定义 + 调用 ≥ 2 处），且只有 scheduler.py 负责判定
    total = sum(
        p.read_text(encoding="utf-8").count("require_human_confirm(")
        for p in _pathlib.Path(scheduler_module.__file__).parent.glob("*.py")
    )
    assert total >= 2, "红线放行口未被调用（gate 是摆设）"


def test_tracking_does_not_touch_node_contracts_or_detectors():
    """★ F4 不接节点契约：`NODE_CONTRACTS` **键集合指纹**与 `DETECTORS` 键集合不变 + 契约层零违规。"""
    import hashlib

    from alphabee.orchestrator.detectors import DETECTORS
    from alphabee.orchestrator.node_contracts import NODE_CONTRACTS, validate_contracts

    assert len(NODE_CONTRACTS) == 16
    fingerprint = hashlib.sha256("|".join(sorted(NODE_CONTRACTS)).encode()).hexdigest()[:16]
    assert fingerprint == "90bd2190b86f1186"  # 键集合指纹（count 相等但键被换位也抓得到）
    assert sorted(DETECTORS) == [
        "artifact_schema_valid",
        "assumption_still_valid",
        "derived_facts_nonempty",
        "downstream_inputs_present",
        "evidence_refs_present",
        "insight_artifacts_present",
    ]
    assert validate_contracts() == []


# ── ② F4-G2 / F4-P3：审查侧已实跑通过、现钉进仓库测试（防静默回退） ─────────


def test_require_human_confirm_is_false_for_real_action_models():
    """★ F4-G2：两类**真实** midterm 行动类模型实例（非替身）同样恒 `False`。

    审查侧曾以这两类实跑过（`alphabee/midterm/models.py:103 StateTransition` /
    `:514 PositionDecision`）——本用例把该证据**钉进仓库测试**：仅覆盖 `None`/`str`/`dict`
    之类替身，无法防住"对此后新增的真实行动类悄悄放行"。
    """
    transition = StateTransition(from_state="S5", to_state="S2", kind="downgrade")
    decision = PositionDecision(position_band="hold", stock_weight=0.0)

    assert require_human_confirm(transition) is False
    assert require_human_confirm(decision) is False


def test_require_human_confirm_ignores_ui_confirmation_claims(monkeypatch):
    """★ F4-G2（对抗性输入）：伪造「UI 已确认」字段 / 审批环境变量 / auto_execute 都不得放行。

    v1 没有任何"人工确认"通道 ⇒ 任何自称已确认的载荷都必须仍被拦下（§9.4 红线）。
    """
    monkeypatch.setenv("ALPHABEE_HUMAN_CONFIRMED", "1")
    monkeypatch.setenv("DSH_APPROVED", "true")

    forged = {
        "kind": "state_transition",
        "ui_confirmed": True,
        "confirmed_by": "human",
        "approved": True,
        "auto_execute": True,
    }
    assert require_human_confirm(forged) is False
    assert require_human_confirm(StateTransition(from_state="S5", to_state="S1", legal=True)) is False


def test_action_gate_reads_require_human_confirm(monkeypatch):
    """★ F4-G2（**变异非真空**）：把 `require_human_confirm` 猴补成恒 `True` ⇒ gate 必须随之放行。

    只断言"该函数恒 False"**无法证明 gate 真读它**（可能只是未被接线的摆设）。本用例钉住
    「gate 入口 → 该函数」这条接线：变异后 `blocked` 必须变空、档位必须回到 Tier 0。
    """
    actions = ["state_transition:downgrade:S5→S2"]
    blocked, tier = _gate_actions(actions)
    assert blocked == actions and tier == 5, "原状：行动类输出必须被拦下并升级人工"

    monkeypatch.setattr(scheduler_module, "require_human_confirm", lambda action: True)
    unblocked, tier_after = _gate_actions(actions)
    assert unblocked == [] and tier_after == 0, "变异后若仍拦截，说明 gate 并未真正读该函数"


def test_f4_does_not_touch_node_contracts():
    """★ F4-P3：F4 不接节点契约 —— 用**完整键集合 + 指纹**机读锚（仅 count 抓不到键被换位）。"""
    from alphabee.orchestrator.detectors import DETECTORS
    from alphabee.orchestrator.node_contracts import NODE_CONTRACTS, validate_contracts

    expected = [
        "collect_raw_facts",
        "explore_conflicts",
        "finalize_message",
        "generate_report",
        "midterm_decision_reporter",
        "record_deviations",
        "resolve_company_track",
        "resolve_driver_profile",
        "resolve_industry_context",
        "resolve_midterm_decision",
        "review_report",
        "review_thesis",
        "run_analysis_engines",
        "run_thesis",
        "synthesize_insights",
        "verify_hypotheses",
    ]
    assert sorted(NODE_CONTRACTS) == expected, "F4 不得新增/删除/替换任何节点契约键"
    assert hashlib.sha256("|".join(expected).encode()).hexdigest()[:16] == "90bd2190b86f1186"
    assert len(DETECTORS) == 6
    assert validate_contracts() == []
