"""对标组端到端构建（COMPANY_TRACK Phase C，C1/C3/C4 汇总）。

来源优先级（C1）：调用方直接给候选（人工/结构化）> 公司业务描述 LLM 推断（本地财报，
最接近人工选股）> 研报/业绩会 LLM 抽取 > 同行业成分股闭集 LLM 择优 > 空。
C4 校验拆分：A 股经 tushare 存在性校验进 ``codes``（基准计算）；
境外代码进 ``international``（仅名单）；无法识别交易所的候选剔除并告警。
C3 持久化：``data/peer_groups/{symbol}.json``（原子写、latest-wins、人工可编辑覆盖）。
"""

from __future__ import annotations

from typing import Any

from alphabee.company_track.contracts import SegmentSnapshot
from alphabee.company_track.peer_extract import (
    extract_peer_candidates,
    infer_peer_candidates,
    select_peer_candidates,
)
from alphabee.company_track.peer_group_store import PeerGroup, PeerGroupStore
from alphabee.company_track.peer_universe import build_peer_universe, resolve_current_code_by_name
from alphabee.company_track.peer_validate import (
    split_domestic_international,
    validate_a_share_codes,
)


def _format_dropped(dropped: list[dict[str, Any]]) -> list[str]:
    """质量闸剔除明细 → 人类可读 notes（code/name/drop/reason）。"""
    lines: list[str] = []
    for item in dropped:
        drop = str(item.get("drop") or "")
        overlap = item.get("overlap")
        # drop 文本已含阈值时不重复打印 overlap 数值
        if isinstance(overlap, (int, float)) and "overlap" not in drop:
            drop = f"{drop} overlap={overlap:.2f}"
        reason = str(item.get("reason") or "").strip()
        if len(reason) > 80:
            reason = reason[:80] + "…"
        lines.append(f"质量闸剔除 {item.get('code') or '?'} {item.get('name') or ''}（{drop}）：{reason}")
    return lines


def build_peer_group(
    symbol: str,
    *,
    candidates: list[dict[str, str]] | None = None,
    business_description: str | None = None,
    fragments: list[str] | None = None,
    universe_codes: list[str] | None = None,
    industry: str = "",
    segments: list[SegmentSnapshot] | None = None,
    use_llm: bool = True,
    name: str = "",
    store: PeerGroupStore | None = None,
) -> tuple[PeerGroup, list[str]]:
    """端到端构建对标组并持久化。

    Args:
        symbol: 标的代码。
        candidates: 调用方直接给的对标候选（人工白名单/结构化来源，优先级最高）。
        business_description: 公司业务描述（本地财报「管理层讨论与分析」）——LLM 据此**推断**
            同环节 A 股直接竞对（最接近人工选股；优先于 ``fragments``）。
        fragments: 研报/业绩会文本片段（LLM 从文本**抽取**点名竞对）。
        universe_codes: 同行业成分股代码闭集（``resolve_industry_context`` 的 ``peer_universe``）；
            无前两者且 ``use_llm`` 时，LLM 从该闭集内择优（杜绝编造）。
        industry: 行业名（prompt 血缘）。
        segments: 业务线分项（LLM 参考）。
        use_llm: 是否启用 LLM 抽取/推断/择优。
        name: 对标组命名。
        store: 存储（默认 data/peer_groups）。

    Returns:
        (peer_group, warnings)：peer_group 已持久化；无任何候选时为空对标组
        （``is_empty()``，不编造）。
    """
    store = store or PeerGroupStore()
    warnings: list[str] = []

    # ── C1：候选来源优先级 ─────────────────────────────────────────
    llm_used = False
    meta: dict[str, Any] = {}
    if not candidates:
        if use_llm and business_description:
            candidates, meta = infer_peer_candidates(
                symbol, segments or [], business_description, industry=industry, use_llm=True
            )
            llm_used = True
        elif use_llm and fragments:
            candidates, meta = extract_peer_candidates(symbol, segments or [], fragments, use_llm=True)
            llm_used = True
        elif use_llm and universe_codes:
            universe = build_peer_universe(universe_codes, exclude=symbol)
            candidates, meta = select_peer_candidates(symbol, segments or [], universe, industry=industry, use_llm=True)
            llm_used = True
        # 质量闸剔除明细优先入 notes（可审计）；无明细才回落 summary note
        dropped_lines = _format_dropped(meta.get("dropped") or [])
        if dropped_lines:
            warnings.extend(dropped_lines)
        elif meta.get("note"):
            warnings.append(meta["note"])
        if not candidates:
            warnings.append("在线推断未产出直接对标（不编造），空对标组")
            # LLM 有效响应且无候选 ⇒ 判定「确无 A 股直接对标」终态，避免每次分析重复调用 LLM
            no_peers = bool(use_llm and meta.get("llm_ok"))
            group = PeerGroup(symbol=symbol, name=name, source="manual", notes=list(warnings), no_peers=no_peers)
            store.save(group)
            return group, warnings

    # ── C4：代码规范化 + A 股/境外拆分 ─────────────────────────────
    domestic, international, invalid = split_domestic_international(candidates)
    if invalid:
        warnings.append(f"无法识别交易所的候选（已剔除）: {invalid}")

    # A 股存在性校验（best-effort）：失败若候选带公司名，按名回查**当前代码**
    # （修复北交所代码迁移等陈旧代码假阴性，如 873593.BJ 实为 920593.BJ）。
    if domestic:
        valid, _bad, error = validate_a_share_codes([c["code"] for c in domestic])
        if error:
            warnings.append(error)
        valid_set = set(valid)
        kept: list[dict[str, str]] = []
        seen_codes: set[str] = set()
        for cand in domestic:
            code = cand["code"]
            if code in valid_set:
                resolved = code
            else:
                resolved = resolve_current_code_by_name(cand.get("name", "")) or ""
                if resolved and resolved != code:
                    warnings.append(f"A 股代码已迁移 {code} → {resolved}（{cand.get('name')}）")
                else:
                    warnings.append(f"A 股代码未通过存在性校验（已剔除）: {code}")
                    resolved = ""
            if not resolved or resolved in seen_codes:
                continue
            seen_codes.add(resolved)
            kept.append({**cand, "code": resolved})
        domestic = kept

    reason_map: dict[str, str] = {}
    for cand in domestic + international:
        reason_map[cand["code"]] = cand.get("reason", "")

    group = PeerGroup(
        symbol=symbol,
        codes=[c["code"] for c in domestic],
        international=[c["code"] for c in international],
        source="llm" if llm_used else "manual",
        name=name,
        notes=list(warnings),
        reason_map=reason_map,
    )
    store.save(group)
    return group, warnings


if __name__ == "__main__":
    # 测试
    from alphabee.company_track.peer_report import fetch_local_report_fragments

    business_description, meta = fetch_local_report_fragments("601138.SH", max_chars=12000)
    print("Business Description:", business_description)
    group, warnings = build_peer_group(
        "601138.SH",
        business_description=business_description[0],
        use_llm=True,
    )
    print(group)
    print("Warnings:", warnings)
