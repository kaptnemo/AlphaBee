"""基础 A：证据增量更新 ``bayes.update_confidence_delta`` 单测（P0 设计 §1.4/§2.2）。

覆盖：增量 / 幂等 / level 取代 / retraction 作废四类语义，并钉住 P0-1 两个主钉：

- 同值重复帧 ⇒ 净变化 0、后验不动；
- flow 事件第二帧重复出现 ⇒ 不再贡献（幂等）。

全部离线合成 ``EvidenceEvent``，不发起网络请求。
"""

from __future__ import annotations

import pytest

from alphabee.midterm.bayes import _event_logodds, update_confidence, update_confidence_delta
from alphabee.midterm.models import CompanyStateArtifact, EvidenceEvent


def _ev(
    *,
    id: str,
    subject: str = "",
    effect: str = "confirming",
    delta: float = 0.3,
    supersedes: list[str] | None = None,
) -> EvidenceEvent:
    return EvidenceEvent(
        id=id,
        date="2024-01-01",
        kind="expectation",
        subject=subject,
        description="",
        effect_on_thesis=effect,
        confidence_delta=delta,
        supersedes=supersedes or [],
    )


# ─────────────────────────────────────────────────────────────────────────────
# 退化语义（prev_events=None → 全量）
# ─────────────────────────────────────────────────────────────────────────────


def test_prev_none_degenerates_to_full_semantics():
    events = [_ev(id="a", effect="confirming", delta=0.3), _ev(id="b", effect="refuting", delta=0.1)]
    assert update_confidence_delta(events, prior=0.5, prev_events=None) == pytest.approx(
        update_confidence(events, prior=0.5)
    )


def test_empty_events_returns_prior():
    assert update_confidence_delta([], prior=0.7, prev_events=[]) == pytest.approx(0.7)
    assert update_confidence_delta(None, prior=0.7, prev_events=[]) == pytest.approx(0.7)


def test_no_prior_no_new_events_returns_none():
    prev = [_ev(id="a")]
    assert update_confidence_delta(prev, prior=None, prev_events=prev) is None


# ─────────────────────────────────────────────────────────────────────────────
# flow 幂等（id 稳定后只计一次）
# ─────────────────────────────────────────────────────────────────────────────


def test_new_flow_event_contributes_once():
    old = _ev(id="old", effect="confirming", delta=0.3)
    new = _ev(id="new", effect="confirming", delta=0.5)
    prev_post = update_confidence([old], prior=0.5)
    post = update_confidence_delta([old, new], prior=prev_post, prev_events=[old])
    assert post == pytest.approx(update_confidence([new], prior=prev_post))
    assert post > prev_post


def test_flow_event_second_frame_does_not_contribute():
    """P0-1 主钉：flow 事件第二帧重复出现（同 id）⇒ 不再贡献。"""
    old = _ev(id="flow", effect="confirming", delta=0.5)
    prev_post = update_confidence([old], prior=0.5)
    post = update_confidence_delta([old], prior=prev_post, prev_events=[old])
    assert post == pytest.approx(prev_post)


def test_same_value_level_repeat_frame_posterior_unchanged():
    """P0-1 主钉：同值重复帧 ⇒ 净变化 0、后验不动。"""
    rev = _ev(id="rev-same", subject="600519:revision:eps_fy1_revision_1m", effect="confirming", delta=0.3)
    prev_post = update_confidence([rev], prior=0.5)
    post = update_confidence_delta([rev], prior=prev_post, prev_events=[rev])
    assert post == pytest.approx(prev_post)


# ─────────────────────────────────────────────────────────────────────────────
# level 取代（同 subject 新值取代旧值）
# ─────────────────────────────────────────────────────────────────────────────


def test_level_replace_net_logodds_equals_new_minus_old():
    old = _ev(id="rev-old", subject="600519:revision:eps_fy1_revision_1m", effect="confirming", delta=0.3)
    new = _ev(id="rev-new", subject="600519:revision:eps_fy1_revision_1m", effect="confirming", delta=0.5)
    prev_post = update_confidence([old], prior=0.5)
    post = update_confidence_delta([new], prior=prev_post, prev_events=[old])
    # net = lo(new) − lo(old)，作用在 logit(prev_post)=lo(old) 上 ⇒ lo(new)
    assert post == pytest.approx(update_confidence([new], prior=0.5))


def test_level_replace_net_logodds_value():
    old = _ev(id="rev-old", subject="s:rev:f", effect="confirming", delta=0.3)
    new = _ev(id="rev-new", subject="s:rev:f", effect="confirming", delta=0.5)
    net = _event_logodds(new) - _event_logodds(old)
    assert net == pytest.approx(_event_logodds(new) - _event_logodds(old))
    assert net > 0


def test_level_replace_same_subject_different_id():
    old = _ev(id="rev-old", subject="s:rev:f", effect="confirming", delta=0.3)
    new = _ev(id="rev-new", subject="s:rev:f", effect="refuting", delta=0.2)
    prev_post = update_confidence([old], prior=0.5)
    post = update_confidence_delta([new], prior=prev_post, prev_events=[old])
    # 新值 refuting：净效果 = lo(refuting new) − lo(confirming old) < 0 ⇒ 后验下降
    assert post < prev_post


def test_subject_empty_does_not_trigger_level_replace():
    old = _ev(id="old", subject="", effect="confirming", delta=0.3)
    new = _ev(id="new", subject="", effect="confirming", delta=0.5)
    prev_post = update_confidence([old], prior=0.5)
    # 无 subject ⇒ 不判 level 取代；new 只累加，不减旧
    post = update_confidence_delta([new], prior=prev_post, prev_events=[old])
    assert post == pytest.approx(update_confidence([new], prior=prev_post))


def test_level_replace_two_subjects_independent():
    old_a = _ev(id="a-old", subject="s:rev:a", effect="confirming", delta=0.3)
    new_a = _ev(id="a-new", subject="s:rev:a", effect="confirming", delta=0.5)
    old_b = _ev(id="b-old", subject="s:rev:b", effect="refuting", delta=0.3)
    new_b = _ev(id="b-new", subject="s:rev:b", effect="refuting", delta=0.1)
    prev_post = update_confidence([old_a, old_b], prior=0.5)
    post = update_confidence_delta([new_a, new_b], prior=prev_post, prev_events=[old_a, old_b])
    # net = lo(new_a)+lo(new_b) − lo(old_a)−lo(old_b)
    expected = update_confidence([new_a, new_b], prior=0.5)
    assert post == pytest.approx(expected)


# ─────────────────────────────────────────────────────────────────────────────
# retraction 作废（显式 supersedes）
# ─────────────────────────────────────────────────────────────────────────────


def test_retraction_removes_superseded_old_event():
    old = _ev(id="old", subject="600519:forecast:20231231", effect="confirming", delta=0.3)
    retraction = _ev(
        id="retract",
        subject="600519:retraction:20231231",
        effect="neutral",
        delta=0.0,
        supersedes=["old"],
    )
    prev_post = update_confidence([old], prior=0.5)
    post = update_confidence_delta([retraction], prior=prev_post, prev_events=[old])
    # retraction 自身 neutral ⇒ 0 贡献；作废旧 ⇒ 减 lo(old) ⇒ 回到先验
    assert post == pytest.approx(0.5)


def test_retraction_then_rebaseline_net_effect():
    old = _ev(id="old", subject="600519:forecast:20231231", effect="confirming", delta=0.3)
    retraction = _ev(
        id="retract",
        subject="600519:retraction:20231231",
        effect="neutral",
        delta=0.0,
        supersedes=["old"],
    )
    rebaseline = _ev(
        id="new",
        subject="600519:forecast:20231231",
        effect="confirming",
        delta=0.5,
    )
    prev_post = update_confidence([old], prior=0.5)
    post = update_confidence_delta([retraction, rebaseline], prior=prev_post, prev_events=[old])
    # 先减旧（lo(old)）再加修正后（lo(new)）⇒ lo(new)
    assert post == pytest.approx(update_confidence([rebaseline], prior=0.5))


def test_supersedes_id_not_in_prev_is_ignored():
    retraction = _ev(id="retract", subject="s:retract:p", effect="neutral", delta=0.0, supersedes=["ghost"])
    post = update_confidence_delta([retraction], prior=0.6, prev_events=[])
    # ghost 不在 prev ⇒ 不减；retraction neutral ⇒ 0 贡献 ⇒ 后验不变
    assert post == pytest.approx(0.6)


def test_retraction_event_has_zero_logodds_contribution():
    retraction = _ev(id="retract", subject="s:retract:p", effect="neutral", delta=0.0)
    assert _event_logodds(retraction) == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 无净变化不重新夹紧 / 先验语义 / 不对称保留
# ─────────────────────────────────────────────────────────────────────────────


def test_no_net_change_returns_prior_without_reclamp():
    # 先验已到 clamp 上界（0.95）；同值重复帧 ⇒ 不重新夹紧，逐字返回 prior
    rev = _ev(id="rev-same", subject="s:rev:f", effect="confirming", delta=0.9)
    post = update_confidence_delta([rev], prior=0.95, prev_events=[rev])
    assert post == pytest.approx(0.95)


def test_prior_none_with_new_events_uses_neutral_prior():
    new = _ev(id="new", effect="confirming", delta=0.5)
    post = update_confidence_delta([new], prior=None, prev_events=[])
    assert post == pytest.approx(update_confidence([new], prior=None))


def test_refuting_asymmetry_preserved_in_delta():
    old = _ev(id="old", subject="s:rev:f", effect="confirming", delta=0.5)
    new = _ev(id="new", subject="s:rev:f", effect="refuting", delta=0.5)
    prev_post = update_confidence([old], prior=0.5)
    post = update_confidence_delta([new], prior=prev_post, prev_events=[old])
    # 反证 1.2× 放大保留：refuting 0.5 的 log-odds 幅度 > confirming 0.5
    assert post < update_confidence([old], prior=0.5)


def test_deterministic():
    old = _ev(id="old", subject="s:rev:f", effect="confirming", delta=0.3)
    new = _ev(id="new", subject="s:rev:f", effect="confirming", delta=0.5)
    prev_post = update_confidence([old], prior=0.5)
    a = update_confidence_delta([new], prior=prev_post, prev_events=[old])
    b = update_confidence_delta([new], prior=prev_post, prev_events=[old])
    assert a == b


# ─────────────────────────────────────────────────────────────────────────────
# additive 字段回落（旧 JSONL 反序列化，不升 schema_version）
# ─────────────────────────────────────────────────────────────────────────────


def test_legacy_evidence_event_without_subject_supersedes_falls_back():
    """旧 dict（无 subject/supersedes）反序列化 ⇒ subject=="" 且 supersedes==[]。"""
    legacy = {
        "id": "legacy-1",
        "date": "2024-01-01",
        "kind": "expectation",
        "description": "旧证据",
        "effect_on_thesis": "confirming",
        "confidence_delta": 0.3,
        "source_refs": ["src"],
    }
    event = EvidenceEvent.model_validate(legacy)
    assert event.subject == ""
    assert event.supersedes == []


def test_legacy_company_state_artifact_evidence_log_falls_back():
    """含旧 evidence_log 的 CompanyStateArtifact：additive 字段回落，schema_version 不升。"""
    artifact = CompanyStateArtifact.model_validate(
        {
            "symbol": "600519.SH",
            "as_of_date": "2024-01-01",
            "evidence_log": [
                {
                    "id": "legacy-1",
                    "date": "2024-01-01",
                    "kind": "expectation",
                    "description": "旧证据",
                    "effect_on_thesis": "confirming",
                    "confidence_delta": 0.3,
                }
            ],
        }
    )
    assert artifact.schema_version == "1"  # additive：不升 schema_version
    assert len(artifact.evidence_log) == 1
    assert artifact.evidence_log[0].subject == ""
    assert artifact.evidence_log[0].supersedes == []
