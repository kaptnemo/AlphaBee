"""classifier_tef.py 的单元测试（TEF design §7.1）。

覆盖七条：S2/S4/S5 门控翻转、S4/S5 先导触发、背离镜像、不变式，以及
「完全自包含（不 import classifier）」的导入面/AST 专测钉。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from alphabee.midterm import classifier as etf
from alphabee.midterm import classifier_tef as tef
from alphabee.midterm.models import Consistency, VariableScores


def _scores(f=None, e=None, t=None, v=None, c=None, r=None) -> VariableScores:
    return VariableScores(
        f_fundamental_trend=f,
        e_revision=e,
        t_relative_strength=t,
        v_valuation_percentile=v,
        c_crowding=c,
        r_risk=r,
    )


def _tef_evidence(scores: VariableScores) -> dict[str, float]:
    return tef._state_evidence(scores)  # noqa: SLF001


def _etf_evidence(scores: VariableScores) -> dict[str, float]:
    return etf._state_evidence(scores)  # noqa: SLF001


# ─────────────────────────────────────────────────────────────────────────────
# §7.1.1–7.1.5：门控翻转 / 先导触发（证据值精确钉，§3.2 公式逐字）
# ─────────────────────────────────────────────────────────────────────────────


def test_s2_gate_flips_e_to_t():
    # e_up=0, t_up=1, f_up=1 ⇒ TEF S2 > 0，ETF S2 = 0（顺序决定性位置 S2）
    scores = _scores(f=1.0, e=0.0, t=1.0)
    assert _tef_evidence(scores)["S2"] > 0.0
    assert _etf_evidence(scores)["S2"] == 0.0


def test_s4_gate_flips_e_to_t():
    # t_down=0, e_not_up=1, v_down=1, c_down=1 ⇒ TEF S4 = 0（价格未破位不判充分定价）
    scores = _scores(e=-1.0, t=0.0, v=-1.0, c=-1.0)
    assert _tef_evidence(scores)["S4"] == 0.0
    assert _etf_evidence(scores)["S4"] > 0.0


def test_s4_leading_trigger():
    # t_down=1, e_not_up=0.5, v_down=1 ⇒ TEF S4 > 0（不等 E 转平）
    scores = _scores(e=None, t=-1.0, v=-1.0)
    assert _tef_evidence(scores)["S4"] > 0.0
    assert tef.classify_state(scores).state.argmax_state == "S4"


def test_s5_gate_requires_price_confirmation():
    # e_down=1, f_down=1, t_down=0 ⇒ TEF S5 = 0（无价格确认不退出），ETF S5 > 0
    scores = _scores(f=-1.0, e=-1.0, t=0.0)
    assert _tef_evidence(scores)["S5"] == 0.0
    assert _etf_evidence(scores)["S5"] > 0.0


def test_s5_breakdown_trigger():
    # t_down=1, e_down=0.5 ⇒ TEF S5 > 0
    scores = _scores(e=-0.5, t=-1.0)
    assert _tef_evidence(scores)["S5"] > 0.0


# ─────────────────────────────────────────────────────────────────────────────
# §7.1.6：背离镜像（§3.3）
# ─────────────────────────────────────────────────────────────────────────────


def test_divergence_mirror_t_up_e_down_named():
    # T↑ + E↓ ⇒ TEF DIVERGENT + 具名 reason「价格先行转强 vs 预期未跟上」
    scores = _scores(f=0.0, e=-0.5, t=0.8)
    result = tef.classify_state(scores)
    deltas = {d.factor: d for d in result.factor_deltas}
    assert deltas["E"].consistency == Consistency.DIVERGENT
    assert deltas["T"].consistency == Consistency.DIVERGENT
    assert any("价格先行" in line and "预期未跟上" in line for line in result.rationale)
    div_tasks = [t for t in result.research_tasks if t.id == "research-divergent"]
    assert div_tasks
    assert any("价格先行" in t.unknown and "预期未跟上" in t.unknown for t in div_tasks)


def test_divergence_mirror_e_up_t_down_is_independent():
    # E↑ + T↓ ⇒ TEF INDEPENDENT（不再具名背离）
    scores = _scores(f=0.0, e=0.8, t=-0.5)
    result = tef.classify_state(scores)
    deltas = {d.factor: d for d in result.factor_deltas}
    assert deltas["E"].consistency == Consistency.INDEPENDENT
    assert deltas["T"].consistency == Consistency.INDEPENDENT
    assert not any("背离" in line for line in result.rationale)


# ─────────────────────────────────────────────────────────────────────────────
# §7.1.7：不变式（S1/S3 逐值相等 + 中性常量/迁移集合相等）
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "scores",
    [
        _scores(),
        _scores(f=0.8, e=0.8, t=0.8),
        _scores(f=0.6, e=-0.5, t=-0.5, v=0.7),
        _scores(f=0.1, e=-0.3, t=-0.6, v=-0.6, c=-0.6),
        _scores(e=-0.8, f=-0.5),
    ],
)
def test_s1_s3_evidence_equal_to_etf(scores):
    assert _tef_evidence(scores)["S1"] == pytest.approx(_etf_evidence(scores)["S1"])
    assert _tef_evidence(scores)["S3"] == pytest.approx(_etf_evidence(scores)["S3"])


def test_neutral_constants_and_migration_sets_equal_to_etf():
    for name in (
        "_S0_BASELINE",
        "_SOFTMAX_TEMPERATURE",
        "_ENTROPY_UNCERTAIN",
        "_UP_T",
        "_DOWN_T",
        "_ACTIVE_T",
        "_DRIFT_EPS",
    ):
        assert getattr(tef, name) == getattr(etf, name), name
    for name in ("_STATES", "_FORWARD", "_BACKWARD", "_REOPEN"):
        assert getattr(tef, name) == getattr(etf, name), name


# ─────────────────────────────────────────────────────────────────────────────
# 完全自包含（§2 原则 1 / §10.2）：只 import models，不 import classifier
# ─────────────────────────────────────────────────────────────────────────────


def test_tef_module_imports_only_models_not_classifier():
    src = Path(tef.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert "alphabee.midterm.models" in imported
    assert "alphabee.midterm.classifier" not in imported
    assert "classifier" not in imported
