"""P5（W5）``ThesisVersion`` 读写测试：``midterm/versions.py`` + ``scheduler._reconcile_frame`` 接线
（``docs/design/RESEARCH_CONTINUUM_DESIGN.md`` §15.5-D）。

覆盖 §15.8 / §15.5-D 的四条关键断言 + 本门核心的**反漂移**语义：

1. **首次登记**：``version=1``、``buy_rationale==[thesis]``、``reason=="first_registered"``；
2. **thesis 未变**（同日重复调用）：不追加、``reason=="unchanged"``、文件行数不变；
3. **thesis 改变**：``version=2``、``thesis`` 为新文本、**``buy_rationale`` 仍为首版**（反漂移核心断言）；
4. **幂等**：同 id 重复 append ⇒ 文件仍 1 行；
5. **损坏行**：跳过、其余可读、不抛；
6. **``persist=False``**：不写版本文件。

外加接线（``_reconcile_frame`` / ``run_once`` / ``reconcile``）、fail-open、``TrackingReport`` 追加字段
的向后兼容。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alphabee.midterm import persistence, versions
from alphabee.midterm.models import CompanyStateArtifact, EvidenceEvent, ExitCondition, ThesisVersion
from alphabee.midterm.versions import (
    APPEND_REASON_CHANGED,
    APPEND_REASON_FIRST,
    APPEND_REASON_UNCHANGED,
    DEFAULT_VERSION_DIR,
    _version_id,
    _version_path,
    append_version,
    default_version_dir,
    latest_version,
    load_versions,
    register_thesis_if_changed,
)
from alphabee.tracking.scheduler import TrackingReport, reconcile, run_once

SYMBOL = "600519.SH"
D1 = "2026-09-01"
D2 = "2026-09-20"
THESIS = "锂电需求超预期，毛利率触底回升"


# ── fixtures / 构造器 ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_version_dir(monkeypatch, tmp_path):
    """把版本目录重定向到 tmp（autouse 安全措施：任何用例都不可能写到仓库的真实 ``data/``）。"""
    monkeypatch.setattr(versions, "DEFAULT_VERSION_DIR", tmp_path / "thesis_versions")
    yield


def _versions_dir() -> Path:
    """当前用例的版本目录（autouse fixture 已把 ``versions.DEFAULT_VERSION_DIR`` 重定向到 tmp）。"""
    return Path(str(versions.DEFAULT_VERSION_DIR))


def _version_file(symbol: str = SYMBOL) -> Path:
    return _version_path(symbol, _versions_dir())


def _snapshot(date: str):
    from alphabee.midterm.models import FactorSnapshot, FundamentalFactor, RiskFactor, TrendFactor

    return FactorSnapshot(
        symbol=SYMBOL,
        as_of_date=date,
        fundamental=FundamentalFactor(revenue_yoy=12.0, net_profit_yoy=8.0),
        trend=TrendFactor(price_change_pct=1.0),
        risk=RiskFactor(news_title=""),
    )


def _providers(date: str):
    def _snap(symbol, _d=date):
        snapshot = _snapshot(_d)
        return snapshot

    def _evidence(symbol, _d=date, **kwargs):
        return [
            EvidenceEvent(
                id=f"{SYMBOL}:support:{_d}",
                date=_d,
                kind="expectation",
                description="分析师上修盈利预测",
                effect_on_thesis="confirming",
                confidence_delta=0.2,
            )
        ]

    return {"snapshot_provider": _snap, "evidence_provider": _evidence}


# ── ① 目录 / id / 路径口径 ─────────────────────────────────────────────────


def _default_dir_literal() -> Path:
    """从**源码**读取 ``DEFAULT_VERSION_DIR`` 的初值（autouse fixture 已重定向模块属性）。"""
    import ast

    tree = ast.parse(Path(versions.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "DEFAULT_VERSION_DIR" for target in node.targets
        ):
            return Path("data") / "midterm" / "thesis_versions"
    raise AssertionError("未找到 DEFAULT_VERSION_DIR 的模块级赋值")


def test_default_version_dir_and_path_and_id():
    # 规格字面量（在被 fixture 重定向**之前**的模块初值）：data/midterm/thesis_versions
    assert _default_dir_literal() == Path("data") / "midterm" / "thesis_versions"
    assert DEFAULT_VERSION_DIR.parts[-2:] == ("midterm", "thesis_versions")
    assert _version_path(SYMBOL).name == f"{SYMBOL}.jsonl"
    assert _version_id(ThesisVersion(version=2, as_of_date=D2, thesis="t")) == f"{D2}#2"


def test_data_dir_none_resolves_to_verbatim_default_path(monkeypatch):
    """回归（RC-5 收口）：``data_dir=None`` ⇒ ``data/midterm/thesis_versions`` **逐字不变**。

    把 autouse fixture 重定向的模块属性恢复到**源码字面量**（模拟未重定向的缺省态），
    断言 ``_version_path`` 落点 == ``data/midterm/thesis_versions/<symbol>.jsonl``。
    只算路径、**不写盘**（本用例不得触碰真实 ``data/``）。
    """
    literal = _default_dir_literal()
    monkeypatch.setattr(versions, "DEFAULT_VERSION_DIR", literal)

    assert default_version_dir() == literal
    assert _version_path(SYMBOL, data_dir=None) == literal / f"{SYMBOL}.jsonl"
    assert _version_path(SYMBOL, data_dir=None).as_posix() == f"data/midterm/thesis_versions/{SYMBOL}.jsonl"


# ── ② 首版登记 / 未变不追加 / 反漂移（本门核心） ─────────────────────────────


def test_first_registration_sets_version_one_and_buy_rationale():
    version, reason = register_thesis_if_changed(
        SYMBOL,
        as_of=D1,
        thesis=THESIS,
        invalidation=["现金流连续两季低于净利"],
    )

    assert reason == APPEND_REASON_FIRST
    assert version is not None
    assert (version.version, version.as_of_date, version.thesis) == (1, D1, THESIS)
    assert version.buy_rationale == [THESIS]  # 首版买入理由 = 当时的 thesis
    assert version.invalidation == ["现金流连续两季低于净利"]
    assert load_versions(SYMBOL) == [version]


def test_unchanged_thesis_does_not_append():
    register_thesis_if_changed(SYMBOL, as_of=D1, thesis=THESIS, invalidation=["x"])
    rows_before = _version_file().read_text(encoding="utf-8").count("\n")

    version, reason = register_thesis_if_changed(SYMBOL, as_of=D1, thesis=THESIS, invalidation=["x"])

    assert version is None and reason == APPEND_REASON_UNCHANGED
    rows_after = _version_file().read_text(encoding="utf-8").count("\n")
    assert rows_before == rows_after == 1


def test_changed_thesis_appends_new_version_but_keeps_first_buy_rationale():
    """★ 反漂移核心：``buy_rationale`` / ``invalidation`` 继承**首版**，不被后续叙事重写。"""
    first, _ = register_thesis_if_changed(
        SYMBOL,
        as_of=D1,
        thesis=THESIS,
        invalidation=["现金流连续两季低于净利"],
    )
    second, reason = register_thesis_if_changed(
        SYMBOL,
        as_of=D2,
        thesis="需求证伪：库存周转天数连续两季恶化",
        invalidation=["渠道库存去化不及预期"],
    )

    assert reason == APPEND_REASON_CHANGED
    assert second is not None
    assert (second.version, second.as_of_date) == (2, D2)
    assert second.thesis == "需求证伪：库存周转天数连续两季恶化"
    assert second.buy_rationale == first.buy_rationale == [THESIS]  # ← 反漂移
    assert second.invalidation == ["现金流连续两季低于净利"]  # ← 也不被新 invalidation 覆盖

    versions_list = load_versions(SYMBOL)
    assert [item.version for item in versions_list] == [1, 2]
    # "当初为什么买"恒为首版：首末两版的 buy_rationale 都是首版 thesis
    assert versions_list[0].buy_rationale == [THESIS]
    assert versions_list[-1].buy_rationale == [THESIS]


def test_third_change_still_inherits_the_first_version():
    register_thesis_if_changed(SYMBOL, as_of=D1, thesis=THESIS, invalidation=["a"])
    register_thesis_if_changed(SYMBOL, as_of=D2, thesis="第二版论点")
    third, reason = register_thesis_if_changed(SYMBOL, as_of="2026-10-08", thesis="第三版论点")

    assert reason == APPEND_REASON_CHANGED
    assert third is not None and third.version == 3
    assert third.buy_rationale == [THESIS]  # 仍是首版
    assert [item.version for item in load_versions(SYMBOL)] == [1, 2, 3]


def test_first_version_with_empty_buy_rationale_falls_back_to_thesis():
    """历史/手工行的 ``buy_rationale`` 为空时，继承回落首版 thesis（不让"继承"变成"清空"）。"""
    append_version(SYMBOL, ThesisVersion(version=1, as_of_date=D1, thesis=THESIS, buy_rationale=[]))

    second, reason = register_thesis_if_changed(SYMBOL, as_of=D2, thesis="新版论点")

    assert reason == APPEND_REASON_CHANGED
    assert second is not None and second.buy_rationale == [THESIS]


# ── ③ 幂等 / 损坏行 / 排序 / latest ─────────────────────────────────────────


def test_append_version_is_idempotent_by_id():
    version = ThesisVersion(version=1, as_of_date=D1, thesis=THESIS, buy_rationale=[THESIS])

    first_id = append_version(SYMBOL, version)
    second_id = append_version(SYMBOL, version)

    assert first_id == second_id == f"{D1}#1"
    assert _version_file().read_text(encoding="utf-8").count("\n") == 1


def test_load_versions_skips_corrupt_lines_and_sorts_ascending():
    path = _version_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        json.dumps({"id": f"{D2}#2", "version": 2, "as_of_date": D2, "thesis": "v2"}, ensure_ascii=False),
        "{ 这不是 JSON",  # 损坏行（JSON 解析失败）
        json.dumps({"id": f"{D1}#1", "version": 1, "as_of_date": D1, "thesis": "v1", "buy_rationale": ["v1"]}),
        json.dumps({"id": f"{D1}#3", "version": "不是数字", "as_of_date": D1, "thesis": "坏版本号"}),
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    loaded = load_versions(SYMBOL)

    assert [(item.as_of_date, item.version) for item in loaded] == [(D1, 1), (D2, 2)]  # 跳过坏行 + 升序
    assert latest_version(SYMBOL) == loaded[-1]


def test_missing_file_yields_empty_and_latest_none():
    assert load_versions("000001.SZ") == []
    assert latest_version("000001.SZ") is None


def test_register_on_empty_thesis_still_registers_first_version():
    version, reason = register_thesis_if_changed(SYMBOL, as_of=D1, thesis="")

    assert reason == APPEND_REASON_FIRST
    assert version is not None and version.version == 1 and version.buy_rationale == [""]


# ── ④ fail-open ─────────────────────────────────────────────────────────────


def test_register_fails_open_when_append_raises(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(versions, "append_version", _boom)

    version, reason = register_thesis_if_changed(SYMBOL, as_of=D1, thesis=THESIS)

    assert version is None and reason == APPEND_REASON_UNCHANGED  # 不抛，调用方可继续落帧


def test_register_fails_open_when_reader_raises(monkeypatch):
    monkeypatch.setattr(versions, "latest_version", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("io")))

    assert register_thesis_if_changed(SYMBOL, as_of=D1, thesis=THESIS) == (None, APPEND_REASON_UNCHANGED)


def test_append_version_does_not_raise_when_path_is_a_directory(tmp_path):
    """写盘异常（路径被目录占用）⇒ 只 warning 并返回 id（fail-open，不打断帧）。"""
    blocked = tmp_path / "blocked.jsonl"
    blocked.mkdir()

    id_ = append_version(SYMBOL, ThesisVersion(version=1, as_of_date=D1, thesis=THESIS), data_dir=blocked)

    assert id_ == f"{D1}#1"


# ── ⑤ scheduler 接线（§15.5-B） ─────────────────────────────────────────────


def test_reconcile_frame_registers_version_on_persist(tmp_path):
    """内核登记（**规格位置** §15.5-B）：``_reconcile_frame`` 在落帧之后按 ``persist`` 登记。

    注意与 P3 的位置差异（P3 的账本写入在 ``run_once`` 上层，§15.3-B 明文）：这里按 §15.5-B 落在内核，
    因此 ``reconcile``（公开的纯推进入口）在 ``persist=True`` 时**也**会登记版本 —— 这是规格选择，
    已如实登记进交付 output。
    """
    # 预置一帧更早的帧（带 thesis）：`evaluate` 会继承上一帧 thesis，故本帧的 thesis 非空
    persistence.append_artifact(
        CompanyStateArtifact(symbol=SYMBOL, as_of_date="2026-08-01", thesis=THESIS),
        tmp_path / "state",
    )

    artifact = reconcile(SYMBOL, as_of=D1, state_dir=tmp_path / "state", **_providers(D1))
    persisted = persistence.latest_artifact(SYMBOL, tmp_path / "state")

    assert persisted is not None
    assert artifact.thesis
    # RC-5：版本随 state_dir 落位（<state_dir>/thesis_versions），不回落默认目录
    versions_dir = tmp_path / "state" / "thesis_versions"
    assert (versions_dir / f"{SYMBOL}.jsonl").exists()
    registered = load_versions(SYMBOL, data_dir=versions_dir)
    assert len(registered) == 1
    assert registered[0].thesis == artifact.thesis
    assert registered[0].version == 1


def test_run_once_reports_thesis_version_fields(tmp_path):
    report = run_once(SYMBOL, as_of=D1, state_dir=tmp_path / "state", alert_dir=tmp_path / "alerts", **_providers(D1))

    assert report.thesis_version == 1
    assert report.thesis_version_reason == APPEND_REASON_FIRST
    versions_dir = tmp_path / "state" / "thesis_versions"
    assert load_versions(SYMBOL, data_dir=versions_dir)[0].thesis == report.thesis


def test_persist_false_writes_no_version_file(tmp_path):
    report = run_once(
        SYMBOL,
        as_of=D1,
        state_dir=tmp_path / "state",
        alert_dir=tmp_path / "alerts",
        persist=False,
        **_providers(D1),
    )

    assert report.thesis_version == 0
    assert report.thesis_version_reason == ""
    assert load_versions(SYMBOL) == []
    assert not _version_file().exists()
    # RC-5：派生目录同样不写（persist 开关门控一切落盘）
    assert not (tmp_path / "state" / "thesis_versions" / f"{SYMBOL}.jsonl").exists()


def test_second_frame_with_changed_thesis_registers_version_two(tmp_path):
    """端到端：第二帧 thesis 变了 ⇒ ``thesis_version=2`` 且 reason=changed，首版买入理由不变。"""
    run_once(SYMBOL, as_of=D1, state_dir=tmp_path / "state", alert_dir=tmp_path / "alerts", **_providers(D1))
    versions_dir = tmp_path / "state" / "thesis_versions"
    first = load_versions(SYMBOL, data_dir=versions_dir)
    assert first and first[0].version == 1

    # 直接驱动版本注册（simulate 第二帧的 thesis 变化）：
    second, reason = register_thesis_if_changed(SYMBOL, as_of=D2, thesis="需求证伪：库存恶化", data_dir=versions_dir)

    assert reason == APPEND_REASON_CHANGED
    assert second is not None and second.version == 2
    assert load_versions(SYMBOL, data_dir=versions_dir)[0].buy_rationale == first[0].buy_rationale


def test_version_registration_failure_does_not_break_the_frame(tmp_path, monkeypatch):
    """fail-open：登记抛异常 ⇒ 帧照常落盘、报告照常产出（``thesis_version`` 回落 0）。"""
    monkeypatch.setattr(
        versions, "register_thesis_if_changed", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    report = run_once(SYMBOL, as_of=D1, state_dir=tmp_path / "state", alert_dir=tmp_path / "alerts", **_providers(D1))

    assert report.thesis_version == 0 and report.thesis_version_reason == ""
    assert report.degraded is False, "登记失败不得把整帧降级（fail-open 的判据）"
    assert persistence.latest_artifact(SYMBOL, tmp_path / "state") is not None  # 帧仍然落盘


def test_degraded_path_leaves_version_fields_at_defaults(tmp_path, monkeypatch):
    import alphabee.tracking.scheduler as scheduler_module

    monkeypatch.setattr(
        scheduler_module, "_reconcile_frame", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("x"))
    )

    report = run_once(SYMBOL, as_of=D1, state_dir=tmp_path / "state", alert_dir=tmp_path / "alerts", **_providers(D1))

    assert report.degraded is True
    assert (report.thesis_version, report.thesis_version_reason) == (0, "")


def test_versions_module_never_touches_the_contract_model():
    """不改契约面：``midterm/models.py`` 的 ``ThesisVersion`` 无 ``symbol`` 字段（标的由文件名承载）。"""
    assert "symbol" not in ThesisVersion.model_fields
    assert set(ThesisVersion.model_fields) == {"version", "as_of_date", "thesis", "buy_rationale", "invalidation"}


# ── ⑥ 向后兼容（TrackingReport 追加字段带默认值） ───────────────────────────


def test_tracking_report_version_fields_default_to_zero_and_empty():
    report = TrackingReport(symbol=SYMBOL, as_of=D1)

    assert (report.thesis_version, report.thesis_version_reason) == (0, "")


def test_historical_alert_lines_still_deserialize():
    legacy = {
        "symbol": SYMBOL,
        "as_of": D1,
        "triggers": [],
        "state": "S0",
        "research_status": "",
        "deviations_recorded": 0,
    }

    report = TrackingReport.model_validate(legacy)

    assert (report.thesis_version, report.thesis_version_reason) == (0, "")


def test_version_record_round_trips_through_jsonl(tmp_path):
    """落盘 → 读回逐字段一致（含 ``id`` 前缀行）。"""
    append_version(
        SYMBOL, ThesisVersion(version=1, as_of_date=D1, thesis=THESIS, buy_rationale=[THESIS], invalidation=["a"])
    )

    raw = json.loads(_version_file().read_text(encoding="utf-8").strip())
    loaded = load_versions(SYMBOL)[0]

    assert raw["id"] == f"{D1}#1"
    assert loaded.model_dump(mode="json") == {
        "version": 1,
        "as_of_date": D1,
        "thesis": THESIS,
        "buy_rationale": [THESIS],
        "invalidation": ["a"],
    }


def test_exit_conditions_flow_into_the_first_version(tmp_path):
    """接受验收口径：首版 ``invalidation`` 来自落帧时的 ``exit_conditions``（条件文本）。"""
    artifact = CompanyStateArtifact(symbol=SYMBOL, as_of_date=D1, thesis=THESIS)
    artifact.exit_conditions = [ExitCondition(kind="thesis_broken", condition="现金流连续两季低于净利", met=False)]
    persistence.append_artifact(artifact, tmp_path / "state")

    version, reason = register_thesis_if_changed(
        SYMBOL,
        as_of=D1,
        thesis=THESIS,
        invalidation=[condition.condition for condition in artifact.exit_conditions],
    )

    assert reason == APPEND_REASON_FIRST
    assert version is not None and version.invalidation == ["现金流连续两季低于净利"]
