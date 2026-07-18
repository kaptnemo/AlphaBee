from alphabee.agents.thesis.engine import ThesisEngine


def test_thesis_engine_explicitly_consumes_anomaly_conflict_and_verification():
    thesis = ThesisEngine().run(
        symbol="600519.SH",
        period="2024Q4",
        signal_results={
            "growth_signal": {
                "level": "low",
                "interpretation": "利润表增速仍为正。",
                "thesis_impact": {"earnings_quality": "positive"},
            }
        },
        anomaly_report={
            "pattern_matches": [
                {
                    "pattern_id": "inflated_revenue",
                    "pattern_name": "虚增收入嫌疑",
                    "severity": "high",
                    "risk_dimension": "earnings_quality",
                    "explanation": "收入增长没有被真实回款支撑。",
                }
            ]
        },
        conflict_analysis={
            "conflicts": [
                {
                    "id": "c1",
                    "theme": "盈利增长但现金流恶化",
                    "description": "利润增长未被现金流验证。",
                    "related_dimensions": ["earnings_quality", "financial_quality"],
                    "severity": "critical",
                    "confidence": 0.9,
                    "hypotheses": [
                        {"id": "h1", "explanation": "收入质量不足", "status": "pending"},
                        {"id": "h2", "explanation": "行业因素导致毛利率下降", "status": "pending"},
                        {"id": "h3", "explanation": "客户回款节奏仍待验证", "status": "pending"},
                    ],
                }
            ]
        },
        verification_results=[
            {
                "hypothesis_id": "h1",
                "status": "verified",
                "summary": "现金流未能验证利润增长。",
                "gaps": ["客户回款明细"],
            },
            {
                "hypothesis_id": "h2",
                "status": "rejected",
                "summary": "行业因素解释不成立。",
                "gaps": [],
            },
            {
                "hypothesis_id": "h3",
                "status": "unknown",
                "summary": "仍需补充客户回款证据。",
                "gaps": ["前五大客户回款集中度"],
            },
        ],
    )

    dim = thesis.dimensions["earnings_quality"]

    assert dim.judgment in ("negative", "strong_negative")
    assert any(
        item.source_type == "anomaly" and item.signal_id == "anomaly_pattern:inflated_revenue"
        for item in dim.evidence
    )
    assert any(item.source_type == "conflict" for item in dim.evidence)
    assert "行业因素解释不成立。" in dim.counter_evidence
    assert "前五大客户回款集中度" in dim.missing_evidence
    assert thesis.primary_risks


def test_company_context_tempers_negative_growth_dimensions():
    signal_results = {
        "expansion_risk": {
            "level": "medium",
            "interpretation": "扩张阶段利润承压。",
            "thesis_impact": {"growth_quality": "negative"},
        }
    }

    base = ThesisEngine().run(
        symbol="300750.SZ",
        period="2024Q4",
        signal_results=signal_results,
    )
    contextual = ThesisEngine().run(
        symbol="300750.SZ",
        period="2024Q4",
        signal_results=signal_results,
        company_context={"lifecycle_stage": "growth"},
    )

    assert contextual.dimensions["growth_quality"].score > base.dimensions["growth_quality"].score
    assert contextual.dimensions["growth_quality"].context_notes
