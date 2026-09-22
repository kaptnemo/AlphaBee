"""CLI 入口级测试：``alphabee/apps/cli/main.py`` 的分派与两侧接线（研究连续体 P2 / D2-B2，§15.2-C/E）。

与 ``tests/orchestrator/test_preflight.py`` 的分工：那边测**判定与 gate 函数**（含 §15.8 P2 四条
不可省断言），这里测**真跑 ``main()``** 的入口分派 —— 阻断是否真的发生在进入 ``run_query`` 之前、
``--track-alerts`` 是否真的是只读视图（不触发 run、不写盘）。

运行约定：本文件 import ``alphabee.apps.cli.main`` ⇒ 拉起 ``orchestrator.agent`` → ``collectors``
→ ``tushare.set_token``（写 ``$HOME/tk.csv``）。按仓库既定口径必须以
``poetry run env HOME=<workspace>/tmp/pytest_home pytest …`` 运行（团队 verify 命令即此形态）。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from alphabee.orchestrator.services import preflight as pf
from alphabee.tracking.scheduler import TrackingReport
from alphabee.tracking.triggers import Trigger, TriggerKind

SYMBOL = "600519.SH"
QUERY = "分析一下贵州茅台"  # 真实符号解析：公司名 → 600519.SH


def _main_module() -> Any:
    return importlib.import_module("alphabee.apps.cli.main")


def _frame(
    state_dir: Path, *, symbol: str = SYMBOL, as_of: str = "2026-09-01", stale_after: str = "2026-09-08"
) -> None:
    """用真实写入器落一帧（陈旧/未对账状态的输入侧）。"""
    from alphabee.midterm.models import CompanyStateArtifact
    from alphabee.midterm.persistence import append_artifact

    append_artifact(
        CompanyStateArtifact(symbol=symbol, as_of_date=as_of, stale_after=stale_after, thesis="核心假设"),
        state_dir,
    )


def _alert_line(alert_dir: Path, *, symbol: str = SYMBOL, as_of: str = "2026-09-01") -> Path:
    """写一行真实告警（``TrackingReport.model_dump(mode="json")``，与 ``_write_alerts`` 同形状）。"""
    alert_dir.mkdir(parents=True, exist_ok=True)
    report = TrackingReport(
        symbol=symbol,
        as_of=as_of,
        triggers=[Trigger(kind=TriggerKind.PRICE_MOVE, symbol=symbol, reason="行情异动：+7.50%")],
        exit_reasons=["SENTINEL_EXIT"],
        blocked_actions=["state_transition:upgrade:S0→S5"],
        escalation_tier=5,
    )
    path = alert_dir / f"{symbol}.jsonl"
    path.write_text(json.dumps(report.model_dump(mode="json"), ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _patch_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state_dir: Path, alert_dir: Path) -> None:
    """把判定数据源指向 tmp（``check_preflight`` 缺省目录）与告警视图目录指向同一处。"""
    monkeypatch.setattr(pf, "DEFAULT_STATE_DIR", state_dir)
    monkeypatch.setattr(pf, "DEFAULT_ALERT_DIR", alert_dir)
    monkeypatch.setattr("alphabee.tracking.scheduler.default_alert_dir", lambda: alert_dir)


def _run_main(monkeypatch: pytest.MonkeyPatch, argv: list[str], tmp_path: Path) -> Any:
    """真跑 ``main()``（日志目录重定向到 tmp，避免污染仓库 ``./logs``）。"""
    monkeypatch.setattr(sys, "argv", ["main.py", *argv, "--log-dir", str(tmp_path / "logs")])
    return _main_module().main()


# ── 阻断侧：进入 run_query 之前 ─────────────────────────────────────────────


def test_main_blocks_before_run_query_without_allow_stale(tmp_path, monkeypatch, capsys):
    """§15.2-C：``blocking`` 且无 ``--allow-stale`` ⇒ ``SystemExit(3)``，且**不进入** ``run_query``。"""
    cli_main = _main_module()
    cli_main.set_color_enabled(False)
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir)
    _patch_env(monkeypatch, tmp_path, state_dir, alert_dir)

    calls: list[Any] = []

    async def _fake_run_query(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(cli_main, "run_query", _fake_run_query)

    with pytest.raises(SystemExit) as excinfo:
        _run_main(monkeypatch, [QUERY], tmp_path)

    assert excinfo.value.code == 3
    assert calls == []  # 采集/流水线一步都没走
    out = capsys.readouterr().out
    assert "已阻断" in out


def test_main_runs_query_with_allow_stale(tmp_path, monkeypatch):
    """``--allow-stale`` 放行 ⇒ 真正进入 ``run_query``（记账由节点侧恒发生，与本分支无关）。"""
    cli_main = _main_module()
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir)
    _patch_env(monkeypatch, tmp_path, state_dir, alert_dir)

    calls: list[Any] = []

    async def _fake_run_query(query: str, *args: Any, **kwargs: Any) -> None:
        calls.append(query)

    monkeypatch.setattr(cli_main, "run_query", _fake_run_query)

    _run_main(monkeypatch, [QUERY, "--allow-stale"], tmp_path)

    assert calls == [QUERY]


def test_main_runs_query_for_first_run(tmp_path, monkeypatch):
    """无帧（首次研究）⇒ 入口放行：不阻断、正常进入 ``run_query``。"""
    cli_main = _main_module()
    _patch_env(monkeypatch, tmp_path, tmp_path / "state", tmp_path / "alerts")

    calls: list[Any] = []

    async def _fake_run_query(query: str, *args: Any, **kwargs: Any) -> None:
        calls.append(query)

    monkeypatch.setattr(cli_main, "run_query", _fake_run_query)

    _run_main(monkeypatch, [QUERY], tmp_path)

    assert calls == [QUERY]


def test_main_does_not_block_chat_mode_without_symbol(tmp_path, monkeypatch):
    """纯对话模式（无 query ⇒ 无标的）⇒ 不阻断，进入 ``run_chat_session``。"""
    cli_main = _main_module()
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir)  # 该标的陈旧，但本次请求没有标的
    _patch_env(monkeypatch, tmp_path, state_dir, alert_dir)

    calls: list[Any] = []

    async def _fake_chat(*args: Any, **kwargs: Any) -> None:
        calls.append(args)

    monkeypatch.setattr(cli_main, "run_chat_session", _fake_chat)

    _run_main(monkeypatch, ["--chat"], tmp_path)

    assert len(calls) == 1


# ── --track-alerts：只读视图 ────────────────────────────────────────────────


def test_main_track_alerts_is_a_read_only_view(tmp_path, monkeypatch, capsys):
    """§15.2-C：``--track-alerts <symbol>`` 打印告警末 N 行；**不触发 run、不写库、不改盘**。"""
    cli_main = _main_module()
    cli_main.set_color_enabled(False)
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir)
    _alert_line(alert_dir)
    _patch_env(monkeypatch, tmp_path, state_dir, alert_dir)

    calls: list[Any] = []

    async def _fake_run_query(*args: Any, **kwargs: Any) -> None:
        calls.append(args)

    monkeypatch.setattr(cli_main, "run_query", _fake_run_query)
    before = (_tree_digest(state_dir), _tree_digest(alert_dir))

    _run_main(monkeypatch, ["--track-alerts", SYMBOL], tmp_path)

    out = capsys.readouterr().out
    assert "跟踪告警（只读视图" in out
    assert f"{SYMBOL}" in out and "as_of=2026-09-01" in out
    assert "price_move" in out and "退出信号=1" in out and "行动类=1" in out
    assert calls == []  # 没有触发流水线
    assert before == (_tree_digest(state_dir), _tree_digest(alert_dir))  # 零写入


def test_main_track_alerts_without_symbol_lists_every_file(tmp_path, monkeypatch, capsys):
    """省略 SYMBOL ⇒ 列出全部标的（按文件名排序，确定性）。"""
    cli_main = _main_module()
    cli_main.set_color_enabled(False)
    alert_dir = tmp_path / "alerts"
    _alert_line(alert_dir, symbol="600519.SH")
    _alert_line(alert_dir, symbol="000001.SZ")
    _patch_env(monkeypatch, tmp_path, tmp_path / "state", alert_dir)

    _run_main(monkeypatch, ["--track-alerts"], tmp_path)

    out = capsys.readouterr().out
    assert out.index("000001.SZ") < out.index("600519.SH")  # 排序确定


def test_main_track_alerts_reports_absent_symbol_file(tmp_path, monkeypatch, capsys):
    cli_main = _main_module()
    cli_main.set_color_enabled(False)
    alert_dir = tmp_path / "alerts"
    alert_dir.mkdir(parents=True)
    _patch_env(monkeypatch, tmp_path, tmp_path / "state", alert_dir)

    _run_main(monkeypatch, ["--track-alerts", "300274.SZ"], tmp_path)

    assert "无告警记录" in capsys.readouterr().out


def test_render_track_alerts_view_skips_unparsable_rows(tmp_path, monkeypatch):
    """坏行（JSON 非法 / 合法但不符合 ``TrackingReport``）只跳过，视图继续渲染。"""
    cli_main = _main_module()
    alert_dir = tmp_path / "alerts"
    path = _alert_line(alert_dir)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{ not json\n")
        fh.write("{}\n")  # 合法 JSON 但缺必填字段 → model_validate 失败
    monkeypatch.setattr("alphabee.tracking.scheduler.default_alert_dir", lambda: alert_dir)

    text = cli_main.render_track_alerts_view(SYMBOL)

    assert "price_move" in text
    assert "告警行不可解析（跳过）" in text


def test_render_track_alerts_view_is_fail_open(tmp_path, monkeypatch):
    """视图侧 fail-open：import/目录解析失败 → 单行提示，不抛、不影响退出码。"""
    cli_main = _main_module()

    def _boom() -> Any:
        raise RuntimeError("alert dir unavailable")

    monkeypatch.setattr(cli_main, "_alerts_root", _boom)
    assert cli_main.render_track_alerts_view(SYMBOL).startswith("跟踪告警视图不可用（fail-open）")


def test_track_alerts_view_shows_real_repo_alerts_when_present():
    """真实产物冒烟（仓库内 ``data/tracking/alerts/*.jsonl`` 存在时）：视图可渲染且只读。"""
    cli_main = _main_module()
    from alphabee.tracking.scheduler import default_alert_dir

    alert_root = Path(default_alert_dir())
    if not alert_root.is_dir() or not any(alert_root.glob("*.jsonl")):
        pytest.skip(f"本地无告警产物：{alert_root}")

    before = _tree_digest(alert_root)
    text = cli_main.render_track_alerts_view(None, limit=3)
    assert "跟踪告警（只读视图" in text
    assert _tree_digest(alert_root) == before


def test_track_alerts_dispatch_returns_before_preflight_gate(tmp_path, monkeypatch):
    """分派顺序钉子：``--track-alerts`` 分支在入口 gate **之前**返回（视图与 gate 互不阻断）。"""
    cli_main = _main_module()
    cli_main.set_color_enabled(False)
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir)  # 陈旧状态存在，但视图请求不该被 gate 拦
    _alert_line(alert_dir)
    _patch_env(monkeypatch, tmp_path, state_dir, alert_dir)

    gate_calls: list[Any] = []
    monkeypatch.setattr(cli_main, "enforce_preflight_gate", lambda *a, **k: gate_calls.append(a))

    _run_main(monkeypatch, ["--track-alerts", SYMBOL], tmp_path)

    assert gate_calls == []


def test_version_like_smoke_of_gate_exit_code_is_not_one(tmp_path, monkeypatch):
    """退出码语义：被前置校验拦下是 ``3``（"未执行"），不是错误码 ``1``。"""
    cli_main = _main_module()
    cli_main.set_color_enabled(False)
    state_dir, alert_dir = tmp_path / "state", tmp_path / "alerts"
    _frame(state_dir)
    _patch_env(monkeypatch, tmp_path, state_dir, alert_dir)

    started = time.monotonic()
    with pytest.raises(SystemExit) as excinfo:
        _run_main(monkeypatch, [QUERY], tmp_path)

    assert excinfo.value.code == 3
    assert time.monotonic() - started < 30  # 未进入流水线（不做任何采集）
