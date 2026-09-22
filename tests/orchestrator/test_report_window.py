"""P1（W3）财报原文窗口测试：``services/report_window.py``（§15.1-B/G）。

覆盖设计文档 §15.1-G 的测试矩阵：
真实样本（``reports/工业富联(601138)``）、``as_of`` 早于所有报告、``max_chars`` 截断、
无该标的目录、manifest 半截 JSON、``full_text_path`` 指向缺失、白名单未命中、
内部异常 fail-open、``enabled=False`` 短路、开关读取 fail-open（默认 True / 异常 False）。

纪律：本文件**不** mock 文件系统参与真实样本用例（直接读仓库内既有 ``reports/``），
其余用 ``tmp_path`` 造最小 fixtures —— 保证"窗口真的能从本地解析产物里取到原文"这件事
被真数据钉住，而不是被 mock 钉住。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alphabee.financial_report.report_parser import reports_root
from alphabee.orchestrator.services import report_window as rw
from alphabee.orchestrator.services.report_window import (
    SECTION_GROUPS,
    ReportWindow,
    report_window_enabled,
    select_report_window,
)

REPO_ROOT = Path(rw.__file__).resolve().parents[3]
REAL_SAMPLE_CODE = "601138"  # 工业富联：reports/工业富联(601138)/财报/<12 期报告>/


def _newest_by_created_at(code: str) -> str | None:
    """命令枚举真实磁盘上的 manifest，返回 ``created_at`` 最大者的 ``report_period``。

    用途：**序口径对照**（证明"报告期最新"与"入库最新"在真实数据上不是同一份）。
    """
    root = REPO_ROOT / "reports"
    company_dirs = sorted(p for p in root.glob(f"*{code}*") if p.is_dir())
    if not company_dirs:
        return None
    manifests: list[dict[str, object]] = []
    for level_one in sorted(p for p in company_dirs[0].glob("*") if p.is_dir()):
        for report_dir in [*sorted(p for p in level_one.glob("*") if p.is_dir()), level_one]:
            manifest = report_dir / ".report_manifest.json"
            if manifest.is_file():
                manifests.append(json.loads(manifest.read_text(encoding="utf-8")))
    if not manifests:
        return None
    newest = max(manifests, key=lambda item: str(item.get("created_at") or ""))
    return str(newest.get("report_period") or "") or None


def _write_report(
    root: Path,
    *,
    code: str = "600584",
    company: str = "长电科技",
    leaf: str = "2025年年度报告",
    text: str = "# 全文\n\n## 第三节 管理层讨论与分析\n营业收入同比增长 12.5%。\n",
    created_at: str = "2026-01-01T00:00:00+00:00",
    page_count: int | None = 100,
    full_text_path: str | None = "auto",
    manifest_text: str | None = None,
) -> Path:
    """造一份最小"已解析财报"目录（manifest + 全文副本），返回报告目录。"""
    report_dir = root / f"{company}({code})" / "财报" / leaf
    report_dir.mkdir(parents=True, exist_ok=True)
    full_dir = root / "_full" / f"{company}({code})" / "财报" / leaf
    full_dir.mkdir(parents=True, exist_ok=True)
    full_file = full_dir / f"{leaf}.md"
    full_file.write_text(text, encoding="utf-8")

    payload = {
        "report_name": f"{company}：{leaf}",
        "company_name": company,
        "company_code": code,
        "category": "财报",
        "report_period": leaf,
        "section_count": 1,
        "file_count": 1,
        "page_count": page_count,
        "created_at": created_at,
        "full_text_path": str(full_file) if full_text_path == "auto" else full_text_path,
    }
    manifest = report_dir / ".report_manifest.json"
    manifest.write_text(
        manifest_text if manifest_text is not None else json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return report_dir


# ── 真实样本（不 mock：证明窗口真的能从既有本地解析产物里取到原文）────────────


@pytest.mark.skipif(not (REPO_ROOT / "reports").is_dir(), reason="仓库本地报告目录不存在（数据未随仓库分发）")
def test_real_sample_yields_sections_without_reason():
    window = select_report_window(REAL_SAMPLE_CODE, as_of="2026-12-31")

    assert window.reason == ""
    assert window.sections, "真实样本必须取到章节（否则 §11 验收 3 仍为 ⬜）"
    assert window.chars > 0
    assert window.chars <= 12_000
    assert window.report_name
    assert window.page_count is not None
    assert Path(window.source_path).is_file()

    groups = {section.group for section in window.sections}
    assert groups, "至少命中一个叙事章节组"
    assert groups <= {"mda", "business", "risk"}
    # 排除项：财报附注/审计报告等噪声章节不得进窗口（标题不含白名单关键词即不会命中）。
    for section in window.sections:
        assert section.chars == len(section.text)
        assert section.text.strip()
        assert "财务报表附注" not in section.title


@pytest.mark.skipif(not (REPO_ROOT / "reports").is_dir(), reason="仓库本地报告目录不存在（数据未随仓库分发）")
def test_real_sample_601138_selects_newest_report_period_not_newest_created_at():
    """★ 序口径钉住（captain 定案）：601138 必须选中**报告期最新**一期，而非 ``created_at`` 最新者。

    实测数据（``reports/工业富联(601138)`` 共 12 份 manifest）：
    ``created_at`` 最新 = 2023 年年度报告（``2026-08-22T07:05``）；报告期最新 = **2026 年半年度报告**
    （``2026-08-21T23:55``，恰是 12 份里 ``created_at`` **最旧**的一份）⇒ 本用例同时钉住
    「选中的是报告期最新」与「它与 ``created_at`` 最新**不是**同一份」两件事。
    """
    window = select_report_window("601138", as_of="2026-12-31")

    assert window.reason == ""
    assert "2026年半年度报告" in window.report_period, f"实际选中 {window.report_period}"
    assert "2023年年度报告" not in window.report_period
    assert window.sections, "报告期最新一期必须能取到叙事章节"
    # 与 created_at 序的对照：本标的两序不一致（这是本用例存在的理由）
    created_at_newest = _newest_by_created_at("601138")
    assert created_at_newest is not None
    assert created_at_newest != window.report_period, "两序在本标的应不一致，否则本用例失去鉴别力"


@pytest.mark.skipif(not (REPO_ROOT / "reports").is_dir(), reason="仓库本地报告目录不存在（数据未随仓库分发）")
def test_real_sample_002130_consistency_control_both_orders_agree():
    """一致性对照：``reports/沃尔核材(002130)`` 两序一致（该家报告期最新 = ``created_at`` 最新）。"""
    window = select_report_window("002130", as_of="2026-12-31")

    assert window.reason == ""
    assert "2026年半年度报告" in window.report_period
    assert _newest_by_created_at("002130") == window.report_period


@pytest.mark.skipif(not (REPO_ROOT / "reports").is_dir(), reason="仓库本地报告目录不存在（数据未随仓库分发）")
def test_real_heading_risk_is_matched_by_substring_rule():
    """★ 子串匹配钉住：真实年报标题 ``十、 重大风险提示`` 必须被规则判为 ``risk``。

    F8 后**不再**断言"该 risk 子树出现在默认预算的窗口里"：子树边界下 ``mda`` 组先取，
    单棵子树即可吃满 ``max_chars=12000`` ⇒ 该标的默认窗口只含 mda 子树。本用例改钉
    **规则本身**（标题级子串匹配，F8 明确要求保持不变的语义），并用真实磁盘数据证明
    该标题确实存在于被测报告全文中。
    """
    window = select_report_window("601138", as_of="2026-12-31")
    assert window.reason == ""

    risk_title = "十、 重大风险提示"
    assert rw._match_group(risk_title) == "risk", "真实标题“风险提示”必须子串命中 risk 组"
    report_text = Path(window.source_path).read_text(encoding="utf-8")
    assert f"## {risk_title}" in report_text


@pytest.mark.skipif(not (REPO_ROOT / "reports").is_dir(), reason="仓库本地报告目录不存在（数据未随仓库分发）")
def test_real_sample_601138_thickness_regression():
    """★ F8 厚度回归钉：子树边界 + 等额分配下 601138 必显著厚于旧叶边界的 253 字符，**且三组都入窗**。

    判别力（自证）：① 把 ``_subtree_span`` 改回叶边界 ⇒ 253 < 3000 且只 1 组 ⇒ **必红**；
    ② 把等额分配改回"先到先得吃满" ⇒ ``risk``/``business`` 被 mda 挤掉 ⇒ **必红**。
    """
    window = select_report_window("601138", as_of="2026-12-31")

    assert window.reason == ""
    assert window.chars >= 3000, f"应远厚于旧边界 253 字符，实际 {window.chars}"
    assert window.chars <= 12_000
    assert window.truncated is True
    groups = {section.group for section in window.sections}
    assert {"mda", "business", "risk"} <= groups, f"等额分配下三组都应有内容，实际 {groups}"
    assert len(window.sections) >= 3
    assert window.chars == sum(section.chars for section in window.sections)


def test_budget_split_equally_across_hit_groups(tmp_path):
    """★ **裁定 C**：预算按"有内容的命中组"**等额切分**，而不是先到先得吃满。

    三组齐备 + ``max_chars=12000`` ⇒ 每组份额 4000；``mda`` 内容 20,000 被截到 **4000**，
    ``business`` 3000 与 ``risk`` 100 **各自完整入窗**。

    判别力（自证）：改回"顺序吃满"后 ``mda`` 独占 12000、``business``/``risk`` 消失 ⇒ 红。
    """
    text = (
        "## 第三节 管理层讨论与分析\n" + "甲" * 20_000 + "\n"
        "## 第四节 主营业务分析\n" + "乙" * 3000 + "\n"
        "## 十、 重大风险提示\n" + "丙" * 100 + "\n"
    )
    _write_report(tmp_path, text=text)

    window = select_report_window("600584", reports_root_path=tmp_path)

    expected_business = len("## 第四节 主营业务分析\n" + "乙" * 3000)
    expected_risk = len("## 十、 重大风险提示\n" + "丙" * 100)
    by_group: dict[str, int] = {}
    for section in window.sections:
        by_group[section.group] = by_group.get(section.group, 0) + section.chars
    assert set(by_group) == {"mda", "business", "risk"}, f"三组都应入窗，实际 {by_group}"
    assert by_group["mda"] == 4000, "mda 被截到其等额份额 4000"
    assert by_group["business"] == expected_business, "business 内容小于份额 ⇒ 完整入窗"
    assert by_group["risk"] == expected_risk, "risk 内容很小也必须入窗（否则即退化为'只有 mda'）"
    assert window.chars == 4000 + expected_business + expected_risk
    assert window.truncated is True


def test_unused_group_share_carries_forward_to_next_group(tmp_path):
    """★ **裁定 C 的余额顺延**：前面组没用完的份额必须顺延给后面的组。

    三组份额各 4000：``mda`` 用满 4000（余额 0）、``business`` 只 50（余额 3950 顺延）、
    ``risk`` 内容 5000 < 4000+3950 ⇒ **完整入窗**（若无顺延，risk 只会被截到 4000）。

    判别力（自证）：去掉余额顺延（每组硬上限 4000）后 ``risk == 5000`` 必红。
    """
    text = (
        "## 第三节 管理层讨论与分析\n" + "甲" * 5000 + "\n"
        "## 第四节 主营业务分析\n" + "乙" * 50 + "\n"
        "## 十、 重大风险提示\n" + "丙" * 5000 + "\n"
    )
    _write_report(tmp_path, text=text)

    window = select_report_window("600584", reports_root_path=tmp_path)

    expected_business = len("## 第四节 主营业务分析\n" + "乙" * 50)
    expected_risk = len("## 十、 重大风险提示\n" + "丙" * 5000)
    by_group: dict[str, int] = {}
    for section in window.sections:
        by_group[section.group] = by_group.get(section.group, 0) + section.chars
    assert by_group["mda"] == 4000, "mda 用满自身份额"
    assert by_group["business"] == expected_business
    assert by_group["risk"] == expected_risk, "risk 靠 business 顺延的余额 ⇒ 完整入窗（>4000）"
    assert by_group["risk"] > 4000, "必须超过自份额才能证明余额确实顺延"
    assert window.chars == 4000 + expected_business + expected_risk
    assert window.chars <= 12_000


# ── 两条不变量（F8 裁定 C 配套，reviewer 建议 / captain 采纳）───────────────


@pytest.mark.parametrize("max_chars", [10, 100, 999, 4000, 12_000, 50_000])
def test_total_chars_never_exceeds_max_chars_invariant(tmp_path, max_chars):
    """★ 不变量 1：**总字符 ≤ `max_chars` 恒成立**（等额分配只改切分方式，不改硬上限性质）。

    判别力（**实测**，两个变异分别验证，见交付说明）：

    * 忠实复现旧"顺序吃满"（`room = max_chars - used`，单一全局剩余预算）⇒ 本用例**仍绿**
      （该变异守得住上限；它的病是"首组独吞"，由 risk 不变量负责抓）；
    * 把每组份额膨胀成 `share = max_chars`（每组合格配额各自等于总预算）⇒ 本用例**变红**
      （那才会真的突破上限）。
    """
    text = (
        "## 第三节 管理层讨论与分析\n" + "甲" * 30_000 + "\n"
        "## 第四节 主营业务分析\n" + "乙" * 8000 + "\n"
        "## 十、 重大风险提示\n" + "丙" * 5000 + "\n"
    )
    _write_report(tmp_path, text=text)

    window = select_report_window("600584", reports_root_path=tmp_path, max_chars=max_chars)

    assert window.reason == ""
    assert window.chars <= max_chars, f"{window.chars} > max_chars={max_chars}"
    assert window.chars == sum(section.chars for section in window.sections)
    for section in window.sections:
        assert section.chars == len(section.text)


@pytest.mark.skipif(not (REPO_ROOT / "reports").is_dir(), reason="仓库本地报告目录不存在（数据未随仓库分发）")
def test_total_chars_invariant_holds_for_whole_local_corpus():
    """★ 不变量 1（真实全库）：22 家本地标的在**默认预算 12000** 下总字符恒 ≤ 12000。"""
    codes = sorted(path.name.split("(")[-1].rstrip(")") for path in (REPO_ROOT / "reports").iterdir() if path.is_dir())

    assert codes, "本地报告目录为空"
    for code in codes:
        window = select_report_window(code, as_of="2026-12-31")
        assert window.chars <= 12_000, f"{code}: {window.chars} > 12000"
        assert window.chars == sum(section.chars for section in window.sections), code


@pytest.mark.skipif(not (REPO_ROOT / "reports").is_dir(), reason="仓库本地报告目录不存在（数据未随仓库分发）")
def test_real_sample_risk_group_nonempty_under_equal_split():
    """★ 不变量 2：等额分配下 **`risk` 组必须能拿到内容**（真实样本 601138）。

    ``## 十、 重大风险提示`` 是**顶层**章节（不被 mda 子树覆盖）⇒ 应作为 ``risk`` 组入窗。

    判别力（**实测**）：忠实复现旧"顺序吃满"（`room = max_chars - used`）后 mda 独吞 12000、
    ``risk`` 被挤掉 ⇒ 本用例 **变红**（对照实验输出见交付说明）。
    """
    window = select_report_window("601138", as_of="2026-12-31")

    assert window.reason == ""
    risk_chars = sum(section.chars for section in window.sections if section.group == "risk")
    assert risk_chars > 0, f"risk 组不得整体缺失，实际 {[(s.group, s.chars) for s in window.sections]}"
    assert any(s.title.strip() == "十、 重大风险提示" for s in window.sections if s.group == "risk")
    assert window.chars <= 12_000


def test_real_sample_dot_suffixed_symbol_matches_same_code():
    """``601138.SH`` 与 ``601138`` 命中同一目录（按 6 位代码匹配）。"""
    with_suffix = select_report_window("601138.SH", as_of="2026-12-31")
    without = select_report_window("601138", as_of="2026-12-31")

    assert with_suffix.reason == without.reason == ""
    assert with_suffix.report_name == without.report_name
    assert [s.title for s in with_suffix.sections] == [s.title for s in without.sections]


# ── 节点级真实样本（P1 接线端到端：真读本地财报 → window_texts 非空）─────────


@pytest.mark.skipif(not (REPO_ROOT / "reports").is_dir(), reason="仓库本地报告目录不存在（数据未随仓库分发）")
def test_midterm_node_window_texts_nonempty_from_real_reports(monkeypatch):
    """★ §11 验收 3 的 P1 部分：真实标的 + 真实本地报告 → ``window_texts`` 非空。

    只替换 LLM/市场通道（``collect_evidence_split`` ↔ 记录入参、``get_decision`` ↔ 假决策），
    **窗口选择走真实代码路径 + 真实磁盘数据**；其余（insight/thesis 映射、issue 记账）全真跑。
    """
    import asyncio

    from alphabee.core import Artifact, ArtifactType, Run, RunStatus
    from alphabee.midterm.models import (
        CognitiveState,
        CompanyStateArtifact,
        ExpectationGap,
        StateBelief,
        VariableScores,
    )
    from alphabee.orchestrator.contracts import InsightArtifact
    from alphabee.orchestrator.nodes import midterm as node

    captured: dict[str, object] = {}

    def _fake_collect(symbol, thesis="", window_texts=None, model=None):
        captured["window_texts"] = window_texts
        return [], []

    def _fake_decision(symbol, evidence=None, **kwargs):
        return CompanyStateArtifact(
            symbol=symbol,
            thesis=str(kwargs.get("thesis") or ""),
            state=StateBelief(
                distribution={"S1": 0.5, "S2": 0.5},
                argmax_state=CognitiveState.S2_CONFIRM.value,
                entropy=0.0,
            ),
            expectation_gap=ExpectationGap(),
            variable_scores=VariableScores(),
            evidence_log=[],
        )

    monkeypatch.setattr(node, "collect_evidence_split", _fake_collect)
    monkeypatch.setattr(node, "get_decision", _fake_decision)

    run = Run(
        id="run-p1",
        goal="分析工业富联",
        status=RunStatus.RUNNING,
        context={"symbol": f"{REAL_SAMPLE_CODE}.SH", "as_of_date": "2026-12-31"},
    )
    state = {
        "run": run,
        "steps": [],
        "artifacts": [
            Artifact(
                id="a-insight",
                type=ArtifactType.INSIGHT_ANALYSIS,
                producer_step="synthesize_insights",
                value=InsightArtifact(core_view="核心观点：看多", confidence="medium").model_dump(mode="json"),
            )
        ],
        "issues": [],
        "decisions": [],
    }
    result = asyncio.run(node.resolve_midterm_decision(state, {}))

    window_texts = captured["window_texts"]
    assert window_texts, "真实本地报告在场时 window_texts 必须非空（P1 的核心收益）"
    joined = "\n".join(window_texts)
    assert "管理层讨论与分析" in joined
    assert "主营业务" in joined
    assert len(joined) <= 12_000
    assert [issue for issue in result["issues"] if issue.category == "report_window_unavailable"] == []
    # 只替换了 LLM 通道：决策 artifact 照常产出，主链不因窗口而中断。
    assert any(artifact.type == ArtifactType.MIDTERM_DECISION for artifact in result["artifacts"])


# ── as_of / 时间轴（**报告期为主序**：captain 定案，优先于 §15.1-B 字面措辞）─────────


def test_as_of_before_all_reports_returns_empty_window(tmp_path):
    _write_report(tmp_path, leaf="2026年年度报告", created_at="2026-01-01T00:00:00+00:00")

    window = select_report_window("600584", as_of="2025-12-31", reports_root_path=tmp_path)

    assert window.sections == []
    assert window.reason == "no_report_before_as_of"


def test_latest_report_is_selected_by_report_period_not_created_at(tmp_path):
    """★ 主序 = 报告期：``created_at`` 与报告期**相反**时，必须按报告期选。"""
    _write_report(
        tmp_path,
        leaf="2025年年度报告",
        created_at="2025-01-01T00:00:00+00:00",  # 入库更早
        text="## 第三节 管理层讨论与分析\n新一期正文\n",
    )
    _write_report(
        tmp_path,
        leaf="2024年年度报告",
        created_at="2026-06-01T00:00:00+00:00",  # 入库更晚（模拟同批 ingest 的目录序）
        text="## 第三节 管理层讨论与分析\n陈旧正文\n",
    )

    window = select_report_window("600584", as_of="2026-12-31", reports_root_path=tmp_path)

    assert window.reason == ""
    assert "2025年年度报告" in window.report_period
    assert "新一期正文" in window.sections[0].text


def test_within_year_order_is_q1_half_q3_annual(tmp_path):
    """年内序：第一季度 < 半年度 < 第三季度 < 年度报告（**半年度不得被误判为年度**）。

    ``created_at`` 统一取 ``2025-01-01``（早于下面每个 ``as_of``）以隔离 point-in-time
    入库约束（F5），使本用例只检验**报告期主序**。
    """
    for leaf in ("2025年第一季度报告", "2025年半年度报告", "2025年第三季度报告", "2025年年度报告"):
        _write_report(
            tmp_path,
            leaf=leaf,
            created_at="2025-01-01T00:00:00+00:00",
            text=f"## 第三节 管理层讨论与分析\n{leaf}正文\n",
        )

    window = select_report_window("600584", as_of="2026-12-31", reports_root_path=tmp_path)

    assert "2025年年度报告" in window.report_period
    assert window.report_period.strip() != "2025年半年度报告"

    # 逐个收敛：as_of 依次卡在期内，选中的报告期应单调前进
    picks = []
    for as_of in ("2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"):
        picks.append(select_report_window("600584", as_of=as_of, reports_root_path=tmp_path).report_period)
    assert picks == ["2025年第一季度报告", "2025年半年度报告", "2025年第三季度报告", "2025年年度报告"], picks


def test_as_of_excludes_not_yet_ingested_manifest(tmp_path):
    """F5：``as_of`` 时点**尚未入库**（``created_at`` > as_of）的报告不得入选。

    报告期期末日（2025-12-31）本身不晚于 ``as_of``，但该产物在 ``as_of`` 时点还不存在
    （``created_at=2026-06-01``）⇒ 必须排除，否则历史回放会把未来才入库的财报接进窗口
    （point-in-time 前视）。此时无任何可入选报告 ⇒ ``reason="no_report_before_as_of"``。
    """
    _write_report(
        tmp_path,
        leaf="2025年年度报告",
        created_at="2026-06-01T00:00:00+00:00",  # 入库晚于 as_of
        text="## 第三节 管理层讨论与分析\n2025 年报正文\n",
    )

    window = select_report_window("600584", as_of="2025-12-31", reports_root_path=tmp_path)

    assert window.reason == "no_report_before_as_of"
    assert window.sections == []

    # 入库之后（as_of 晚于 created_at）同一份报告即可入选 ⇒ 约束只针对"时点尚未入库"
    later = select_report_window("600584", as_of="2026-12-31", reports_root_path=tmp_path)
    assert later.reason == ""
    assert "2025年年度报告" in later.report_period


def test_created_at_is_only_tiebreak_within_same_period(tmp_path):
    """同一报告期多份产物：取 ``created_at`` 最后入库者（唯一允许 ``created_at`` 生效的场合）。"""
    _write_report(
        tmp_path,
        leaf="2025年年度报告",
        created_at="2025-02-01T00:00:00+00:00",
        text="## 第三节 管理层讨论与分析\n初版\n",
    )
    _write_report(
        tmp_path,
        leaf="2025年年度报告（修订后）",
        created_at="2026-03-01T00:00:00+00:00",
        text="## 第三节 管理层讨论与分析\n修订版\n",
    )

    window = select_report_window("600584", as_of="2026-12-31", reports_root_path=tmp_path)

    assert "修订版" in window.sections[0].text


def test_unparsable_report_period_falls_back_to_created_at(tmp_path):
    """报告期不可解析（无年份/无期间）→ 排序垫底、``as_of`` 退回 ``created_at`` 兜底。"""
    _write_report(tmp_path, leaf="未标注期间的报告", created_at="2026-06-01T00:00:00+00:00")

    assert select_report_window("600584", as_of="2026-12-31", reports_root_path=tmp_path).reason == ""
    assert (
        select_report_window("600584", as_of="2025-01-01", reports_root_path=tmp_path).reason
        == "no_report_before_as_of"
    )


def test_no_as_of_takes_newest_report(tmp_path):
    _write_report(
        tmp_path, leaf="2024年年度报告", created_at="2026-06-01T00:00:00+00:00", text="## 管理层讨论与分析\n旧\n"
    )
    _write_report(
        tmp_path, leaf="2026年半年度报告", created_at="2025-01-01T00:00:00+00:00", text="## 管理层讨论与分析\n新\n"
    )

    window = select_report_window("600584", reports_root_path=tmp_path)

    assert window.reason == ""
    assert "2026年半年度报告" in window.report_period
    assert "新" in window.sections[0].text


# ── 预算（max_chars / max_sections / 组间有序 / 文本边界 / 子串匹配）─────────


def test_max_chars_budget_truncates_and_counts(tmp_path):
    _write_report(tmp_path, text="## 第三节 管理层讨论与分析\n" + "甲" * 500 + "\n")

    window = select_report_window("600584", max_chars=200, reports_root_path=tmp_path)

    assert window.reason == ""
    assert window.truncated is True
    assert len(window.sections) >= 1
    assert window.chars <= 200
    assert window.chars == sum(section.chars for section in window.sections)


def test_max_chars_and_max_sections_are_hard_upper_bounds(tmp_path):
    """★ 两条预算都是**硬上限**：只截断到剩余预算，绝不"整段保留后溢出"。"""
    text = "".join(f"## 风险因素 {i}\n" + "乙" * 300 + "\n" for i in range(8))
    _write_report(tmp_path, text=text)

    for max_chars, max_sections in ((100, 8), (500, 2), (1_000, 3), (12_000, 4)):
        window = select_report_window(
            "600584", reports_root_path=tmp_path, max_chars=max_chars, max_sections=max_sections
        )
        assert window.chars <= max_chars, (max_chars, max_sections, window.chars)
        assert len(window.sections) <= max_sections, (max_chars, max_sections, len(window.sections))
        assert window.chars == sum(section.chars for section in window.sections)
        assert window.truncated is True


def test_section_text_boundary_is_subtree(tmp_path):
    """★ F8 文本边界 = **子树**：父章节 text 必须包含其全部子标题段落，且去重只计一次。

    判别力（自证）：把 `_subtree_span` 改回"叶边界"后，`子章节正文 in parent.text` 与
    `len(sections) == 1` **必然变红**（旧边界下会得到 2 段且父章节不含子正文）。
    """
    text = "## 第三节 管理层讨论与分析\n父章节自身正文\n### 一、 经营情况讨论与分析\n子章节正文\n"
    _write_report(tmp_path, text=text)

    window = select_report_window("600584", reports_root_path=tmp_path)

    assert window.reason == ""
    assert len(window.sections) == 1, "父子树已覆盖命中子章节 ⇒ 子树只计一次（F8 去重）"
    parent = window.sections[0]
    assert parent.title == "第三节 管理层讨论与分析"
    assert "父章节自身正文" in parent.text
    assert "子章节正文" in parent.text, "子树边界下父章节必须括住子标题段落"
    assert "### 一、 经营情况讨论与分析" in parent.text, "子树保留原始标题层级写法"
    assert parent.chars == len(parent.text)
    assert window.chars == parent.chars


def test_subtree_boundary_stops_at_same_or_higher_level_heading(tmp_path):
    """子树**不**越过同级/更高级标题：``## A``（命中）之后的另一个 ``## B`` 不算它的子树。"""
    text = "## 第一节 主营业务分析\n一层正文\n### 细节一\n更深正文\n## 第二节 释义\n另一同级章节正文\n"
    _write_report(tmp_path, text=text)

    window = select_report_window("600584", reports_root_path=tmp_path)

    assert window.reason == ""
    assert len(window.sections) == 1
    assert "更深正文" in window.sections[0].text
    assert "另一同级章节正文" not in window.sections[0].text, "子树边界=下一个同级或更高级标题"


def test_top_down_claim_attribution_beats_earlier_group(tmp_path):
    """★ **顶层声明制（F8 裁定 A）**：祖先认领的子树归属**祖先的组**，内层不再认领。

    构造：``## 十、 重大风险提示``（``risk``，组序第 3）下挂 ``### 一、 主营业务风险``
    （``business``，组序第 2）。按组序先取子标题的旧实现会产出 **2 段且文本重复**；
    裁定 A 下必须是 **1 段、group=risk、文本只出现一次**。

    判别力（自证）：把 ``_claim_roots`` 的 ``blocked_until`` 守卫去掉（内层继续认领）后，
    ``len(sections) == 1`` 与 ``group == "risk"`` **必然变红**。
    """
    text = "## 十、 重大风险提示\n风险总述\n### 一、 主营业务风险\n业务侧风险细节\n## 第十一节 其他\n无关正文\n"
    _write_report(tmp_path, text=text)

    window = select_report_window("600584", reports_root_path=tmp_path)

    assert window.reason == ""
    assert len(window.sections) == 1, f"祖先认领后内层不得再认领，实际 {[s.title for s in window.sections]}"
    only = window.sections[0]
    assert only.group == "risk", "子树文本归属认领者（父章节）所在的组"
    assert only.title == "十、 重大风险提示"
    assert "业务侧风险细节" in only.text
    assert only.text.count("业务侧风险细节") == 1
    assert window.chars == only.chars


def test_max_sections_counts_claimed_sections_not_headings(tmp_path):
    """★ ``max_sections`` 计**被认领的章节数**（F8 裁定 B）：一棵巨大子树只算 1 段。

    两个互不嵌套的 ``business`` 根节点（组内按文档顺序）⇒ ``max_sections=1`` 只取第一个，
    而它内部有 3 个层级标题却只算 1 段。
    """
    text = (
        "## 第一节 主营业务分析\n" + "甲" * 3000 + "\n"
        "### 子标题一\n" + "乙" * 3000 + "\n"
        "#### 子标题二\n" + "丙" * 3000 + "\n"
        "## 第二节 主营业务分析之二\n" + "丁" * 3000 + "\n"
    )
    _write_report(tmp_path, text=text)

    window = select_report_window("600584", reports_root_path=tmp_path, max_sections=1, max_chars=100_000)

    assert window.reason == ""
    assert len(window.sections) == 1, f"两棵子树各算 1 段；实际 {[s.title for s in window.sections]}"
    assert window.sections[0].title == "第一节 主营业务分析", "组内按文档顺序取首个认领根"
    assert "子标题二" in window.sections[0].text, "整棵子树（含更深标题）只算 1 段"
    assert window.truncated is True, "还有未认领的候选 ⇒ truncated"
    assert window.chars == len(window.sections[0].text)


def test_substring_matching_pins_real_heading_ten_major_risk(tmp_path):
    """★ 子串匹配钉住：真实年报标题 ``十、 重大风险提示`` 命中 ``risk`` 组（预期行为）。"""
    text = "## 十、 重大风险提示\n公司面临的主要风险如下。\n"
    _write_report(tmp_path, text=text)

    window = select_report_window("600584", reports_root_path=tmp_path)

    assert window.reason == ""
    assert len(window.sections) == 1
    assert window.sections[0].title == "十、 重大风险提示"
    assert window.sections[0].group == "risk", "关键词“风险提示”是子串匹配，必须命中"
    # 反例：不含白名单关键词的标题（真实年报里的噪声章节）不得入选
    _write_report(
        tmp_path,
        code="600585",
        company="海螺水泥",
        leaf="2025年年度报告",
        text="## 财务报表附注\n附注正文\n## 审计报告\n审计正文\n",
    )
    assert select_report_window("600585", reports_root_path=tmp_path).reason == "no_matching_section"


def test_group_keywords_are_substring_matched():
    """子串匹配是**文档化约定**（非全等）：含关键词的更长标题也算命中。"""
    assert rw._match_group("第十节 财务报告与风险提示汇总") == "risk"
    assert rw._match_group("第四节 主营业务情况说明") == "business"
    assert rw._match_group("第三节 管理层讨论与分析") == "mda"
    assert rw._match_group("第三节 财务报告与审计意见") == ""


def test_max_sections_budget_stops_selection(tmp_path):
    text = "".join(f"## 风险因素 {i}\n" + "乙" * 20 + "\n" for i in range(5))
    _write_report(tmp_path, text=text)

    window = select_report_window("600584", max_sections=2, reports_root_path=tmp_path)

    assert window.reason == ""
    assert len(window.sections) == 2
    assert window.truncated is True
    assert [section.group for section in window.sections] == ["risk", "risk"]


def test_group_order_is_mda_business_risk(tmp_path):
    """组间有序：原文里 risk 在前，但选中顺序必须是 mda → business → risk。"""
    text = "## 重大风险提示\n风险正文\n## 第四节 主营业务分析\n业务正文\n## 第三节 管理层讨论与分析\n讨论正文\n"
    _write_report(tmp_path, text=text)

    window = select_report_window("600584", reports_root_path=tmp_path)

    assert [section.group for section in window.sections] == ["mda", "business", "risk"]
    assert [group for group, _ in SECTION_GROUPS] == ["mda", "business", "risk"]


def test_empty_body_parent_keeps_its_subtree(tmp_path):
    """★ F8：自身正文为空的父章节**仍入窗**（其子树正文即窗口内容）——这正是 601138 的情形。

    判别力（自证）：旧边界（叶节点 + 丢弃空正文父章节）下本用例会选中子章节标题
    （``一、 经营情况讨论与分析``）且 chars 很小 ⇒ 两个断言都变红。
    """
    text = "## 第三节 管理层讨论与分析\n### 一、 经营情况讨论与分析\n正文实体\n"
    _write_report(tmp_path, text=text)

    window = select_report_window("600584", reports_root_path=tmp_path)

    assert window.reason == ""
    assert len(window.sections) == 1
    assert window.sections[0].title == "第三节 管理层讨论与分析", "空正文父章节按子树入窗"
    assert "正文实体" in window.sections[0].text


def test_truly_empty_section_is_skipped(tmp_path):
    """自身无正文**且无子树**的纯空标题仍不进窗口（避免产生空 section）。"""
    text = "## 第三节 管理层讨论与分析\n## 十、 重大风险提示\n"
    _write_report(tmp_path, text=text)

    window = select_report_window("600584", reports_root_path=tmp_path)

    assert window.sections == []
    assert window.reason == "no_matching_section"


# ── 降级路径（§15.1-E 逐条）────────────────────────────────────────────────


def test_no_local_report_for_unknown_code(tmp_path):
    window = select_report_window("999999.SZ", reports_root_path=tmp_path)

    assert window.sections == []
    assert window.reason == "no_local_report"


def test_symbol_without_six_digit_code_is_no_local_report(tmp_path):
    _write_report(tmp_path)

    assert select_report_window("未知标的", reports_root_path=tmp_path).reason == "no_local_report"


def test_unparsable_manifest_is_reported(tmp_path):
    _write_report(tmp_path, manifest_text="{ half json")

    window = select_report_window("600584", reports_root_path=tmp_path)

    assert window.sections == []
    assert window.reason == "manifest_unreadable"


def test_missing_full_text_path_is_reported(tmp_path):
    report_dir = _write_report(tmp_path, full_text_path="/nonexistent/nowhere.md")

    window = select_report_window("600584", reports_root_path=tmp_path)

    assert window.sections == []
    assert window.reason == "full_text_missing"
    assert report_dir.is_dir()


def test_full_text_falls_back_to_report_dir_markdown(tmp_path):
    """``full_text_path`` 缺失时回退报告目录下的 ``*.md``（旧平铺布局）。"""
    report_dir = _write_report(tmp_path, full_text_path="")
    (report_dir / "平铺全文.md").write_text("## 经营情况讨论与分析\n回退正文\n", encoding="utf-8")

    window = select_report_window("600584", reports_root_path=tmp_path)

    assert window.reason == ""
    assert window.source_path.endswith("平铺全文.md")
    assert "回退正文" in window.sections[0].text


def test_whitelist_miss_is_reported(tmp_path):
    _write_report(tmp_path, text="## 财务报表附注\n附注正文\n## 审计报告\n审计正文\n")

    window = select_report_window("600584", reports_root_path=tmp_path)

    assert window.sections == []
    assert window.reason == "no_matching_section"


def test_internal_error_is_fail_open(tmp_path, monkeypatch):
    """内部异常 → ``internal_error: …`` + 空窗口，**不抛出**（§15.0 C-3）。"""
    _write_report(tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(rw, "_select_sections", _boom)
    window = select_report_window("600584", reports_root_path=tmp_path)

    assert window.sections == []
    assert window.reason.startswith("internal_error: boom")


def test_unreadable_root_does_not_raise(tmp_path):
    """根目录根本不存在 → 空窗口 + reason，不抛异常。"""
    window = select_report_window("600584", reports_root_path=tmp_path / "nope")

    assert window.sections == []
    assert window.reason == "no_local_report"


def test_disabled_short_circuits(tmp_path):
    _write_report(tmp_path)

    window = select_report_window("600584", reports_root_path=tmp_path, enabled=False)

    assert window == ReportWindow(symbol="600584", reason="disabled")


def test_result_is_pydantic_model_with_defaults():
    """契约：``ReportWindow`` / ``WindowSection`` 全字段带默认值（append 字段不破坏历史）。"""
    window = ReportWindow()

    assert window.symbol == "" and window.reason == "" and window.sections == []
    assert window.page_count is None and window.truncated is False and window.chars == 0


def test_default_reports_root_is_repo_reports():
    assert reports_root() == REPO_ROOT / "reports"


# ── P1 回归口径：开关关闭 ⇒ 逐字回到旧实现（`_conflict_explanations(artifacts) or None`）──


def test_switch_off_window_texts_equal_legacy_and_no_side_effects(tmp_path, monkeypatch):
    """★ `report_window.enabled=false` 的**逐字**回归口径（F1 修复后的可复算版本）。

    :func:`_window_texts` 在开关关闭时的返回值必须与旧实现
    ``_conflict_explanations(artifacts) or None`` 完全相同，**且不读盘**。

    本用例**不 stub** ``node.select_report_window``（F1 修复要求），而是：

    ① ``monkeypatch.setattr(rw, "reports_root", _boom)`` 作为**反 stub 哨兵** —— 若开关
       没被透传进窗口选择（即 M5 型"没修好"变异体），就会真的走到读盘分支并触发 ``_boom``；
    ② 造一份**真实可命中的**临时 ``reports_root``（同 code、含 ``mda`` 叙事章节）—— 若开关
       失效，窗口会返回 ``## …`` 原文，使 ``texts == legacy`` 断言失败
       ⇒ 本用例对"修好/没修好"**敏感**（M5 变异可被杀死）。
    """
    import asyncio

    from alphabee.core import Artifact, ArtifactType, Run, RunStatus
    from alphabee.midterm.models import (
        CognitiveState,
        CompanyStateArtifact,
        ExpectationGap,
        StateBelief,
        VariableScores,
    )
    from alphabee.orchestrator.contracts import InsightArtifact
    from alphabee.orchestrator.nodes import midterm as node
    from alphabee.orchestrator.services import report_window as rw

    artifacts = [
        Artifact(
            id="a-insight",
            type=ArtifactType.INSIGHT_ANALYSIS,
            producer_step="synthesize_insights",
            value=InsightArtifact(core_view="核心观点：看多", confidence="medium").model_dump(mode="json"),
        ),
        _conflict_artifact(),
    ]
    legacy = node._conflict_explanations(artifacts) or None  # 旧实现的输出
    assert legacy is not None  # 哨兵：legacy 非空才能区分"只喂冲突解释"与"喂了原文"

    # 开关关闭：``_window_texts`` 内读 ``report_window_enabled()`` ⇒ False，并透传进窗口选择。
    monkeypatch.setattr(node, "report_window_enabled", lambda: False)
    # 反 stub 哨兵：开关若未真正门控窗口选择，读盘分支被走到即在此失败。
    monkeypatch.setattr(
        rw,
        "reports_root",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("enabled=false 时不得调用 reports_root()（窗口选择必须被开关短路）")
        ),
    )

    # 同 code（600584）的**真实**报告根：开关失效时窗口会命中 `mda` 章节 ⇒ texts != legacy
    _write_report(
        tmp_path,
        text="# 全文\n\n## 第三节 管理层讨论与分析\n" + "营业收入同比增长 12.5%。" * 20 + "\n",
    )
    monkeypatch.setattr(rw, "_settings", lambda: type("_S", (), {"reports_root": str(tmp_path)})())

    texts, window = node._window_texts(artifacts, symbol="600584.SH", as_of_date="2026-12-31")

    assert texts == legacy, "开关关闭时 _window_texts 必须与旧实现逐字相同（含顺序）"
    assert window.reason == "disabled"
    assert window.sections == []

    # ② 节点级：窗口不可用 + 开关关闭 ⇒ 不新增任何 report_window_* issue
    import alphabee.config as config_mod

    class _SettingsWithoutSection:
        pass

    monkeypatch.setattr(config_mod, "get_settings", lambda: _SettingsWithoutSection())
    captured: dict[str, object] = {}

    def _fake_collect(symbol, thesis="", window_texts=None, model=None):
        captured["window_texts"] = window_texts
        return [], []

    def _fake_decision(symbol, evidence=None, **kwargs):
        return CompanyStateArtifact(
            symbol=symbol,
            thesis=str(kwargs.get("thesis") or ""),
            state=StateBelief(
                distribution={"S1": 0.5, "S2": 0.5},
                argmax_state=CognitiveState.S2_CONFIRM.value,
                entropy=0.0,
            ),
            expectation_gap=ExpectationGap(),
            variable_scores=VariableScores(),
            evidence_log=[],
        )

    monkeypatch.setattr(node, "collect_evidence_split", _fake_collect)
    monkeypatch.setattr(node, "get_decision", _fake_decision)
    run = Run(
        id="run-legacy",
        goal="分析",
        status=RunStatus.RUNNING,
        context={"symbol": "600519.SH", "as_of_date": "2026-12-31"},
    )
    result = asyncio.run(
        node.resolve_midterm_decision(
            {"run": run, "steps": [], "artifacts": artifacts, "issues": [], "decisions": []}, {}
        )
    )

    assert captured["window_texts"] == legacy
    assert legacy is not None and "盈利增长但现金流恶化" in legacy[0]
    assert [issue for issue in result["issues"] if issue.category.startswith("report_window_")] == []


def test_disabled_selection_reads_nothing(tmp_path, monkeypatch):
    """``enabled=False`` 必须在读配置/读盘**之前**返回（连配置都不看）。"""
    import alphabee.config as config_mod

    def _boom():
        raise AssertionError("enabled=False 时不得读配置")

    monkeypatch.setattr(config_mod, "get_settings", _boom)
    _write_report(tmp_path)

    window = select_report_window("600584", reports_root_path=tmp_path, enabled=False)

    assert window.sections == [] and window.reason == "disabled"


def _conflict_artifact():
    """一条已验证冲突（``explanation`` 即旧的唯一窗口文本来源）。"""
    from alphabee.agents.schemas import ConflictAnalysisResult
    from alphabee.core import Artifact, ArtifactType

    return Artifact(
        id="a-conflict",
        type=ArtifactType.CONFLICTS_RESULT,
        producer_step="explore_conflicts",
        value=ConflictAnalysisResult.model_validate(
            {
                "conflicts": [
                    {
                        "id": "c1",
                        "theme": "盈利增长但现金流恶化",
                        "description": "利润增长没有被现金流验证。",
                        "related_dimensions": ["earnings_quality"],
                        "severity": "high",
                        "confidence": 0.9,
                        "hypotheses": [
                            {
                                "id": "h1",
                                "conflict_id": "c1",
                                "explanation": "收入质量不足",
                                "predictions": [],
                                "required_evidence": [],
                                "score": 0.8,
                                "status": "verified",
                            }
                        ],
                    }
                ]
            }
        ).model_dump(mode="json"),
    )


# ── 配置开关读取（fail-open）────────────────────────────────────────────────


def test_report_window_enabled_reads_settings(monkeypatch):
    import alphabee.config as config_mod
    from alphabee.config import ReportWindowSettings

    class _Settings:
        report_window = ReportWindowSettings(enabled=False)

    monkeypatch.setattr(config_mod, "get_settings", lambda: _Settings())
    assert report_window_enabled() is False

    class _EnabledSettings:
        report_window = ReportWindowSettings(enabled=True)

    monkeypatch.setattr(config_mod, "get_settings", lambda: _EnabledSettings())
    assert report_window_enabled() is True


def test_missing_report_window_section_defaults_true_but_broken_settings_is_off(monkeypatch):
    """两条口径分开钉：① 旧 ``config.yaml`` 缺段 → §15.1-F 默认 ``True``（不变量）；
    ② ``get_settings()`` 返回的对象**根本没有** ``report_window`` 属性（配置不可用）
    → ``False``（fail-open 退回现状，不凭空打开新行为）。"""
    import alphabee.config as config_mod

    # ① 缺段（旧 config.yaml 的真实形状）：Settings 用默认值补齐 ⇒ 开关为 True
    assert config_mod.ReportWindowSettings().enabled is True
    assert config_mod.get_settings().report_window.enabled is True  # 真跑项目 config.yaml

    class _SettingsWithoutSection:
        pass

    monkeypatch.setattr(config_mod, "get_settings", lambda: _SettingsWithoutSection())
    assert report_window_enabled() is False


def test_report_window_enabled_failure_defaults_off(monkeypatch):
    """配置读取异常 → 回落 ``False``（退回现状，不凭空打开新行为）。"""
    import alphabee.config as config_mod

    def _boom():
        raise RuntimeError("no config")

    monkeypatch.setattr(config_mod, "get_settings", _boom)
    assert report_window_enabled() is False


def test_config_budget_knobs_are_wired(tmp_path, monkeypatch):
    """§15.1-F 的 ``max_chars`` / ``max_sections`` 必须**真的生效**（不是死配置）。"""
    import alphabee.config as config_mod
    from alphabee.config import ReportWindowSettings

    text = "".join(f"## 风险因素 {i}\n" + "乙" * 50 + "\n" for i in range(4))
    _write_report(tmp_path, text=text)

    class _Settings:
        report_window = ReportWindowSettings(max_chars=120, max_sections=1)

    monkeypatch.setattr(config_mod, "get_settings", lambda: _Settings())
    window = select_report_window("600584", reports_root_path=tmp_path)

    assert window.reason == ""
    assert len(window.sections) == 1
    assert window.chars <= 120
    assert window.truncated is True

    # 显式入参优先于配置（节点/调用方仍可覆盖）
    explicit = select_report_window("600584", reports_root_path=tmp_path, max_chars=60, max_sections=4)
    assert explicit.chars <= 60


def test_config_reports_root_is_wired(tmp_path, monkeypatch):
    """``report_window.reports_root`` 生效：默认根目录取不到时，配置指向的根能取到。"""
    import alphabee.config as config_mod
    from alphabee.config import ReportWindowSettings

    _write_report(tmp_path, text="## 第三节 管理层讨论与分析\n配置根正文\n")

    class _Settings:
        report_window = ReportWindowSettings(reports_root=str(tmp_path))

    monkeypatch.setattr(config_mod, "get_settings", lambda: _Settings())
    assert select_report_window("600584").reason == ""
    assert select_report_window("600584").sections[0].text.endswith("配置根正文")

    # 显式入参优先：换一个（空的）根目录 → 取不到
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    assert select_report_window("600584", reports_root_path=empty_root).reason == "no_local_report"


def test_example_config_section_matches_model_defaults():
    """``config.yaml.example`` 的 ``report_window`` 段与模型默认值**同源**（防漂移）。"""
    import yaml

    from alphabee.config import ReportWindowSettings

    example = yaml.safe_load((REPO_ROOT / "config.yaml.example").read_text(encoding="utf-8"))
    configured = example["report_window"]
    defaults = ReportWindowSettings()
    assert configured == {
        "enabled": defaults.enabled,
        "max_chars": defaults.max_chars,
        "max_sections": defaults.max_sections,
        "reports_root": defaults.reports_root,
    }
    # 示例配置必须能被模型接受（键名打错会在此暴露，而不是在用户 import 期）
    assert ReportWindowSettings(**configured).enabled is defaults.enabled


# ── F11：`report_window_unavailable` 文案二分支回归钉（verifier-rc F11）──────────


def test_unavailable_issue_wording_branches_on_window_texts(monkeypatch):
    """★ F11：`report_window_unavailable` 文案由「``window_texts`` 是否为 ``None``」**真实决定**。

    **分支前置条件（与 truncated 档互斥，改 stub 时勿走错分支）**：本用例走节点的
    ``if not window.sections and window.reason:`` 分支 ⇒ stub 必须 ``sections=[]`` 且
    ``reason`` **非空**（此处 ``"no_local_report"``）；若置 ``reason=""`` 就会落到
    ``elif window.truncated:``（那是 F13 档的地盘）。

    期望串为**逐字字面量**（不从实现侧复用拼接逻辑，避免随实现漂移）：
    ① ``window_texts is None``（artifacts 里没有已验证冲突解释）⇒ «…；本次仅数值类证据参与方向判定。»
    ② 含 1 条已验证冲突解释 ⇒ «…；本次定性判定仅依赖已验证冲突解释。»

    判别力自证（**两种变异都必须红**，由 verifier-rc 的 M11/M12 实测确认）：
    ① 把二分支改回**无条件旧文案** ⇒ **仅场景 B 红**；
    ② 把分支条件**取反**（``is None`` → ``is not None``）⇒ **场景 A 与 B 都红**。
    （注：**不得**写成"恒按 ``None`` 分支 ⇒ 场景 A 红"—— A 期望的正是 ``None`` 分支文案，
    该写法已由 M11/M12 实测证伪。）
    """
    import asyncio

    from alphabee.core import Artifact, ArtifactType, Run, RunStatus
    from alphabee.midterm.models import (
        CognitiveState,
        CompanyStateArtifact,
        ExpectationGap,
        StateBelief,
        VariableScores,
    )
    from alphabee.orchestrator.contracts import InsightArtifact
    from alphabee.orchestrator.nodes import midterm as node
    from alphabee.orchestrator.services.report_window import ReportWindow

    def _fake_decision(symbol, evidence=None, **kwargs):
        return CompanyStateArtifact(
            symbol=symbol,
            thesis=str(kwargs.get("thesis") or ""),
            state=StateBelief(
                distribution={"S1": 0.5, "S2": 0.5},
                argmax_state=CognitiveState.S2_CONFIRM.value,
                entropy=0.0,
            ),
            expectation_gap=ExpectationGap(),
            variable_scores=VariableScores(),
            evidence_log=[],
        )

    insight = Artifact(
        id="a-i-f11",
        type=ArtifactType.INSIGHT_ANALYSIS,
        producer_step="synthesize_insights",
        value=InsightArtifact(core_view="看多", confidence="medium").model_dump(mode="json"),
    )

    def _message(artifacts):
        monkeypatch.setattr(
            node,
            "select_report_window",
            lambda symbol, **kw: ReportWindow(symbol=symbol, sections=[], reason="no_local_report"),
        )
        # **显式**固定开关，避免"靠真实 config.yaml 缺段回落 True"的隐性依赖。
        monkeypatch.setattr(node, "report_window_enabled", lambda: True)
        monkeypatch.setattr(node, "collect_evidence_split", lambda *a, **k: ([], []))
        monkeypatch.setattr(node, "get_decision", _fake_decision)
        run = Run(
            id="r-f11",
            goal="g",
            status=RunStatus.RUNNING,
            context={"symbol": "600519.SH", "as_of_date": "2026-12-31"},
        )
        result = asyncio.run(
            node.resolve_midterm_decision(
                {"run": run, "steps": [], "artifacts": artifacts, "issues": [], "decisions": []},
                {},
            )
        )
        return next(i.message for i in result["issues"] if i.category == "report_window_unavailable")

    assert _message([insight]) == "财报原文窗口不可用（no_local_report）；本次仅数值类证据参与方向判定。"
    assert (
        _message([insight, _conflict_artifact()])
        == "财报原文窗口不可用（no_local_report）；本次定性判定仅依赖已验证冲突解释。"
    )


# ── F13：`report_window_truncated` 文案（组份额截断）参数化回归钉 ─────────────


@pytest.mark.parametrize("chars", [0, 4101])
def test_truncated_issue_wording_uses_group_quota(monkeypatch, chars):
    """★ F13：`report_window_truncated` 文案必须是「**被组份额截断**」（不是把截断归因于**预算**的旧说法）。

    **为何改**：等额分配（裁定 C）下截断由**组份额**引起、**总预算从未耗尽**（实测 601138 =
    4,101 < 12,000）⇒ 旧文案（把截断归因于**预算**而非组份额）与真实条件不符（F4 同类）。

    **分支前置条件（与 F11 档互斥）**：本用例走节点的 ``elif window.truncated:`` 分支 ⇒
    stub 必须 ``sections=[]`` **且 ``reason=""``**；若置 ``reason`` 非空就会落到
    ``if not window.sections and window.reason:``（那是 F11 档的地盘）。

    参数化两档：``chars=0`` 钉**分支**、``chars=4101`` 钉**数值插值**（只测 0 会漏掉"文案里
    丢掉 ``{window.chars}`` 插值"这类假绿）；期望串为逐字字面量，**不加**否定式断言。

    判别力自证：把文案改回被**预算**截断的旧说法 ⇒ 两档**必红**（verifier-rc 的 M13 变异）。
    """
    import asyncio

    from alphabee.core import Artifact, ArtifactType, Run, RunStatus
    from alphabee.midterm.models import (
        CognitiveState,
        CompanyStateArtifact,
        ExpectationGap,
        StateBelief,
        VariableScores,
    )
    from alphabee.orchestrator.contracts import InsightArtifact
    from alphabee.orchestrator.nodes import midterm as node
    from alphabee.orchestrator.services.report_window import ReportWindow

    def _fake_decision(symbol, evidence=None, **kwargs):
        return CompanyStateArtifact(
            symbol=symbol,
            thesis=str(kwargs.get("thesis") or ""),
            state=StateBelief(
                distribution={"S1": 0.5, "S2": 0.5},
                argmax_state=CognitiveState.S2_CONFIRM.value,
                entropy=0.0,
            ),
            expectation_gap=ExpectationGap(),
            variable_scores=VariableScores(),
            evidence_log=[],
        )

    insight = Artifact(
        id="a-i-f13",
        type=ArtifactType.INSIGHT_ANALYSIS,
        producer_step="synthesize_insights",
        value=InsightArtifact(core_view="看多", confidence="medium").model_dump(mode="json"),
    )
    monkeypatch.setattr(
        node,
        "select_report_window",
        lambda symbol, **kw: ReportWindow(symbol=symbol, sections=[], reason="", chars=chars, truncated=True),
    )
    monkeypatch.setattr(node, "report_window_enabled", lambda: True)
    monkeypatch.setattr(node, "collect_evidence_split", lambda *a, **k: ([], []))
    monkeypatch.setattr(node, "get_decision", _fake_decision)

    run = Run(
        id=f"r-f13-{chars}",
        goal="g",
        status=RunStatus.RUNNING,
        context={"symbol": "600519.SH", "as_of_date": "2026-12-31"},
    )
    result = asyncio.run(
        node.resolve_midterm_decision(
            {"run": run, "steps": [], "artifacts": [insight], "issues": [], "decisions": []},
            {},
        )
    )

    message = next(i.message for i in result["issues"] if i.category == "report_window_truncated")
    if chars == 0:
        assert message == "财报原文窗口被组份额截断（0 字符）。"
    else:
        assert message == "财报原文窗口被组份额截断（4101 字符）。"
