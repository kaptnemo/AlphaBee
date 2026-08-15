"""行业知识持久化单元测试（industry-context Phase 1，JSON 快照 + 过期语义）。"""

from datetime import date, timedelta

from alphabee.industry.contracts import IndustryContextArtifact
from alphabee.industry.persistence import (
    STALE_AFTER_DAYS,
    IndustryProfileStore,
    is_stale,
    suggest_stale_after,
)


def _artifact(as_of_date="2026-08-15", stale_after=None, **overrides) -> IndustryContextArtifact:
    kwargs = dict(
        industry="白酒",
        classification_standard="sw_l1",
        industry_code="801120.SI",
        as_of_date=as_of_date,
        valuation_benchmarks={"industry_pe_ttm": 25.0},
        financial_benchmarks={"industry_avg_roe": 0.15},
        growth_benchmarks={"industry_revenue_yoy": 12.3},
    )
    if stale_after is not None:
        kwargs["stale_after"] = stale_after
    kwargs.update(overrides)
    return IndustryContextArtifact(**kwargs)


# ── 存取往返 ───────────────────────────────────────────────────────────────


def test_save_load_roundtrip(tmp_path):
    store = IndustryProfileStore(root=tmp_path)
    artifact = _artifact()
    path = store.save(artifact)

    assert path.exists()
    assert path.parent.name == "sw_l1"
    assert path.name == "801120.SI.json"

    loaded = store.load("sw_l1", "801120.SI")
    assert loaded is not None
    assert loaded == artifact
    assert loaded.industry == "白酒"


def test_save_overwrites_latest_wins(tmp_path):
    store = IndustryProfileStore(root=tmp_path)
    store.save(_artifact(as_of_date="2026-08-01"))
    store.save(_artifact(as_of_date="2026-09-01"))
    loaded = store.load("sw_l1", "801120.SI")
    assert loaded.as_of_date == "2026-09-01"  # latest-wins


def test_load_missing_returns_none(tmp_path):
    store = IndustryProfileStore(root=tmp_path)
    assert store.load("sw_l1", "999999") is None


def test_load_with_filters(tmp_path):
    store = IndustryProfileStore(root=tmp_path)
    store.save(_artifact(as_of_date="2026-08-15"))
    assert store.load("sw_l1", "801120.SI", schema_version="2") is not None
    assert store.load("sw_l1", "801120.SI", schema_version="1") is None
    assert store.load("sw_l1", "801120.SI", as_of_date="2026-08-15") is not None
    assert store.load("sw_l1", "801120.SI", as_of_date="2026-01-01") is None


def test_path_for_sanitizes_codes(tmp_path):
    store = IndustryProfileStore(root=tmp_path)
    path = store.path_for("sw_l1", "../evil/801120.SI")
    # 路径组件不得逃逸存储根目录（".." 只可能出现在文件名内部，不构成目录穿越）
    resolved_root = (tmp_path / "sw_l1").resolve()
    assert path.resolve().is_relative_to(resolved_root)
    assert "/../" not in str(path)


def test_list_profiles(tmp_path):
    store = IndustryProfileStore(root=tmp_path)
    store.save(_artifact(industry="白酒"))
    store.save(
        _artifact(
            industry="银行",
            classification_standard="sw_l1",
            industry_code="801780.SI",
            stale_after="2000-01-01",
        )
    )
    infos = store.list_profiles()
    assert len(infos) == 2
    by_code = {info.industry_code: info for info in infos}
    assert by_code["801780.SI"].industry == "银行"
    assert by_code["801780.SI"].stale is True  # 过期快照被标记
    assert by_code["801120.SI"].stale is False


def test_broken_file_skipped_in_list(tmp_path):
    store = IndustryProfileStore(root=tmp_path)
    store.save(_artifact())
    broken = store.path_for("sw_l1", "801120.SI")
    broken.write_text("{not json", encoding="utf-8")
    assert store.list_profiles() == []


# ── stale_after 建议 ───────────────────────────────────────────────────────


def test_suggest_stale_after_earliest_category_wins():
    assert STALE_AFTER_DAYS["valuation"] == 30
    assert STALE_AFTER_DAYS["financial"] == 90
    assert STALE_AFTER_DAYS["growth"] == 90
    assert STALE_AFTER_DAYS["qualitative"] == 30

    # valuation(30d) + financial(90d) → 取最早 30 天
    result = suggest_stale_after("2026-08-15", {"valuation", "financial"})
    assert result == (date(2026, 8, 15) + timedelta(days=30)).isoformat()

    # 只有财务/成长 → 90 天
    result = suggest_stale_after("2026-08-15", {"financial", "growth"})
    assert result == (date(2026, 8, 15) + timedelta(days=90)).isoformat()


def test_suggest_stale_after_empty_or_invalid():
    assert suggest_stale_after("2026-08-15", set()) is None
    assert suggest_stale_after("not-a-date", {"valuation"}) is None


# ── is_stale ───────────────────────────────────────────────────────────────


def test_is_stale_with_explicit_stale_after():
    artifact = _artifact(stale_after="2026-08-10")
    assert is_stale(artifact, now=date(2026, 8, 15)) is True
    assert is_stale(artifact, now=date(2026, 8, 10)) is False  # 到期日当天未过期


def test_is_stale_fallback_to_valuation_default():
    # 无 stale_after → 按 as_of_date + 30 天兜底（2026-08-15 + 30d = 2026-09-14）
    artifact = _artifact(as_of_date="2026-08-15")
    assert is_stale(artifact, now=date(2026, 9, 14)) is False  # 到期日当天未过期
    assert is_stale(artifact, now=date(2026, 9, 15)) is True


def test_is_stale_unparseable_dates_not_stale():
    artifact = _artifact(as_of_date="bad-date", stale_after="bad-date")
    assert is_stale(artifact, now=date(2026, 9, 1)) is False
