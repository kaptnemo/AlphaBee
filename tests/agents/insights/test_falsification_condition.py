"""证伪条件类型化：FalsificationCondition + 向后兼容 coercion + 报告按 kind 渲染。"""

from alphabee.agents.insights.models import InsightOutput
from alphabee.agents.schemas import (
    FalsificationCondition,
    coerce_falsification_conditions,
)
from alphabee.orchestrator.contracts import InsightArtifact
from alphabee.orchestrator.reporter import _falsification_markdown


def test_falsification_condition_normalizes_kind_synonyms():
    assert FalsificationCondition(condition="c", kind="falsify").kind == "disconfirm"
    assert FalsificationCondition(condition="c", kind="strengthen").kind == "confirm"
    assert FalsificationCondition(condition="c", kind="escalation").kind == "escalate"
    assert FalsificationCondition(condition="c").kind == "disconfirm"  # 默认


def test_coerce_falsification_conditions_backward_compat():
    result = coerce_falsification_conditions(
        [
            "纯字符串条件",
            {"condition": "确认条件", "kind": "confirm", "direction": "加强"},
        ]
    )
    assert len(result) == 2
    assert result[0].condition == "纯字符串条件"
    assert result[0].kind == "disconfirm"  # 旧字符串 → 默认证伪
    assert result[1].condition == "确认条件"
    assert result[1].kind == "confirm"
    assert result[1].direction == "加强"


def test_insight_output_types_conditions_with_kind_preserved():
    output = InsightOutput(
        core_view="观点",
        central_tension="矛盾",
        main_driver="驱动",
        what_would_change_my_mind=[
            "若应收账龄改善则削弱负面判断",
            {"condition": "若毛利率抬升", "kind": "confirm", "direction": "加强当前观点"},
            {"condition": "若担保爆雷", "kind": "escalate", "direction": "风险升级"},
        ],
    )
    conds = output.what_would_change_my_mind
    assert len(conds) == 3
    assert conds[0].kind == "disconfirm"
    assert conds[1].kind == "confirm"
    assert conds[2].kind == "escalate"


def test_insight_artifact_backward_compat_strings():
    artifact = InsightArtifact.model_validate({"core_view": "观点", "what_would_change_my_mind": ["旧字符串条件"]})
    conds = artifact.what_would_change_my_mind
    assert len(conds) == 1
    assert conds[0].condition == "旧字符串条件"
    assert conds[0].kind == "disconfirm"


def test_falsification_markdown_groups_by_kind_and_clarifies_disconfirm():
    markdown = _falsification_markdown(
        [
            FalsificationCondition(condition="条件A", kind="disconfirm"),
            FalsificationCondition(condition="条件B", kind="confirm"),
        ]
    )
    assert "[证伪] 条件A" in markdown
    assert "该条件削弱当前观点，而非证明相反观点" in markdown
    assert "[确认] 条件B" in markdown
