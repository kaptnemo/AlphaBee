"""商业模式分类测试（COMPANY_TRACK Phase E，E1/E2）。"""

from alphabee.company_track import (
    BUSINESS_MODEL_LABELS,
    BUSINESS_MODELS,
    classify_business_model,
)


def test_archetype_catalog():
    assert set(BUSINESS_MODELS) == {"brand", "odm", "component", "integrator", "other"}
    assert BUSINESS_MODEL_LABELS["odm"] == "ODM/OEM 代工商"
    assert BUSINESS_MODEL_LABELS["component"] == "核心零部件商"


def test_odm_low_margin_low_rd():
    model, evidence = classify_business_model(gross_margin=0.12, rd_ratio=0.04)
    assert model == "odm"
    assert "毛利率 12.0%" in evidence
    assert "研发费率 4.0%" in evidence


def test_component_high_margin_high_rd():
    model, evidence = classify_business_model(gross_margin=0.55, rd_ratio=0.18)
    assert model == "component"


def test_brand_high_margin_low_rd():
    model, _ = classify_business_model(gross_margin=0.65, rd_ratio=0.05)
    assert model == "brand"


def test_integrator_mid_margin_mid_rd():
    model, _ = classify_business_model(gross_margin=0.30, rd_ratio=0.12)
    assert model == "integrator"


def test_outside_bands_other():
    model, evidence = classify_business_model(gross_margin=0.30, rd_ratio=0.03)
    assert model == "other"
    assert "人工确认" in evidence


def test_missing_metrics_other_not_guessing():
    model, evidence = classify_business_model(gross_margin=None, rd_ratio=0.10)
    assert model == "other"
    assert "指标不足" in evidence
    # 全部缺失也不猜测
    assert classify_business_model()[0] == "other"


def test_customer_concentration_bolsters_odm():
    model, evidence = classify_business_model(gross_margin=0.12, rd_ratio=0.04, customer_concentration=0.60)
    assert model == "odm"
    assert "高客户集中度佐证" in evidence


def test_llm_review_success(monkeypatch):
    import alphabee.utils.llm as llm_module

    class FakeModel:
        def invoke(self, prompt):
            return type(
                "R",
                (),
                {"content": '{"business_model": "odm", "evidence": "代工模式，毛利率 12%"}'},
            )()

    monkeypatch.setattr(llm_module, "create_chat_model", lambda component, **kw: FakeModel())
    model, evidence = classify_business_model(gross_margin=0.12, rd_ratio=0.04, use_llm=True)
    assert model == "odm"
    assert evidence == "代工模式，毛利率 12%"


def test_llm_review_failure_falls_back(monkeypatch):
    import alphabee.utils.llm as llm_module

    def boom(component, **kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(llm_module, "create_chat_model", boom)
    model, evidence = classify_business_model(gross_margin=0.12, rd_ratio=0.04, use_llm=True)
    assert model == "odm"  # 回退规则
    assert "毛利率 12.0%" in evidence


def test_llm_invalid_model_ignored(monkeypatch):
    import alphabee.utils.llm as llm_module

    class FakeModel:
        def invoke(self, prompt):
            return type("R", (), {"content": '{"business_model": "unicorn"}'})()

    monkeypatch.setattr(llm_module, "create_chat_model", lambda component, **kw: FakeModel())
    model, _ = classify_business_model(gross_margin=0.12, rd_ratio=0.04, use_llm=True)
    assert model == "odm"  # 非法枚举 → 回退规则
