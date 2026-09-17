"""F4 触发器测试（§9.2 / §9.3 / §14.5-A）。

覆盖：**五类 kind 各一例**、阈值**边界**、**纯函数性 / 无副作用**、
配置 **fail-open**（读 config 但缺失/异常回落 midterm 自己的默认阈值）、
`Trigger` frozen 语义、`TriggerKind` **不扩枚举**；以及**复用而非重写**的可证伪判据：
**哨兵传导**（midterm 侧 patch 成哨兵 ⇒ 本模块输出随之变化）、**必抛传导**、
**无第二份实现**（AST/文本反向扫描）、**阈值不落第二份字面量**。
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import pathlib

import pytest

from alphabee.midterm import diff_consumers
from alphabee.midterm.models import (
    ArtifactRef,
    CompanyStateArtifact,
    CompanyStateDiff,
    ExitCondition,
    FactorSnapshot,
    FundamentalFactor,
    PositionDiff,
    RiskFactor,
    StateBelief,
    StateShift,
    TrendFactor,
)
from alphabee.tracking import triggers as triggers_module
from alphabee.tracking.triggers import (
    DEFAULT_PRICE_MOVE_PCT,
    Trigger,
    TriggerKind,
    TriggerThresholds,
    detect_fact_triggers,
    detect_triggers,
    manual_trigger,
    monitor_kwargs,
    thresholds_from_settings,
)

SYMBOL = "600519.SH"


# ── 构造器 ──────────────────────────────────────────────────────────────────


def _snapshot(
    *, as_of: str = "2026-09-18", revenue_yoy: float = 12.0, price_change_pct: float = 1.0, news: str = ""
) -> FactorSnapshot:
    return FactorSnapshot(
        symbol=SYMBOL,
        as_of_date=as_of,
        fundamental=FundamentalFactor(revenue_yoy=revenue_yoy, net_profit_yoy=8.0),
        trend=TrendFactor(price_change_pct=price_change_pct),
        risk=RiskFactor(news_title=news),
    )


def _artifact(
    *,
    as_of: str = "2026-09-18",
    stale_after: str | None = None,
    exit_met: bool = False,
    snapshot: FactorSnapshot | None = None,
) -> CompanyStateArtifact:
    artifact = CompanyStateArtifact(symbol=SYMBOL, thesis="核心假设", as_of_date=as_of, stale_after=stale_after)
    artifact.state = StateBelief(distribution={"S0": 1.0}, argmax_state="S0", entropy=0.0)
    if exit_met:
        artifact.exit_conditions = [ExitCondition(kind="thesis_broken", condition="现金流连续两季低于净利", met=True)]
    artifact.factor_snapshot = snapshot
    return artifact


def _frame_diff(*, tv: float = 0.0, downgrade: bool = False, divergence: bool = False, elapsed_days: int = 19):
    """造一个真实 `CompanyStateDiff`（用于驱动 check_exit / monitor_triggers 的**委托**路径）。"""
    shift = StateShift(argmax_from="S2", argmax_to="S1", tv_distance=tv, kind="downgrade" if downgrade else "same")
    return CompanyStateDiff(
        symbol=SYMBOL,
        prev=None,
        curr=ArtifactRef(id=f"{SYMBOL}:2026-09-18", date="2026-09-18", symbol=SYMBOL),
        elapsed_days=elapsed_days,
        state_shift=shift,
        position=PositionDiff(band_weight_divergence=divergence) if divergence else None,
        confidence=None,
    )


# ── ① 五类 kind 各一例 ──────────────────────────────────────────────────────


def test_kind_financial_report_from_fundamental_refresh():
    """FINANCIAL_REPORT：财报类字段相对上一帧实质变化（代理判定新报告期/财报刷新）。"""
    found = detect_fact_triggers(
        SYMBOL,
        snapshot=_snapshot(as_of="2026-09-30", revenue_yoy=12.0),
        prev_snapshot=_snapshot(as_of="2026-06-30", revenue_yoy=5.0),
    )
    assert [t.kind for t in found] == [TriggerKind.FINANCIAL_REPORT]
    assert found[0].payload["changed_fields"] == ["revenue_yoy"]
    assert found[0].payload["source"] == "fundamental_refresh_proxy"  # 代理判定必须自证


def test_kind_announcement_from_news_title_change():
    """ANNOUNCEMENT：``risk.news_title`` 非空且与上一帧不同（代理判定）。"""
    found = detect_fact_triggers(
        SYMBOL, snapshot=_snapshot(news="公司公告：拟回购不超过 10 亿元"), prev_snapshot=_snapshot(news="")
    )
    assert [t.kind for t in found] == [TriggerKind.ANNOUNCEMENT]
    assert found[0].payload["news_title"].startswith("公司公告")


def test_kind_price_move_from_snapshot_percent():
    """PRICE_MOVE（行情侧）：``|price_change_pct| > 阈值``。"""
    found = detect_fact_triggers(SYMBOL, snapshot=_snapshot(price_change_pct=-7.5), prev_snapshot=_snapshot())
    assert [t.kind for t in found] == [TriggerKind.PRICE_MOVE]
    assert found[0].payload == {
        "source": "price_snapshot",
        "price_change_pct": -7.5,
        "threshold": DEFAULT_PRICE_MOVE_PCT,
    }


def test_kind_price_move_from_belief_drift_comes_from_monitor_triggers():
    """PRICE_MOVE（信念位移侧）：**由 midterm 的 `monitor_triggers` 判定**，本模块只做投影。"""
    found = detect_triggers(SYMBOL, as_of="2026-09-20", artifact=_artifact(), frame_diff=_frame_diff(tv=0.4))
    assert [t.kind for t in found] == [TriggerKind.PRICE_MOVE]
    payload = found[0].payload
    assert payload["source"] == "monitor_triggers"  # 判定来源自证 = midterm
    assert payload["reason"].startswith("tv_distance=")  # 原样透传 midterm 的 reason
    assert payload["threshold"] is None  # 缺省不传阈值 ⇒ 用 monitor_triggers 自己的默认


def test_kind_stale_expired():
    """STALE_EXPIRED：``stale_after`` 已过期。"""
    found = detect_triggers(SYMBOL, as_of="2026-09-20", artifact=_artifact(stale_after="2026-09-10"))
    assert [t.kind for t in found] == [TriggerKind.STALE_EXPIRED]
    assert found[0].payload == {
        "stale_after": "2026-09-10",
        "as_of": "2026-09-20",
        "overdue_days": 10,
        "grace_days": 0,
    }


def test_kind_manual_from_check_exit_delegation():
    """MANUAL（退出侧）：**由 midterm 的 `check_exit` 判定**（三条分支一条都不复制）。"""
    for keyword in ({"downgrade": True}, {"divergence": True}):
        found = detect_triggers(SYMBOL, as_of="2026-09-20", artifact=_artifact(), frame_diff=_frame_diff(**keyword))
        manual = [t for t in found if t.kind is TriggerKind.MANUAL]
        assert manual and "check_exit" in manual[0].payload["sources"]
        assert manual[0].payload["reasons"]  # midterm 给的退出原因原样带上


def test_kind_manual_single_frame_fallback_reads_artifact_field():
    """MANUAL：``exit_conditions[*].met`` 作"持续告警"来源（与 check_exit 合并为一条）。"""
    found = detect_triggers(SYMBOL, as_of="2026-09-20", artifact=_artifact(exit_met=True))
    assert [t.kind for t in found] == [TriggerKind.MANUAL]
    assert found[0].payload["sources"] == ["artifact_exit_conditions"]


def test_kind_manual_via_explicit_cli_trigger():
    """MANUAL（人工注入）：CLI 显式触发不属于任何自动判定，单独构造。"""
    trigger = manual_trigger(SYMBOL, reason="人工触发：复核论点")
    assert trigger.kind is TriggerKind.MANUAL and trigger.payload["source"] == "manual"


def test_all_five_kinds_are_covered():
    """五类 kind 全覆盖（防"枚举里有一类永远产不出来"的死枚举）。"""
    produced = {
        *(
            t.kind
            for t in detect_triggers(
                SYMBOL,
                as_of="2026-09-20",
                artifact=_artifact(stale_after="2026-09-01", exit_met=True),
                frame_diff=_frame_diff(tv=0.9, downgrade=True),
            )
        ),
        *(
            t.kind
            for t in detect_fact_triggers(
                SYMBOL,
                snapshot=_snapshot(price_change_pct=9.9, news="公告"),
                prev_snapshot=_snapshot(revenue_yoy=1.0),
            )
        ),
    }
    assert produced == set(TriggerKind)


def test_trigger_kind_enum_is_not_expanded():
    """`TriggerKind` 恰为 §14.5-A 的五种（**不扩枚举**：扩张属行为变更，须走 ROADMAP）。"""
    assert [kind.value for kind in TriggerKind] == [
        "financial_report",
        "announcement",
        "price_move",
        "stale_expired",
        "manual",
    ]


# ── ② 阈值边界 ──────────────────────────────────────────────────────────────


def test_tv_threshold_boundary_uses_midterm_default():
    """tv 边界用**midterm 自己的默认阈值**：等于不触发、略高于触发（本模块不掺和阈值）。"""
    at_limit = diff_consumers._TV_TRIGGER  # 仅测试读取 midterm 的值；tracking 源码里不出现该符号
    equal = detect_triggers(SYMBOL, as_of="2026-09-20", artifact=_artifact(), frame_diff=_frame_diff(tv=at_limit))
    assert equal == []
    above = detect_triggers(
        SYMBOL, as_of="2026-09-20", artifact=_artifact(), frame_diff=_frame_diff(tv=at_limit + 0.02)
    )
    assert [t.kind for t in above] == [TriggerKind.PRICE_MOVE]


def test_tv_threshold_override_is_passed_through():
    """显式覆盖阈值时**传参**给 monitor_triggers（委托而非自判）。"""
    limits = TriggerThresholds(tv_distance=0.05)
    found = detect_triggers(
        SYMBOL, as_of="2026-09-20", artifact=_artifact(), frame_diff=_frame_diff(tv=0.1), thresholds=limits
    )
    assert [t.kind for t in found] == [TriggerKind.PRICE_MOVE]
    assert found[0].payload["threshold"] == 0.05


def test_monitor_kwargs_omits_default_threshold():
    """★ 硬要求：阈值缺省时**不传参** ⇒ 用 `monitor_triggers` 自己的默认常量（无第二份字面量）。"""
    assert monitor_kwargs(TriggerThresholds()) == {}
    assert monitor_kwargs(TriggerThresholds(tv_distance=0.42)) == {"tv_threshold": 0.42}


def test_price_move_boundary_is_strict():
    """行情阈值开区间：等于不触发、超过才触发。"""
    at_threshold = detect_fact_triggers(
        SYMBOL, snapshot=_snapshot(price_change_pct=DEFAULT_PRICE_MOVE_PCT), prev_snapshot=_snapshot()
    )
    above = detect_fact_triggers(
        SYMBOL, snapshot=_snapshot(price_change_pct=DEFAULT_PRICE_MOVE_PCT + 0.01), prev_snapshot=_snapshot()
    )
    assert at_threshold == []
    assert [t.kind for t in above] == [TriggerKind.PRICE_MOVE]


def test_stale_boundary_and_grace_window():
    """stale 边界：`as_of == stale_after` 不触发；过期 1 天触发；宽限期可延后触发点。"""
    artifact = _artifact(stale_after="2026-09-20")
    assert detect_triggers(SYMBOL, as_of="2026-09-20", artifact=artifact) == []
    assert [t.kind for t in detect_triggers(SYMBOL, as_of="2026-09-21", artifact=artifact)] == [
        TriggerKind.STALE_EXPIRED
    ]
    grace = TriggerThresholds(stale_grace_days=3)
    assert detect_triggers(SYMBOL, as_of="2026-09-23", artifact=artifact, thresholds=grace) == []
    assert [t.kind for t in detect_triggers(SYMBOL, as_of="2026-09-24", artifact=artifact, thresholds=grace)] == [
        TriggerKind.STALE_EXPIRED
    ]


# ── ③ ★ 复用而非重写：哨兵传导 / 必抛传导 / 无第二份实现 ─────────────────────


def test_sentinel_probe_check_exit_changes_output(monkeypatch):
    """★ 行为探针（退出侧）：把 midterm 的 `check_exit` patch 成哨兵 ⇒ 本模块输出**随之变化**。"""

    class _Sentinel:
        should_exit = True
        reasons = ["SENTINEL_EXIT"]

    monkeypatch.setattr(diff_consumers, "check_exit", lambda d: _Sentinel())
    found = detect_triggers(SYMBOL, as_of="2026-09-20", artifact=_artifact(), frame_diff=_frame_diff())
    manual = [t for t in found if t.kind is TriggerKind.MANUAL]
    assert manual and manual[0].payload["reasons"] == ["SENTINEL_EXIT"]
    assert "check_exit" in manual[0].payload["sources"]


def test_sentinel_probe_monitor_triggers_changes_output(monkeypatch):
    """★ 行为探针（监控侧）：把 `monitor_triggers` patch 成哨兵 ⇒ 本模块输出**随之变化**。"""

    class _Sentinel:
        triggered = True
        triggers = ["tv_distance=0.999>0.001", "evidence_arrival_rate=9.999>0.5"]

    monkeypatch.setattr(diff_consumers, "monitor_triggers", lambda d, **kwargs: _Sentinel())
    found = detect_triggers(SYMBOL, as_of="2026-09-20", artifact=_artifact(), frame_diff=_frame_diff())
    kinds = [(t.kind, t.payload["reason"]) for t in found]
    assert (TriggerKind.PRICE_MOVE, "tv_distance=0.999>0.001") in kinds
    assert (TriggerKind.MANUAL, "evidence_arrival_rate=9.999>0.5") in kinds


def test_raising_probe_propagates_from_triggers(monkeypatch):
    """★ 反向探针：midterm 侧改成"必抛" ⇒ 本模块**随之失败**（没有绕过、没有吃掉异常）。"""

    def _boom(d, **kwargs):
        raise RuntimeError("midterm monitor exploded")

    monkeypatch.setattr(diff_consumers, "monitor_triggers", _boom)
    with pytest.raises(RuntimeError, match="midterm monitor exploded"):
        detect_triggers(SYMBOL, as_of="2026-09-20", artifact=_artifact(), frame_diff=_frame_diff())


def _code_only(path: pathlib.Path) -> str:
    """剥掉**全部 docstring** 后的代码文本（反向扫描必须只看代码，不误伤解释性文字）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_no_duplicate_implementation_in_tracking():
    """★ 反向清单：tracking **代码面**内不得出现 midterm 逻辑的复制体（常量 / 公式 / 分支）。"""
    paths = sorted(pathlib.Path(triggers_module.__file__).parent.glob("*.py"))
    assert paths, "未扫到 tracking 源文件，扫描口径需与代码同步"
    for path in paths:
        code = _code_only(path)
        assert "0.3" not in code, f"{path.name} 出现 tv 阈值字面量（应走 midterm 默认）"
        assert "_TV_TRIGGER" not in code, f"{path.name} 复制了 midterm 的阈值常量名"
        assert "sum(abs(" not in code, f"{path.name} 重算了 tv 公式（应委托 monitor_triggers）"
        assert "exit_conditions_met" not in code, f"{path.name} 复制了 check_exit 的分支字段"
        assert "band_weight_divergence" not in code, f"{path.name} 复制了 check_exit 的仓位分支"


def test_ast_calls_resolve_to_midterm_engines():
    """符号级调用点解析：本模块对 diff 消费方的调用**必须**指向 `diff_consumers` 属性。"""
    tree = ast.parse(pathlib.Path(triggers_module.__file__).read_text(encoding="utf-8"))
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "diff_consumers"
    ]
    assert calls == ["check_exit", "monitor_triggers"]
    # 且**不得**以 import 期绑定名调用（否则哨兵探针会失效）
    assert not [name for name in ("check_exit", "monitor_triggers") if hasattr(triggers_module, name)]


# ── ④ 纯函数性 / 无副作用 ───────────────────────────────────────────────────


def test_detectors_are_pure_and_side_effect_free(tmp_path, monkeypatch):
    """纯函数：两次结果相同；入参 artifact 未被子修改；运行期不产生任何文件。"""
    artifact = _artifact(stale_after="2026-09-01", exit_met=True, snapshot=_snapshot(price_change_pct=8.0))
    before = artifact.model_dump(mode="json")
    monkeypatch.chdir(tmp_path)
    frame = _frame_diff(tv=0.5)
    first = detect_triggers(SYMBOL, as_of="2026-09-20", artifact=artifact, frame_diff=frame)
    second = detect_triggers(SYMBOL, as_of="2026-09-20", artifact=artifact, frame_diff=frame)
    assert first == second
    assert artifact.model_dump(mode="json") == before  # 未改入参
    assert list(tmp_path.rglob("*")) == []  # 无落盘/无临时文件


def test_detect_triggers_source_has_no_io_primitives():
    """静态证据：检测函数源码里不出现 IO/LLM/时钟原语（纯函数是**契约**，不只是习惯）。"""
    for func in (detect_triggers, detect_fact_triggers):
        source = inspect.getsource(func)
        for forbidden in ("open(", "Path(", "datetime.now", "date.today", "get_settings", "requests"):
            assert forbidden not in source, f"{func.__name__} 不应出现 {forbidden!r}"


def test_missing_inputs_produce_no_triggers():
    """缺失即不猜测：``artifact=None`` → 无触发；缺 ``stale_after`` → 不触发；非法日期 → 不触发。"""
    assert detect_triggers(SYMBOL, as_of="2026-09-20", artifact=None) == []
    assert detect_triggers(SYMBOL, as_of="2026-09-20", artifact=_artifact()) == []
    assert detect_triggers(SYMBOL, as_of="not-a-date", artifact=_artifact(stale_after="2026-09-01")) == []
    # 无 diff ⇒ tv 不判定（不自算公式），单帧只做 stale/exit/price 三项
    frames = detect_triggers(SYMBOL, as_of="2026-09-20", artifact=_artifact())
    assert all(t.kind is not TriggerKind.PRICE_MOVE for t in frames)


def test_frame_diff_absent_keeps_the_contract_signature_usable():
    """契约签名（不传 frame_diff）仍可用：委托项缺失即不判，其余三类照常。"""
    artifact = _artifact(stale_after="2026-09-01", exit_met=True, snapshot=_snapshot(price_change_pct=9.0))
    kinds = [t.kind for t in detect_triggers(SYMBOL, as_of="2026-09-20", artifact=artifact)]
    assert kinds == [TriggerKind.STALE_EXPIRED, TriggerKind.MANUAL, TriggerKind.PRICE_MOVE]


# ── ⑤ 配置 fail-open ────────────────────────────────────────────────────────


def test_thresholds_fail_open_when_config_unavailable(monkeypatch):
    """读配置失败 → 回落 midterm 默认（tv 档 = None = 不传参），绝不抛错。"""

    def _boom():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr("alphabee.config.get_settings", _boom)
    limits = thresholds_from_settings()
    assert limits.tv_distance is None and monitor_kwargs(limits) == {}
    assert limits.price_move_pct == DEFAULT_PRICE_MOVE_PCT


def test_thresholds_default_section_absent(monkeypatch):
    """`DeviationSettings` 当前无 `tracking` 段 → 缺段同样 fail-open（而不是 AttributeError）。"""
    from alphabee.config import DeviationSettings

    class _Settings:
        deviation = DeviationSettings()

    monkeypatch.setattr("alphabee.config.get_settings", lambda: _Settings())
    assert thresholds_from_settings() == TriggerThresholds()


def test_thresholds_read_config_when_section_present(monkeypatch):
    """配置段存在且给了字段 → 字段生效（证明"阈值读 config"不是一句空话）。"""

    class _Tracking:
        tv_distance = 0.05
        price_move_pct = 9.5
        stale_grace_days = 2
        stale_after_days = 30

    class _Deviation:
        tracking = _Tracking()

    class _Settings:
        deviation = _Deviation()

    monkeypatch.setattr("alphabee.config.get_settings", lambda: _Settings())
    limits = thresholds_from_settings()
    assert (limits.tv_distance, limits.price_move_pct, limits.stale_grace_days, limits.stale_after_days) == (
        0.05,
        9.5,
        2,
        30,
    )
    assert monitor_kwargs(limits) == {"tv_threshold": 0.05}

    class _Bad:
        tv_distance = True
        price_move_pct = float("nan")
        stale_grace_days = -1
        stale_after_days = 0

    class _Dev2:
        tracking = _Bad()

    class _Settings2:
        deviation = _Dev2()

    monkeypatch.setattr("alphabee.config.get_settings", lambda: _Settings2())
    assert thresholds_from_settings() == TriggerThresholds()


# ── ⑥ 数据结构语义 ──────────────────────────────────────────────────────────


def test_trigger_is_frozen_dataclass():
    """`Trigger` 是 frozen dataclass（可比较、可入 set、下游改不动）。"""
    trigger = Trigger(kind=TriggerKind.MANUAL, symbol=SYMBOL, reason="r", payload={"a": 1})
    assert dataclasses.is_dataclass(trigger)
    with pytest.raises(dataclasses.FrozenInstanceError):
        trigger.reason = "changed"  # type: ignore[misc]
    assert hash(trigger)
    assert trigger == Trigger(kind=TriggerKind.MANUAL, symbol=SYMBOL, reason="r", payload={"a": 1})


def test_module_has_no_import_time_config_read():
    """模块级**不读配置**：`triggers` 模块源码不含模块级 `get_settings()` 调用。"""
    source = pathlib.Path(triggers_module.__file__).read_text(encoding="utf-8")
    body_lines = [ln for ln in source.splitlines() if ln and not ln.startswith((" ", "\t", "#", '"""', "'''"))]
    assert not [ln for ln in body_lines if "get_settings()" in ln]
