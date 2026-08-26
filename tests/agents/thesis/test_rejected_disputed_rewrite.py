"""P0-③：rejected 假设回写 dimension 分数（含双重计数修复）。"""

from alphabee.agents.thesis.engine import ThesisEngine


def _high_cash_high_debt_inputs(
    *,
    disputed_pattern=None,
    disputed_signal=None,
    explanation="账上现金和有息负债同时偏高，属于大存大贷，需排查资金占用。",
):
    """构造“大存大贷”模式的信号/异常/冲突/验证输入。

    该模式会以两条路径各扣一次 credit_risk：
      - 信号路径 signal_id=anomaly_pattern_high_cash_high_debt (source_type=signal)
      - 异常直连 signal_id=anomaly_pattern:high_cash_high_debt (source_type=anomaly)
    """
    signal_results = {
        "anomaly_pattern_high_cash_high_debt": {
            "level": "high",
            "interpretation": "账上现金和有息负债同时显著偏高，不符合常规商业逻辑。",
            "thesis_impact": {"credit_risk": "negative"},
        }
    }
    anomaly_report = {
        "pattern_matches": [
            {
                "pattern_id": "high_cash_high_debt",
                "pattern_name": "大存大贷",
                "severity": "high",
                "risk_dimension": "credit_risk",
                "explanation": "货币资金和有息负债同时显著偏高。",
            }
        ]
    }
    hypothesis = {
        "id": "h1",
        "explanation": explanation,
        "status": "pending",
        "disputed_pattern_ids": disputed_pattern or [],
        "disputed_signal_ids": disputed_signal or [],
    }
    conflict_analysis = {
        "conflicts": [
            {
                "id": "c1",
                "theme": "大存大贷是否为资金占用",
                "description": "货币资金与有息负债同时偏高。",
                "related_dimensions": ["credit_risk"],
                "severity": "high",
                "confidence": 0.8,
                "hypotheses": [hypothesis],
            }
        ]
    }
    verification_results = [
        {
            "hypothesis_id": "h1",
            "status": "rejected",
            "summary": "公司本期完成再融资，货币资金激增源于融资行为，不构成资金占用。",
            "gaps": [],
        }
    ]
    return signal_results, anomaly_report, conflict_analysis, verification_results


def _run_thesis(signal_results, anomaly_report, conflict_analysis, verification_results):
    return ThesisEngine().run(
        symbol="002130.SZ",
        period="20260630",
        signal_results=signal_results,
        anomaly_report=anomaly_report,
        conflict_analysis=conflict_analysis,
        verification_results=verification_results,
    )


def test_rejected_hypothesis_rewrites_credit_risk_score_and_removes_both_paths():
    inputs = _high_cash_high_debt_inputs(
        disputed_pattern=["high_cash_high_debt"],
        disputed_signal=["anomaly_pattern_high_cash_high_debt"],
    )
    thesis = _run_thesis(*inputs)

    dim = thesis.dimensions["credit_risk"]

    # 被证伪后，credit_risk 不再被压成 strong_negative，而是回到中性。
    assert dim.judgment != "strong_negative"
    assert dim.score == 0.0

    # 双重计数验证：signal 与 anomaly 两条贡献路径必须同时被移除。
    disputed_sids = {"anomaly_pattern_high_cash_high_debt", "anomaly_pattern:high_cash_high_debt"}
    assert not any(e.signal_id in disputed_sids for e in dim.evidence)

    # 反证文字已写入维度，供报告端呈现“为什么这条维度被修订”。
    assert any("再融资" in item for item in dim.counter_evidence)


def test_rejected_hypothesis_falls_back_to_keyword_inference():
    # 不显式给 disputed_*，靠解释文字里的“大存大贷”关键词兜底命中。
    thesis = _run_thesis(*_high_cash_high_debt_inputs())

    dim = thesis.dimensions["credit_risk"]
    assert dim.judgment != "strong_negative"
    assert dim.score == 0.0


def test_rejected_hypothesis_without_match_keeps_score_conservatively():
    # 解释文字不含任何模式名/信号 id，兜底未命中 → 保守不扣分，保持原判定。
    thesis = _run_thesis(*_high_cash_high_debt_inputs(explanation="公司账务处理符合会计准则。"))

    dim = thesis.dimensions["credit_risk"]
    assert dim.judgment == "strong_negative"
    assert dim.score < -0.5
