"""公司赛道组装测试（COMPANY_TRACK Phase B，B3/B4）。"""

from datetime import date

import alphabee.company_track.data as data_module
import alphabee.company_track.track as track_module
from alphabee.company_track import build_company_track
from alphabee.company_track.contracts import SegmentCollection, SegmentSnapshot


def _collection(segments=None, source="em", latest="20251231", error=None):
    if segments is None:
        segments = [
            SegmentSnapshot(
                report_date="20251231",
                segment_name="存储芯片",
                category="按产品分类",
                revenue_share=71.3,
                revenue_yoy=26.4,
                gross_margin=42.8,
                source="em",
            ),
            SegmentSnapshot(
                report_date="20251231",
                segment_name="微控制器",
                category="按产品分类",
                revenue_share=20.8,
                revenue_yoy=None,
                gross_margin=35.8,
                source="em",
            ),
            SegmentSnapshot(
                report_date="20241231",
                segment_name="存储芯片",
                category="按产品分类",
                revenue_share=68.0,
                revenue_yoy=None,
                source="em",
            ),
            SegmentSnapshot(
                report_date="20241231",
                segment_name="微控制器",
                category="按产品分类",
                revenue_share=25.0,
                revenue_yoy=None,
                source="em",
            ),
        ]
    return SegmentCollection(symbol="603986.SH", segments=segments, source=source, latest_period=latest, error=error)


def test_build_company_track_success(monkeypatch):
    monkeypatch.setattr(data_module, "fetch_business_segments", lambda *a, **k: _collection())

    artifact = build_company_track("603986.SH", sw_industry="电子", sw_code="801080.SI")
    assert artifact.symbol == "603986.SH"
    assert artifact.dominant_segment == "存储芯片"
    assert artifact.fastest_segment == "存储芯片"
    assert artifact.track_label == "存储芯片"
    assert artifact.track_method == "rule"
    assert "71.3%" in artifact.override_basis
    assert artifact.as_of_date == "20251231"
    # B4 新鲜度：报告期 + 90 天
    assert artifact.stale_after == date(2026, 3, 31).isoformat()
    # B3 并存：申万基线字段
    assert artifact.sw_industry == "电子"
    assert artifact.sw_code == "801080.SI"
    # 注记：报告期口径 + 来源
    assert any("20251231 报告期" in note for note in artifact.review_notes)
    assert any("segment_source:em" in ref for ref in artifact.source_refs)
    assert artifact.review_status == "approved"


def test_build_company_track_no_data_degraded(monkeypatch):
    monkeypatch.setattr(
        data_module, "fetch_business_segments", lambda *a, **k: _collection([], source="none", error="双源均失败")
    )
    artifact = build_company_track("603986.SH")
    assert artifact.degraded is True
    assert artifact.review_status == "rejected"
    assert artifact.track_label == ""
    assert any("双源均失败" in note for note in artifact.review_notes)


def test_build_company_track_drift_marks_needs_review(monkeypatch):
    segments = [
        SegmentSnapshot(
            report_date="20241231", segment_name="通信设备", category="按产品分类", revenue_share=60.0, source="em"
        ),
        SegmentSnapshot(
            report_date="20251231", segment_name="云服务器", category="按产品分类", revenue_share=65.0, source="em"
        ),
    ]
    monkeypatch.setattr(
        data_module, "fetch_business_segments", lambda *a, **k: _collection(segments, latest="20251231")
    )

    artifact = build_company_track("603986.SH")
    assert artifact.review_status == "needs_review"
    assert any("业务主线漂移" in note for note in artifact.review_notes)


def test_build_company_track_uses_annual_base_when_latest_is_h1(monkeypatch):
    # 恒瑞场景：最新期 2026H1 是「销售商品」收入性质拆分，年报 2025 是「肿瘤」→ 标签取年报
    segments = [
        SegmentSnapshot(
            report_date="20260630", segment_name="销售商品", category="按产品分类", revenue_share=90.0, source="em"
        ),
        SegmentSnapshot(
            report_date="20260630", segment_name="许可收入", category="按产品分类", revenue_share=9.0, source="em"
        ),
        SegmentSnapshot(
            report_date="20251231", segment_name="肿瘤", category="按产品分类", revenue_share=52.7, source="em"
        ),
        SegmentSnapshot(
            report_date="20251231", segment_name="神经科学", category="按产品分类", revenue_share=13.5, source="em"
        ),
    ]
    monkeypatch.setattr(
        data_module, "fetch_business_segments", lambda *a, **k: _collection(segments, latest="20260630")
    )

    artifact = build_company_track("600276.SH")
    assert artifact.track_label == "肿瘤"
    assert artifact.dominant_segment == "肿瘤"
    # 数据新鲜度仍以最新期计（stale 判定依据），但标签基于年报期
    assert artifact.as_of_date == "20260630"
    assert any("20251231 报告期" in note for note in artifact.review_notes)
    assert any("回退最近年报期" in note for note in artifact.review_notes)


def test_build_company_track_filters_passthrough(monkeypatch):
    captured = {}

    def fake_fetch(symbol, **kwargs):
        captured.update(kwargs)
        return _collection()

    monkeypatch.setattr(data_module, "fetch_business_segments", fake_fetch)
    build_company_track("603986.SH", min_share=5.0, drop_other=False)
    assert captured == {"min_share": 5.0, "drop_other": False}


# ── 人工覆盖表兜底（B3 override）────────────────────────────────────────────


def _write_overrides(tmp_path, text, name="overrides.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_override_hit_takes_precedence(monkeypatch, tmp_path):
    # 命中：标签取人工确认值，依据记「人工覆盖：」，method=override，notes 留痕
    monkeypatch.setattr(data_module, "fetch_business_segments", lambda *a, **k: _collection())
    table = _write_overrides(
        tmp_path,
        "601138.SH:\n"
        "  track_label: 云计算\n"
        "  basis: 东财按行业分类：云计算 66.75%（同比 +88.7%）主导，产品口径为粗大类单桶\n"
        "  effective_from: '20251231'\n",
    )
    monkeypatch.setattr(track_module, "_OVERRIDES_PATH", table)

    artifact = build_company_track("601138.SH", sw_industry="电子")

    assert artifact.track_label == "云计算"
    assert artifact.track_method == "override"
    assert artifact.override_basis == (
        "人工覆盖：东财按行业分类：云计算 66.75%（同比 +88.7%）主导，产品口径为粗大类单桶"
    )
    assert any("人工覆盖生效" in note and "20251231" in note for note in artifact.review_notes)
    # 覆盖只改标签/依据/方式：结构信号与取数元数据仍来自规则路径
    assert artifact.dominant_segment == "存储芯片"
    assert artifact.segments  # 仍是同一批分项
    assert artifact.sw_industry == "电子"
    assert artifact.as_of_date == "20251231"
    # 覆盖后不应再出现 LLM 复核留痕
    assert not any("LLM" in note for note in artifact.review_notes)


def test_override_miss_keeps_rule_behaviour(monkeypatch, tmp_path):
    # 未命中（表内无该 symbol）：行为与现状完全一致
    monkeypatch.setattr(data_module, "fetch_business_segments", lambda *a, **k: _collection())
    table = _write_overrides(
        tmp_path,
        "601138.SH:\n  track_label: 云计算\n  basis: 依据\n  effective_from: '20251231'\n",
    )
    monkeypatch.setattr(track_module, "_OVERRIDES_PATH", table)

    artifact = build_company_track("603986.SH")

    assert artifact.track_method == "rule"
    assert artifact.track_label == "存储芯片"
    assert "71.3%" in artifact.override_basis
    assert not any("人工覆盖" in note for note in artifact.review_notes)
    assert artifact.review_status == "approved"

    # 与「空覆盖表」基线逐字段一致（除 generated_at）
    monkeypatch.setattr(track_module, "_OVERRIDES_PATH", _write_overrides(tmp_path, "{}\n", name="empty.yaml"))
    baseline = build_company_track("603986.SH")
    for field in (
        "track_label",
        "track_method",
        "override_basis",
        "dominant_segment",
        "fastest_segment",
        "review_status",
    ):
        assert getattr(baseline, field) == getattr(artifact, field)
    assert baseline.review_notes == artifact.review_notes


def test_broken_override_table_degrades_to_rule(monkeypatch, tmp_path):
    # 无法解析的 YAML / 缺必需字段 / 文件缺失 → 不崩溃，降级规则路径 + 留痕告警
    monkeypatch.setattr(data_module, "fetch_business_segments", lambda *a, **k: _collection())

    monkeypatch.setattr(track_module, "_OVERRIDES_PATH", _write_overrides(tmp_path, "601138.SH: [未闭合\n"))
    broken = build_company_track("601138.SH")
    assert broken.track_method == "rule"
    assert broken.track_label == "存储芯片"
    assert any("人工覆盖表解析失败" in note for note in broken.review_notes)

    monkeypatch.setattr(
        track_module, "_OVERRIDES_PATH", _write_overrides(tmp_path, "601138.SH:\n  track_label: 云计算\n")
    )
    missing = build_company_track("601138.SH")
    assert missing.track_method == "rule"
    assert missing.track_label == "存储芯片"
    assert any("缺 basis/effective_from" in note for note in missing.review_notes)

    monkeypatch.setattr(track_module, "_OVERRIDES_PATH", tmp_path / "gone.yaml")
    absent = build_company_track("601138.SH")
    assert absent.track_method == "rule"
    assert absent.track_label == "存储芯片"
    assert any("人工覆盖表不可读" in note for note in absent.review_notes)

    # 单条非法不影响同表其他合法条目
    monkeypatch.setattr(
        track_module,
        "_OVERRIDES_PATH",
        _write_overrides(
            tmp_path,
            "601138.SH:\n  track_label: 云计算\n  basis: 依据\n  effective_from: '20251231'\n"
            "603986.SH: 不是映射\n"
            "002475.SZ:\n  track_label: 消费电子\n  basis: 依据\n  effective_from: '20251231'\n",
        ),
    )
    partial = build_company_track("601138.SH")
    assert partial.track_method == "override"
    assert partial.track_label == "云计算"
    assert any("条目非映射" in note for note in partial.review_notes)
