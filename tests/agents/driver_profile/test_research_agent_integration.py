"""驱动画像研究代理真 LLM 冒烟（``@pytest.mark.integration``）。

**只在显式跑集成测试且配置了 LLM_API_KEY 时执行**：

    poetry run pytest tests/agents/driver_profile/ -q -m integration

无 key 时：``-m "not integration"`` 直接 deselected（不失败），显式跑集成时 skip。
断言的是设计稿 §12 的两条硬指标在真模型上的最小可观测形式：
``driver_hypotheses`` 非空（确实做了研究）且每条证据可溯（C2 通过）。
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

from alphabee.config import DomainContextResearchSettings
from alphabee.domain_context import research
from alphabee.domain_context.context_router import RouterInput
from alphabee.domain_context.contracts import DriverProfile

load_dotenv()

SYMBOL = "002916.SZ"  # 深南电路：本地 reports/ 有已解析年报，且驱动变量非行业模板


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("LLM_API_KEY"), reason="需要 LLM_API_KEY 才能跑真 LLM 冒烟"),
]


async def test_driver_research_smoke_yields_traceable_hypotheses(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = DomainContextResearchSettings(
        enabled=True,
        max_tool_calls=12,
        wall_timeout_s=600.0,
        cache_dir=str(tmp_path / "cache"),
    )
    router_input = RouterInput(
        symbol=SYMBOL,
        track_label="覆铜板",
        industry="电子",
        sw_code="801080.SI",
        business_model="component",
    )
    research_input = research.build_research_input(SYMBOL, router_input)

    result = await research.run_driver_research(
        SYMBOL,
        research_input,
        settings=settings,
        cache_dir=settings.cache_dir,
        track_label=router_input.track_label,
        trigger="smoke",
        use_cache=False,
    )
    profile = research.converge(
        DriverProfile(symbol=SYMBOL, playbook="generic_fundamental", anchor_strength="weak"),
        result,
        research_input=research_input,
        settings=settings,
        trigger="smoke",
    )
    payload = research.finalize_research_artifact(
        profile, tier=int(result.meta.get("tier", 0)), meta={"trigger": "smoke"}
    )

    assert payload["research_meta"]["fallback_tier"] == 0, payload["research_meta"].get("issues")
    assert profile.driver_hypotheses, "真 LLM 冒烟：应产出至少一条驱动假设（证据可溯者）"
    for hypothesis in profile.driver_hypotheses:
        assert hypothesis.evidence, hypothesis.variable
        assert any(evidence.ref.strip() for evidence in hypothesis.evidence), hypothesis.variable
    assert profile.primary_drivers
