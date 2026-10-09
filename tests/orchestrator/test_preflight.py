"""P2（D2-B2）入口前置校验测试：``services/preflight.py`` + ``collectors`` 记账接线 + CLI 两路径
（``docs/design/RESEARCH_CONTINUUM_DESIGN.md`` §15.2-E / §15.8 P2 行）。

覆盖 §15.8 P2 的四条**不可省**断言：

1. **无帧不阻断**：``checked=False`` 且 ``blocking=False``（首次研究必须放行）；
2. **陈旧/触发 → ``blocking``**：帧 ``stale_after`` 到期，或告警末行有未消费触发；
3. **CLI 两路径**：``--allow-stale`` 放行（且**仍命中**）/ 缺省拒绝（``SystemExit(3)``）；
4. **``stale_state_run`` 入账**：节点级跑 ``prepare_analysis_context`` 产 D5/MEDIUM issue 且采集继续。

外加 §15.2-E 的其余行（告警损坏行只跳过、配置缺段仍可 import、``block_stale_runs=false`` 只记录
不阻断）与两条口径钉子（``days_since`` = 帧龄、``stale_after == today`` 不算陈旧）。

纪律：除"必须打桩的外部依赖"（LLM agent / 结构化事实模型 / 配置读取）之外**不 mock**——帧用真实
写入器 ``midterm.persistence.append_artifact`` 落盘、告警行用真实 ``TrackingReport.model_dump``
落盘、判定走真实 ``check_preflight``；重 import 链（``collectors`` / ``apps.cli.main``，
会带起 ``tushare.set_token``）一律**在用例内**延迟 import，保持本文件在任意 HOME 下都能被收集。
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from alphabee.config import (
    ConfigLoader,
    DeviationSettings,
    DeviationTrackingSettings,
    Settings,
)
from alphabee.core import DeviationClass, IssueScope, IssueSeverity, StepStatus
from alphabee.midterm.models import (
    ArtifactRef,
    CompanyStateArtifact,
    CompanyStateDiff,
    StateShift,
)
from alphabee.midterm.persistence import append_artifact
from alphabee.orchestrator.services import preflight as pf
from alphabee.orchestrator.services.deviation import CLASS_BY_CATEGORY
from alphabee.tracking.scheduler import DEFAULT_ALERT_DIR as TRACKING_ALERT_DIR
from alphabee.tracking.scheduler import DEFAULT_STALE_AFTER_DAYS, TrackingReport
from alphabee.tracking.triggers import Trigger, TriggerKind

REPO_ROOT = Path(pf.__file__).resolve().parents[3]
SYMBOL = "600519.SH"
#: 用**公司名**而非裸代码：``extract_symbols_from_query`` 的裸代码分支要求代码出现在
#: ``static/all_stocks.csv`` 的映射里，公司名分支才是该函数在真实查询上的常规入口。
QUERY = "分析一下贵州茅台"


# ── 构造器（真实写入器 / 真实模型，不手写 JSON 形状） ────────────────────────


def _frame(
    state_dir: Path,
    *,
    symbol: str = SYMBOL,
    as_of: str = "2026-09-01",
    stale_after: str | None = "2026-09-08",
) -> CompanyStateArtifact:
    """用真实写入器落一帧（``data/midterm/state/<symbol>.jsonl``），返回该帧。"""
    artifact = CompanyStateArtifact(symbol=symbol, as_of_date=as_of, stale_after=stale_after, thesis="核心假设")
    append_artifact(artifact, state_dir)
    return artifact


def _trigger(kind: TriggerKind = TriggerKind.PRICE_MOVE, reason: str = "行情异动：+7.50% 绝对值超阈值 5%") -> Trigger:
    return Trigger(kind=kind, symbol=SYMBOL, reason=reason, payload={"source": "price_snapshot"})


def _alert_row(
    *,
    symbol: str = SYMBOL,
    as_of: str = "2026-09-01",
    triggers: tuple[Trigger, ...] = (),
    exit_reasons: tuple[str, ...] = (),
    monitor_reasons: tuple[str, ...] = (),
    blocked_actions: tuple[str, ...] = (),
) -> dict[str, Any]:
    """一行真实告警（``_write_alerts`` 落盘的就是 ``TrackingReport.model_dump(mode="json")``）。"""
    return TrackingReport(
        symbol=symbol,
        as_of=as_of,
        triggers=list(triggers),
        exit_reasons=list(exit_reasons),
        monitor_reasons=list(monitor_reasons),
        blocked_actions=list(blocked_actions),
        escalation_tier=5 if blocked_actions else 0,
    ).model_dump(mode="json")


def _write_alert_lines(alert_dir: Path, rows: list[Any], *, symbol: str = SYMBOL) -> Path:
    """按行写告警文件；``str`` 元素原样写入（用于坏行），``dict`` 走 JSON 序列化。"""
    alert_dir.mkdir(parents=True, exist_ok=True)
    path = alert_dir / f"{symbol}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(row if isinstance(row, str) else json.dumps(row, ensure_ascii=False))
            fh.write("\n")
    return path


def _tree_snapshot(root: Path) -> dict[str, str]:
    """目录内容的可比较快照（相对路径 → sha256）：用于"只读、零写入"钉子。"""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _load_args_module() -> Any:
    """按**文件路径**加载 ``apps/cli/args.py``。

    不能 ``import alphabee.apps.cli.args``：包 ``__init__`` 会 import ``main`` → ``orchestrator.agent``
    → ``collectors`` → ``tushare.set_token``（写 ``$HOME/tk.csv``）；沿用 ``test_telemetry.py`` 的既有写法，
    让参数解析用例不依赖那个重链。
    """
    path = REPO_ROOT / "alphabee" / "apps" / "cli" / "args.py"
    spec = importlib.util.spec_from_file_location("p2_cli_args_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parsed_args(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> Any:
    """真跑 ``parse_args()``（``sys.argv`` 注入），返回 Namespace。"""
    module = _load_args_module()
    monkeypatch.setattr(sys, "argv", ["main.py", *argv])
    return module.parse_args()


# ── ① 签名 / 纯规则 / 不新增存储（§15.2-A） ─────────────────────────────────


def test_verdict_fields_are_the_spec_ten_in_order():
    """``PreflightVerdict`` 十字段与 §15.2-A 逐字一致（字段名 + 顺序 + 默认值）。"""
    assert list(pf.PreflightVerdict.model_fields) == [
        "symbol",
        "checked",
        "latest_frame_id",
        "as_of_date",
        "stale",
        "stale_after",
        "days_since",
        "pending",
        "blocking",
        "note",
    ]
    empty = pf.PreflightVerdict()
    assert (empty.symbol, empty.checked, empty.latest_frame_id, empty.as_of_date) == ("", False, "", "")
    assert (empty.stale, empty.stale_after, empty.days_since) == (False, "", None)
    assert (empty.pending, empty.blocking, empty.note) == ([], False, "")


def test_check_preflight_signature_matches_spec():
    """``check_preflight(symbol, *, state_dir=None, alert_dir=None, today=None, block_enabled=True)``。"""
    import inspect

    signature = inspect.signature(pf.check_preflight)
    assert list(signature.parameters) == ["symbol", "state_dir", "alert_dir", "today", "block_enabled"]
    assert all(
        signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        for name in ("state_dir", "alert_dir", "today", "block_enabled")
    )
    assert [signature.parameters[name].default for name in ("state_dir", "alert_dir", "today", "block_enabled")] == [
        None,
        None,
        None,
        True,
    ]


def test_default_dirs_match_upstream_owners():
    """缺省目录与真实属主同口径（不 import 属主模块的代价由这两条钉子兜住，防漂移）。"""
    from alphabee.midterm.persistence import DEFAULT_DATA_DIR

    assert pf.DEFAULT_STATE_DIR == Path(DEFAULT_DATA_DIR)
    assert pf.DEFAULT_ALERT_DIR == Path(TRACKING_ALERT_DIR)


_PURE_MODULE_IMPORT_ROOTS = {"__future__", "datetime", "json", "logging", "pathlib", "typing", "pydantic"}


def test_module_level_imports_are_pure_only():
    """模块级 import 只允许纯依赖：不拉 ``alphabee.*``（尤其 ``tracking`` → tushare 链）/ LLM / 网络库。"""
    import ast

    tree = ast.parse(Path(pf.__file__).read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= _PURE_MODULE_IMPORT_ROOTS, f"模块级 import 越界：{sorted(roots - _PURE_MODULE_IMPORT_ROOTS)}"


@pytest.mark.parametrize(
    "needle",
    ["alphabee.tracking", "httpx", "requests", "openai", "langchain", "tushare", "socket", "urllib"],
)
def test_module_source_has_no_tracking_or_network_dependency(needle: str):
    """源码级（AST）：**任何位置**的 import 都不得出现 tracking / 网络库 / LLM 客户端。

    用 AST 而不是子串匹配：docstring 里提到这些名字是叙述（本条不判），真正的判据是 import 图。
    """
    import ast

    tree = ast.parse(Path(pf.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert needle not in imported, f"越界 import：{needle}（全部 import = {sorted(imported)}）"


def test_import_allowlist_is_exactly_pure_plus_midterm_persistence():
    """import 图**精确**等于白名单：纯依赖 + 函数体内唯一的 ``alphabee.midterm.persistence``。"""
    import ast

    tree = ast.parse(Path(pf.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert imported == {
        "__future__",
        "datetime",
        "json",
        "logging",
        "pathlib",
        "typing",
        "pydantic",
        "alphabee.midterm.persistence",
    }


def test_check_preflight_makes_no_network_calls(tmp_path, monkeypatch):
    """纯规则：判定路径不得触达网络（connect/create_connection 一旦被调用即断言失败）。"""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("入口前置校验不得发起网络调用")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)

    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir)
    _write_alert_lines(alert_dir, [_alert_row(triggers=(_trigger(),))])

    verdict = pf.check_preflight(SYMBOL, state_dir=state_dir, alert_dir=alert_dir, today="2026-09-22")
    assert verdict.blocking is True


def test_check_preflight_never_writes(tmp_path):
    """只读既有产物、不新增存储：判定前后目录内容（含 mtime 无关的 sha256）逐字节不变。"""
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir)
    _write_alert_lines(alert_dir, [_alert_row(triggers=(_trigger(),)), _alert_row(exit_reasons=("SENTINEL_EXIT",))])

    before = _tree_snapshot(tmp_path)
    for today in ("2026-09-02", "2026-09-22"):
        pf.check_preflight(SYMBOL, state_dir=state_dir, alert_dir=alert_dir, today=today)
    assert _tree_snapshot(tmp_path) == before


# ── ② 无帧不阻断（§15.8 P2-①；§15.2 设计决策 2） ────────────────────────────


def test_no_frame_is_not_checked_and_not_blocking(tmp_path):
    verdict = pf.check_preflight(
        SYMBOL, state_dir=tmp_path / "state", alert_dir=tmp_path / "alerts", today="2026-09-22"
    )
    assert verdict.checked is False
    assert verdict.blocking is False
    assert verdict.stale is False
    assert verdict.latest_frame_id == ""
    assert verdict.days_since is None
    assert "首次研究" in verdict.note


def test_no_frame_with_pending_alerts_still_not_blocking(tmp_path):
    """**无帧 ⇒ 放行是硬结论**：即便告警末行有未消费触发，也不阻断（首次研究）。"""
    alert_dir = tmp_path / "alerts"
    _write_alert_lines(alert_dir, [_alert_row(triggers=(_trigger(),), exit_reasons=("SENTINEL_EXIT",))])

    verdict = pf.check_preflight(SYMBOL, state_dir=tmp_path / "state", alert_dir=alert_dir, today="2026-09-22")
    assert verdict.checked is False
    assert verdict.blocking is False
    # pending 仍如实回填（另一份既有产物的事实），但不参与判定
    assert verdict.pending == ["trigger:price_move", "exit:SENTINEL_EXIT"]


@pytest.mark.parametrize("symbol", [None, "", "   "])
def test_empty_symbol_never_blocks(symbol):
    verdict = pf.check_preflight(symbol, today="2026-09-22")
    assert verdict.checked is False and verdict.blocking is False
    assert verdict.note == "未识别标的（symbol 为空）⇒ 不阻断"


def test_missing_state_and_alert_dirs_do_not_raise(tmp_path):
    verdict = pf.check_preflight(SYMBOL, state_dir=tmp_path / "nope", alert_dir=tmp_path / "nope2", today="2026-09-22")
    assert verdict.checked is False and verdict.blocking is False


# ── ③ 陈旧 / 触发 → blocking（§15.8 P2-②） ──────────────────────────────────


def test_stale_frame_is_stale_and_blocking_by_default(tmp_path):
    state_dir = tmp_path / "state"
    _frame(state_dir, as_of="2026-09-01", stale_after="2026-09-08")

    verdict = pf.check_preflight(SYMBOL, state_dir=state_dir, alert_dir=tmp_path / "alerts", today="2026-09-22")
    assert verdict.checked is True
    assert verdict.stale is True
    assert verdict.blocking is True
    assert verdict.latest_frame_id == f"{SYMBOL}:2026-09-01"
    assert verdict.as_of_date == "2026-09-01"
    assert verdict.stale_after == "2026-09-08"
    assert "已陈旧" in verdict.note


def test_fresh_frame_is_not_stale_and_not_blocking(tmp_path):
    state_dir = tmp_path / "state"
    _frame(state_dir, as_of="2026-09-20", stale_after="2026-10-08")

    verdict = pf.check_preflight(SYMBOL, state_dir=state_dir, alert_dir=tmp_path / "alerts", today="2026-09-22")
    assert verdict.stale is False and verdict.blocking is False
    assert verdict.days_since == 2


def test_stale_after_equal_to_today_is_not_stale(tmp_path):
    """口径：``stale_after < today`` 才算到期（``== today`` 视为仍在保鲜期内）。"""
    state_dir = tmp_path / "state"
    _frame(state_dir, as_of="2026-09-01", stale_after="2026-09-22")

    verdict = pf.check_preflight(SYMBOL, state_dir=state_dir, alert_dir=tmp_path / "alerts", today="2026-09-22")
    assert verdict.stale is False and verdict.blocking is False


@pytest.mark.parametrize("stale_after", [None, "", "not-a-date"])
def test_absent_or_invalid_stale_after_is_not_stale(tmp_path, stale_after):
    """缺字段/非法日期 → 不判陈旧（不猜测、不抛）——守"不新增判定"的边界。"""
    state_dir = tmp_path / "state"
    _frame(state_dir, as_of="2026-09-01", stale_after=stale_after)

    verdict = pf.check_preflight(SYMBOL, state_dir=state_dir, alert_dir=tmp_path / "alerts", today="2026-09-22")
    assert verdict.checked is True
    assert verdict.stale is False and verdict.blocking is False


def test_days_since_is_frame_age_not_overdue_days(tmp_path):
    """口径钉子：``days_since`` = ``today − as_of_date``（帧龄），**不是**超过 ``stale_after`` 的天数。"""
    state_dir = tmp_path / "state"
    _frame(state_dir, as_of="2026-09-01", stale_after="2026-09-08")

    verdict = pf.check_preflight(SYMBOL, state_dir=state_dir, alert_dir=tmp_path / "alerts", today="2026-09-22")
    assert verdict.days_since == 21  # 不是 14（14 才是"超期天数"）


def test_days_since_not_clamped_for_future_frame(tmp_path):
    """未来 dated 的帧给出负数帧龄：宁可暴露也不静默夹取（与 detection_latency 同取向）。"""
    state_dir = tmp_path / "state"
    _frame(state_dir, as_of="2026-10-01", stale_after="2026-10-20")

    verdict = pf.check_preflight(SYMBOL, state_dir=state_dir, alert_dir=tmp_path / "alerts", today="2026-09-22")
    assert verdict.days_since == -9


def test_alert_last_row_triggers_make_pending_and_blocking(tmp_path):
    """告警末行的 ``triggers`` 非空 ⇒ ``pending`` 非空且 ``blocking``（帧本身不陈旧也一样）。"""
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir, as_of="2026-09-20", stale_after="2026-10-08")
    _write_alert_lines(alert_dir, [_alert_row(triggers=(_trigger(),))])

    verdict = pf.check_preflight(SYMBOL, state_dir=state_dir, alert_dir=alert_dir, today="2026-09-22")
    assert verdict.stale is False
    assert verdict.pending == ["trigger:price_move"]
    assert verdict.blocking is True
    assert "1 条未消费触发" in verdict.note


def test_pending_digest_covers_exit_and_monitor_reasons(tmp_path):
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir)
    _write_alert_lines(
        alert_dir,
        [
            _alert_row(
                triggers=(_trigger(), _trigger(TriggerKind.MANUAL, "人工确认闸拦下 state_transition")),
                exit_reasons=("SENTINEL_EXIT",),
                monitor_reasons=("SENTINEL_MON",),
            )
        ],
    )

    verdict = pf.check_preflight(SYMBOL, state_dir=state_dir, alert_dir=alert_dir, today="2026-09-22")
    assert verdict.pending == [
        "trigger:price_move",
        "trigger:manual",
        "exit:SENTINEL_EXIT",
        "monitor:SENTINEL_MON",
    ]


def test_only_the_last_alert_row_counts(tmp_path):
    """§15.2-A 的口径是**末行**：更早的帧有触发也不算 pending。"""
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir, as_of="2026-09-20", stale_after="2026-10-08")
    _write_alert_lines(alert_dir, [_alert_row(triggers=(_trigger(),)), _alert_row()])

    verdict = pf.check_preflight(SYMBOL, state_dir=state_dir, alert_dir=alert_dir, today="2026-09-22")
    assert verdict.pending == []
    assert verdict.blocking is False


def test_corrupt_alert_lines_are_skipped(tmp_path):
    """§15.2-E："告警含损坏行 → 跳过该行、不抛异常"。"""
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir)
    _write_alert_lines(
        alert_dir,
        [
            "{ this is not json",
            _alert_row(triggers=(_trigger(),)),
            "",
            "[1, 2, 3]",  # 合法 JSON 但非对象 → 同样跳过
        ],
    )

    verdict = pf.check_preflight(SYMBOL, state_dir=state_dir, alert_dir=alert_dir, today="2026-09-22")
    assert verdict.pending == ["trigger:price_move"]
    assert verdict.blocking is True


def test_corrupt_frame_file_fails_open(tmp_path):
    """帧文件整体损坏（pydantic 校验失败）→ fail-open，不抛、不阻断。"""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / f"{SYMBOL}.jsonl").write_text('{"as_of_date": "2026-09-01", "state": "not-an-object"}\n', "utf-8")

    verdict = pf.check_preflight(SYMBOL, state_dir=state_dir, alert_dir=tmp_path / "alerts", today="2026-09-22")
    assert verdict.blocking is False
    assert verdict.checked is False
    assert "fail-open" in verdict.note


def test_state_dir_pointing_at_a_file_fails_open(tmp_path):
    """``state_dir`` 指向普通文件（路径异常）→ 仍然只返回 verdict，不抛。"""
    file_path = tmp_path / "state.jsonl"
    file_path.write_text("", "utf-8")

    verdict = pf.check_preflight(SYMBOL, state_dir=file_path, alert_dir=tmp_path / "alerts", today="2026-09-22")
    assert verdict.blocking is False
    assert verdict.checked is False


@pytest.mark.parametrize("today", ["not-a-date", "2026-13-45", ""])
def test_invalid_today_falls_back_to_system_date(tmp_path, today):
    """非法/缺失 ``today`` → 用系统当日（不抛）；判定仍可用。"""
    state_dir = tmp_path / "state"
    _frame(state_dir, as_of="2020-01-01", stale_after="2020-01-08")

    verdict = pf.check_preflight(SYMBOL, state_dir=state_dir, alert_dir=tmp_path / "alerts", today=today)
    assert verdict.checked is True
    assert verdict.stale is True  # 2020 年的帧相对"今天"必然陈旧


def test_block_enabled_false_never_blocks_but_still_hits(tmp_path):
    """**记录侧契约**：``block_enabled=False`` ⇒ ``blocking`` 恒 False，但命中事实（stale/pending）照常返回。

    这正是 §15.2-B 记账接线依赖的形状：记账看"命中"，阻断才看 ``blocking``。
    """
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir, as_of="2026-09-01", stale_after="2026-09-08")
    _write_alert_lines(alert_dir, [_alert_row(triggers=(_trigger(),))])

    recorded = pf.check_preflight(
        SYMBOL, state_dir=state_dir, alert_dir=alert_dir, today="2026-09-22", block_enabled=False
    )
    gated = pf.check_preflight(SYMBOL, state_dir=state_dir, alert_dir=alert_dir, today="2026-09-22")

    assert recorded.blocking is False
    assert gated.blocking is True
    assert (recorded.checked, recorded.stale, recorded.pending) == (gated.checked, gated.stale, gated.pending)


# ── ④ 节点级入账（§15.8 P2-④；§15.2-B "记录恒发生"） ────────────────────────


class _StubAgent:
    async def ainvoke(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        from langchain_core.messages import AIMessage

        return {"messages": [AIMessage(content="fact narrative")]}


class _StubFacts:
    def to_fact_values(self) -> dict[str, float]:
        return {"revenue": 1.0}


async def _run_entry_nodes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state_dir: Path,
    alert_dir: Path,
    query: str = QUERY,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """真跑入口两节点 ``prepare_analysis_context`` → ``collect_raw_facts``，返回两份 partial state。

    只打桩三个外部依赖（LLM agent / 两个结构化事实模型）；前置校验是 ``prepare_analysis_context``
    的职责，故把它的帧/告警目录指到 tmp，再按图顺序跑到采集节点，验证"入账且采集继续"。
    """
    collectors = importlib.import_module("alphabee.orchestrator.collectors")
    monkeypatch.setattr(collectors, "fact_collector_agent_factory", lambda: _StubAgent())
    monkeypatch.setattr(collectors, "get_financial_facts_model", lambda symbol: _StubFacts())
    monkeypatch.setattr(collectors, "get_market_facts_model", lambda symbol: _StubFacts())
    monkeypatch.setattr(pf, "DEFAULT_STATE_DIR", state_dir)
    monkeypatch.setattr(pf, "DEFAULT_ALERT_DIR", alert_dir)

    from langchain_core.messages import HumanMessage

    from alphabee.orchestrator.nodes.prepare_analysis_context import prepare_analysis_context

    state: dict[str, Any] = {"messages": [HumanMessage(content=query)]}
    ctx = await prepare_analysis_context(state, {})
    collected = await collectors.collect_raw_facts({**state, "run": ctx["run"]}, {})
    return ctx, collected


async def test_prepare_analysis_context_records_stale_state_run_issue(tmp_path, monkeypatch):
    """陈旧帧 ⇒ 入口节点产 ``stale_state_run``（D5/MEDIUM/planning）issue，且**采集继续**。"""
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir, as_of="2026-09-01", stale_after="2026-09-08")

    ctx, collected = await _run_entry_nodes(monkeypatch, state_dir=state_dir, alert_dir=alert_dir)

    recorded = [issue for issue in ctx["issues"] if issue.category == "stale_state_run"]
    assert len(recorded) == 1
    issue = recorded[0]
    assert issue.deviation_class is DeviationClass.D5_CONTROL
    assert issue.severity is IssueSeverity.MEDIUM
    assert issue.scope is IssueScope.PLANNING
    assert issue.detected_at_step == "prepare_analysis_context"
    assert issue.related_step == "prepare_analysis_context"
    assert issue.recovery_action == "proceeded_without_reconcile"
    assert f"{SYMBOL}:2026-09-01" in issue.message
    assert "待消费触发 0 条" in issue.message
    # 入口 Step 因"命中记录"落 PARTIAL；采集继续：artifact 照产、采集 Step 仍 SUCCEEDED
    assert ctx["steps"][0].status is StepStatus.PARTIAL
    assert collected["artifacts"]
    assert collected["steps"][0].status is StepStatus.SUCCEEDED
    assert collected["fact_values"] == {"revenue": 1.0}


async def test_prepare_analysis_context_records_pending_triggers_even_when_frame_fresh(tmp_path, monkeypatch):
    """未消费触发（帧不陈旧）同样入账：判定复用告警末行，不新增条件。"""
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir, as_of="2026-09-20", stale_after="2099-01-01")  # 远期 stale_after ⇒ 帧真正保鲜
    _write_alert_lines(alert_dir, [_alert_row(triggers=(_trigger(),), exit_reasons=("SENTINEL_EXIT",))])

    ctx, _ = await _run_entry_nodes(monkeypatch, state_dir=state_dir, alert_dir=alert_dir)

    recorded = [issue for issue in ctx["issues"] if issue.category == "stale_state_run"]
    assert len(recorded) == 1
    assert "待消费触发 2 条" in recorded[0].message


async def test_prepare_analysis_context_calls_preflight_with_block_disabled(tmp_path, monkeypatch):
    """§15.2-B 的调用形状：``check_preflight(symbol, block_enabled=False)``——记录侧**永不阻断**。

    判别力：若把 ``block_enabled`` 丢掉（或记录侧误传 True），这一条即红；若把记账判据从"命中"
    改成 ``verdict.blocking``，则在 ``block_enabled=False`` 下**一条都不记** ⇒ 上面两条入账用例即红。
    """
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir, as_of="2026-09-01", stale_after="2026-09-08")

    from alphabee.orchestrator.nodes import prepare_analysis_context as rqc

    calls: list[tuple[Any, dict[str, Any]]] = []
    real = pf.check_preflight

    def _spy(symbol: Any, **kwargs: Any) -> pf.PreflightVerdict:
        calls.append((symbol, kwargs))
        return real(symbol, **kwargs)

    monkeypatch.setattr(rqc, "check_preflight", _spy)
    await _run_entry_nodes(monkeypatch, state_dir=state_dir, alert_dir=alert_dir)

    assert calls == [(SYMBOL, {"block_enabled": False})]


async def test_prepare_analysis_context_silent_for_first_run(tmp_path, monkeypatch):
    """无帧（首次研究）⇒ 不产任何 ``stale_state_run`` issue，也不改 Step 状态（零回归面）。"""
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"

    ctx, collected = await _run_entry_nodes(monkeypatch, state_dir=state_dir, alert_dir=alert_dir)

    assert [issue.category for issue in ctx["issues"]] == []
    assert ctx["steps"][0].status is StepStatus.SUCCEEDED
    assert collected["steps"][0].status is StepStatus.SUCCEEDED


async def test_prepare_analysis_context_silent_for_fresh_frame_without_triggers(tmp_path, monkeypatch):
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    # 节点内 check_preflight 用系统当日；stale_after 取远期值以与运行日期解耦（避免时间炸弹）。
    _frame(state_dir, as_of="2026-09-20", stale_after="2099-01-01")

    ctx, _ = await _run_entry_nodes(monkeypatch, state_dir=state_dir, alert_dir=alert_dir)

    assert [issue.category for issue in ctx["issues"]] == []


def test_stale_state_run_category_is_registered_as_d5():
    """§15.8 覆盖守卫的本地钉子（全仓守卫在 ``test_deviation_service.py``；两处必须一致）。"""
    assert CLASS_BY_CATEGORY["stale_state_run"] is DeviationClass.D5_CONTROL


# ── ⑤ CLI 两路径（§15.8 P2-③；§15.2-C） ──────────────────────────────────────


def test_parse_args_defaults_and_flags(monkeypatch):
    """``--allow-stale`` / ``--track-alerts`` 的解析形状（与 §15.2-C 逐字一致）。"""
    default = _parsed_args(monkeypatch, [QUERY])
    assert default.allow_stale is False
    assert default.track_alerts is None

    allow = _parsed_args(monkeypatch, [QUERY, "--allow-stale"])
    assert allow.allow_stale is True

    listing = _parsed_args(monkeypatch, ["--track-alerts"])
    assert listing.track_alerts == ""  # nargs="?" + const="" ⇒ "列出全部标的"

    one = _parsed_args(monkeypatch, ["--track-alerts", "601138"])
    assert one.track_alerts == "601138"


def _gate_args(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> Any:
    return _parsed_args(monkeypatch, argv)


def _patch_preflight_dirs(monkeypatch: pytest.MonkeyPatch, state_dir: Path, alert_dir: Path) -> None:
    monkeypatch.setattr(pf, "DEFAULT_STATE_DIR", state_dir)
    monkeypatch.setattr(pf, "DEFAULT_ALERT_DIR", alert_dir)


def test_symbol_from_query_matches_collectors_source(monkeypatch):
    """入口 gate 的符号解析与 ``prepare_analysis_context._first_symbol`` 同源（同函数、同取值顺序）。"""
    cli_main = importlib.import_module("alphabee.apps.cli.main")
    from alphabee.orchestrator.nodes.prepare_analysis_context import _first_symbol

    assert cli_main.symbol_from_query(QUERY) == _first_symbol(QUERY) == SYMBOL
    assert cli_main.symbol_from_query("") is None


def test_symbol_from_query_fails_open_when_stock_list_unreadable(monkeypatch):
    """符号解析失败 ⇒ ``None``（＝不阻断），绝不因入口校验崩溃。"""
    cli_main = importlib.import_module("alphabee.apps.cli.main")

    def _boom(query: str) -> Any:
        raise FileNotFoundError("all_stocks.csv missing")

    monkeypatch.setattr("alphabee.tools.common.extract_symbols_from_query", _boom)
    assert cli_main.symbol_from_query(QUERY) is None


def test_gate_blocks_with_exit_3_when_stale_without_allow_stale(tmp_path, monkeypatch, capsys):
    """§15.2-E：``blocking`` 且无 ``--allow-stale`` ⇒ ``SystemExit(3)``（"未执行"退出码）。"""
    cli_main = importlib.import_module("alphabee.apps.cli.main")
    cli_main.set_color_enabled(False)
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir, as_of="2026-09-01", stale_after="2026-09-08")
    _patch_preflight_dirs(monkeypatch, state_dir, alert_dir)
    args = _gate_args(monkeypatch, [QUERY])

    with pytest.raises(SystemExit) as excinfo:
        cli_main.enforce_preflight_gate(args, start_ts=time.monotonic())

    assert excinfo.value.code == 3  # 无 message（纯退出码），与 tracking `--loop` 的 exit 2 同风格
    out = capsys.readouterr().out
    assert "已阻断" in out and "--allow-stale" in out


def test_gate_allows_with_allow_stale_and_still_reports_the_hit(tmp_path, monkeypatch):
    """``--allow-stale`` 放行：**不**抛；返回的 verdict 仍显示命中（记账侧随后恒发生）。"""
    cli_main = importlib.import_module("alphabee.apps.cli.main")
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir, as_of="2026-09-01", stale_after="2026-09-08")
    _patch_preflight_dirs(monkeypatch, state_dir, alert_dir)
    args = _gate_args(monkeypatch, [QUERY, "--allow-stale"])

    verdict = cli_main.enforce_preflight_gate(args, start_ts=time.monotonic())

    assert (verdict.stale, verdict.blocking) == (True, True)


def test_gate_does_not_block_for_first_run(tmp_path, monkeypatch):
    """无帧 ⇒ 入口放行（§15.2 设计决策 2），即便配置默认阻断。"""
    cli_main = importlib.import_module("alphabee.apps.cli.main")
    _patch_preflight_dirs(monkeypatch, tmp_path / "state", tmp_path / "alerts")
    args = _gate_args(monkeypatch, [QUERY])

    verdict = cli_main.enforce_preflight_gate(args, start_ts=time.monotonic())

    assert verdict.checked is False and verdict.blocking is False


_BASE_SETTINGS: Settings | None = None


def _base_settings() -> Settings:
    """真实 ``get_settings()`` 的一次性快照（供 ``_settings_with_tracking`` 派生）。

    必须**先取真值再打桩**：``_settings_with_tracking`` 内部若再调 ``get_settings()``，在
    "把 ``get_settings`` 换成这个 lambda"的用例里会无限递归。
    """
    global _BASE_SETTINGS
    if _BASE_SETTINGS is None:
        from alphabee.config import get_settings

        _BASE_SETTINGS = get_settings()
    return _BASE_SETTINGS


def _settings_with_tracking(**overrides: Any) -> Settings:
    base = _base_settings()
    deviation = base.deviation.model_copy(update={"tracking": base.deviation.tracking.model_copy(update=overrides)})
    return base.model_copy(update={"deviation": deviation})


def test_gate_does_not_block_when_block_stale_runs_is_off(tmp_path, monkeypatch):
    """§15.2-D：``block_stale_runs=false`` ⇒ **不阻断但命中照旧**（只剩记账）。"""
    base = _settings_with_tracking(block_stale_runs=False)  # 先取真配置，再打桩（防自递归）
    cli_main = importlib.import_module("alphabee.apps.cli.main")
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir, as_of="2026-09-01", stale_after="2026-09-08")
    _patch_preflight_dirs(monkeypatch, state_dir, alert_dir)
    monkeypatch.setattr("alphabee.config.get_settings", lambda: base)
    args = _gate_args(monkeypatch, [QUERY])

    verdict = cli_main.enforce_preflight_gate(args, start_ts=time.monotonic())

    assert verdict.stale is True
    assert verdict.blocking is False


def test_gate_fails_open_to_blocking_when_config_unavailable(tmp_path, monkeypatch):
    """配置不可读 ⇒ 回落**默认阻断**（fail-open 的方向是"不静默放行"）。"""
    cli_main = importlib.import_module("alphabee.apps.cli.main")
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir, as_of="2026-09-01", stale_after="2026-09-08")
    _patch_preflight_dirs(monkeypatch, state_dir, alert_dir)

    def _boom() -> Any:
        raise RuntimeError("config unavailable")

    monkeypatch.setattr("alphabee.config.get_settings", _boom)
    assert cli_main.block_stale_runs_from_settings() is True
    args = _gate_args(monkeypatch, [QUERY])
    with pytest.raises(SystemExit) as excinfo:
        cli_main.enforce_preflight_gate(args, start_ts=time.monotonic())
    assert excinfo.value.code == 3


def test_block_stale_runs_from_settings_reads_the_config_value(monkeypatch):
    off = _settings_with_tracking(block_stale_runs=False)  # 先取真配置，再打桩（防自递归）
    on = _settings_with_tracking(block_stale_runs=True)
    cli_main = importlib.import_module("alphabee.apps.cli.main")

    monkeypatch.setattr("alphabee.config.get_settings", lambda: off)
    assert cli_main.block_stale_runs_from_settings() is False

    monkeypatch.setattr("alphabee.config.get_settings", lambda: on)
    assert cli_main.block_stale_runs_from_settings() is True


# ── ⑥ 配置：deviation.tracking 段（§15.7 / §15.2-D） ────────────────────────


def test_tracking_settings_defaults_are_the_registered_values():
    assert DeviationTrackingSettings().model_dump() == {
        "stale_after_days": 7,
        "block_stale_runs": True,
        "tv_distance": 0.3,
        "evidence_rate": 0.5,
        "max_alerts_shown": 20,
    }


def test_tracking_defaults_equal_upstream_constants():
    """§15.7 的"显式登记既有默认"必须**真的**等于上游常量（否则就是第二份阈值定义）。"""
    from alphabee.midterm import diff_consumers

    section = DeviationTrackingSettings()
    assert section.stale_after_days == DEFAULT_STALE_AFTER_DAYS
    assert section.tv_distance == diff_consumers._TV_TRIGGER
    assert section.evidence_rate == diff_consumers._EVIDENCE_RATE_TRIGGER


def test_tracking_thresholds_explicit_registration_keeps_monitor_verdict():
    """本段落地后 ``monitor_kwargs()`` 开始显式传 ``tv_threshold=0.3``：**有效判定逐字不变**。

    这就是"新增段不改变未配置者行为"的可复算证据：同一 ``CompanyStateDiff`` 在"不传参"与
    "显式传本段登记值"两条路径下产出**相等**的 ``MonitorTrigger``（含阈值边界 0.3 自身）。
    """
    from alphabee.midterm import diff_consumers
    from alphabee.tracking.triggers import monitor_kwargs, thresholds_from_settings

    limits = thresholds_from_settings()
    assert limits.tv_distance == diff_consumers._TV_TRIGGER
    assert monitor_kwargs(limits) == {"tv_threshold": diff_consumers._TV_TRIGGER}

    for tv in (0.0, 0.29, 0.3, 0.4):
        frame = CompanyStateDiff(
            symbol=SYMBOL,
            prev=None,
            curr=ArtifactRef(id=f"{SYMBOL}:2026-09-18", date="2026-09-18", symbol=SYMBOL),
            elapsed_days=10,
            state_shift=StateShift(argmax_from="S0", argmax_to="S0", tv_distance=tv, kind="same"),
            position=None,
            confidence=None,
        )
        assert diff_consumers.monitor_triggers(frame, **monitor_kwargs(limits)) == diff_consumers.monitor_triggers(
            frame
        )


def test_config_example_tracking_section_matches_model_defaults():
    """``config.yaml.example`` 的 ``deviation.tracking`` 段与模型默认值**同源**（防文档漂移）。"""
    example = yaml.safe_load((REPO_ROOT / "config.yaml.example").read_text(encoding="utf-8"))
    assert example["deviation"]["tracking"] == DeviationTrackingSettings().model_dump()


@pytest.mark.parametrize(
    "raw",
    [
        {"llm": {"api_key": "k", "base_url": "u", "model": "m"}},  # 完全没有 deviation 段
        {"llm": {"api_key": "k", "base_url": "u", "model": "m"}, "deviation": {"recovery": {"enabled": False}}},
    ],
)
def test_legacy_config_without_tracking_section_still_constructs(raw):
    """旧 ``config.yaml``（无 ``deviation`` / 无 ``deviation.tracking``）仍可 import 并取默认。"""
    settings = Settings(**raw)
    assert settings.deviation.tracking.model_dump() == DeviationTrackingSettings().model_dump()


def test_legacy_config_file_loads_through_the_real_loader(tmp_path):
    """真跑 ``ConfigLoader`` + ``Settings``：旧配置文件（无 deviation 段）路径可见地不抛。"""
    legacy = tmp_path / "config.yaml"
    legacy.write_text("llm:\n  api_key: k\n  base_url: u\n  model: m\n", encoding="utf-8")

    raw = ConfigLoader(str(legacy)).load_config()
    settings = Settings(**raw)

    assert isinstance(settings.deviation, DeviationSettings)
    assert settings.deviation.tracking.block_stale_runs is True  # 默认阻断（= 未配置者的行为，已登记）
