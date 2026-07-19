"""Tests for InsightAgent models."""

import pytest
from pydantic import ValidationError

from alphabee.agents.insights.models import (
    EvidenceItem,
    InsightOutput,
    MaterialityRank,
)


class TestEvidenceItem:
    def test_valid_minimal(self):
        item = EvidenceItem(statement="应收增速高于收入增速", source="signal:receivable_quality")
        assert item.statement == "应收增速高于收入增速"
        assert item.source == "signal:receivable_quality"
        assert item.weight == "moderate"

    def test_valid_explicit_weight(self):
        item = EvidenceItem(statement="毛利率稳定", source="derived_fact:gross_margin", weight="strong")
        assert item.weight == "strong"

    def test_invalid_weight_rejected(self):
        with pytest.raises(ValidationError):
            EvidenceItem(statement="x", source="y", weight="invalid")

    def test_to_dict(self):
        item = EvidenceItem(statement="现金流恶化", source="anomaly:ocf_decline", weight="strong")
        d = item.model_dump(mode="json")
        assert d["statement"] == "现金流恶化"
        assert d["weight"] == "strong"


class TestMaterialityRank:
    def test_valid(self):
        rank = MaterialityRank(
            variable="应收账款质量",
            importance="critical",
            reasoning="决定盈利是否可转化为现金",
        )
        assert rank.importance == "critical"

    def test_to_dict(self):
        rank = MaterialityRank(variable="毛利率", importance="high", reasoning="反映定价权")
        d = rank.model_dump(mode="json")
        assert d["variable"] == "毛利率"
        assert d["importance"] == "high"


class TestInsightOutput:
    def test_valid_minimal(self):
        output = InsightOutput(
            core_view="公司增长质量下降，当前估值需要更强的利润兑现能力支撑",
            central_tension="市场高成长定价 vs 财务质量恶化",
            main_driver="应收账款回收情况",
            what_would_change_my_mind=["若应收账龄改善，将推翻核心负面判断"],
        )
        assert "增长质量下降" in output.core_view
        assert output.supporting_evidence == []
        assert output.confidence == "medium"

    def test_full_construction(self):
        output = InsightOutput(
            core_view="基本面稳健但估值已充分反映，上行空间有限",
            central_tension="稳健经营 vs 估值天花板",
            main_driver="未来两个季度的收入增速",
            supporting_evidence=[
                EvidenceItem(statement="毛利率行业领先", source="derived_fact:gross_margin", weight="strong"),
                EvidenceItem(statement="现金流健康", source="signal:cashflow_quality", weight="strong"),
            ],
            counter_evidence=[
                EvidenceItem(statement="PE高于历史中位数", source="market:pe_ttm", weight="moderate"),
            ],
            materiality_rank=[
                MaterialityRank(variable="毛利率", importance="critical", reasoning="核心盈利能力的锚"),
                MaterialityRank(variable="收入增速", importance="high", reasoning="决定估值消化速度"),
            ],
            business_model_context="消费品牌公司，轻资产模式，现金流自然优于重资产企业",
            base_case="收入增长10-15%，利润率稳定，估值温和消化",
            bull_case="新产品线超预期放量，收入增速重回20%+",
            bear_case="行业竞争加剧，毛利率承压，估值下修",
            what_would_change_my_mind=[
                "若毛利率连续两个季度下滑超过2个百分点",
                "若新产品线季度收入超过总收入的20%",
            ],
            confidence="medium",
        )
        assert len(output.supporting_evidence) == 2
        assert len(output.counter_evidence) == 1
        assert len(output.materiality_rank) == 2
        assert len(output.what_would_change_my_mind) == 2
        assert output.business_model_context != ""

    def test_serialization_roundtrip(self):
        original = InsightOutput(
            core_view="测试观点",
            central_tension="测试矛盾",
            main_driver="测试变量",
            supporting_evidence=[
                EvidenceItem(statement="证据1", source="signal:s1", weight="strong"),
            ],
            counter_evidence=[
                EvidenceItem(statement="反证1", source="signal:s2", weight="weak"),
            ],
            materiality_rank=[
                MaterialityRank(variable="变量1", importance="critical", reasoning="原因"),
            ],
            business_model_context="测试语境",
            base_case="基准",
            bull_case="乐观",
            bear_case="悲观",
            what_would_change_my_mind=["条件1", "条件2"],
            confidence="high",
        )

        d = original.model_dump(mode="json")
        restored = InsightOutput.model_validate(d)
        assert restored.core_view == original.core_view
        assert restored.central_tension == original.central_tension
        assert len(restored.supporting_evidence) == 1
        assert len(restored.counter_evidence) == 1
        assert len(restored.what_would_change_my_mind) == 2
        assert restored.confidence == "high"
