from alphabee.agents.thesis.critic import CriticEngine
from alphabee.agents.thesis.models import EvidenceItem, InvestmentThesis, ThesisDimension
from alphabee.agents.thesis.tools import _render_thesis


def test_critic_engine_consumes_counter_missing_and_context_notes():
    thesis = InvestmentThesis(
        symbol="600519.SH",
        period="2024Q4",
        dimensions={
            "financial_quality": ThesisDimension(
                id="financial_quality",
                name="财务质量",
                judgment="negative",
                score=-0.6,
                confidence=0.8,
                evidence=[
                    EvidenceItem(
                        signal_id="cross_validation_break",
                        signal_name="cross_validation_break",
                        level="high",
                        impact="negative",
                    )
                ],
                counter_evidence=["行业景气并未同步恶化"],
                missing_evidence=["前五大客户回款集中度"],
                context_notes=["项目制业务会天然拉长验收回款周期"],
            )
        },
    )

    enriched = CriticEngine().enrich(thesis, {})
    questions = [q.question for q in enriched.critic_questions]
    categories = {q.category for q in enriched.critic_questions}

    assert any("反向证据：行业景气并未同步恶化" in q for q in questions)
    assert any("缺少关键证据：前五大客户回款集中度" in q for q in questions)
    assert any("语境校准：项目制业务会天然拉长验收回款周期" in q for q in questions)
    assert {"counter_evidence", "evidence_gap", "industry_cycle"} <= categories


def test_render_thesis_shows_new_evidence_sections():
    thesis = InvestmentThesis(
        symbol="600519.SH",
        period="2024Q4",
        dimensions={
            "earnings_quality": ThesisDimension(
                id="earnings_quality",
                name="盈利质量",
                judgment="negative",
                score=-0.7,
                confidence=0.9,
                interpretation="盈利质量偏弱。",
                evidence=[
                    EvidenceItem(
                        signal_id="anomaly_pattern:inflated_revenue",
                        signal_name="虚增收入嫌疑",
                        level="high",
                        impact="negative",
                        interpretation="收入增长没有被真实回款支撑",
                        source_type="anomaly",
                    )
                ],
                counter_evidence=["行业因素解释不成立"],
                missing_evidence=["客户回款明细"],
                context_notes=["项目制业务需结合验收节奏理解应收变化"],
            )
        },
    )

    rendered = _render_thesis(thesis)

    assert "[异常模式]" in rendered
    assert "**反向证据**" in rendered
    assert "**缺失证据**" in rendered
    assert "**语境说明**" in rendered
    assert "收入增长没有被真实回款支撑" in rendered
