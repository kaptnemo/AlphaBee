"""对标组端到端构建（COMPANY_TRACK Phase C，C1/C3/C4 汇总）。

来源优先级（C1）：调用方直接给候选（人工/结构化）> 研报/业绩会 LLM 抽取 > 空。
C4 校验拆分：A 股经 tushare 存在性校验进 ``codes``（基准计算）；
境外代码进 ``international``（仅名单）；无法识别交易所的候选剔除并告警。
C3 持久化：``data/peer_groups/{symbol}.json``（原子写、latest-wins、人工可编辑覆盖）。
"""

from __future__ import annotations

from alphabee.company_track.contracts import SegmentSnapshot
from alphabee.company_track.peer_extract import extract_peer_candidates
from alphabee.company_track.peer_group_store import PeerGroup, PeerGroupStore
from alphabee.company_track.peer_validate import (
    split_domestic_international,
    validate_a_share_codes,
)


def build_peer_group(
    symbol: str,
    *,
    candidates: list[dict[str, str]] | None = None,
    fragments: list[str] | None = None,
    segments: list[SegmentSnapshot] | None = None,
    use_llm: bool = True,
    name: str = "",
    store: PeerGroupStore | None = None,
) -> tuple[PeerGroup, list[str]]:
    """端到端构建对标组并持久化。

    Args:
        symbol: 标的代码。
        candidates: 调用方直接给的对标候选（人工白名单/结构化来源，优先级最高）；
            未给且 ``use_llm`` + ``fragments`` 时走 LLM 抽取。
        fragments: 研报/业绩会文本片段（LLM 抽取输入）。
        segments: 业务线分项（LLM 抽取参考）。
        use_llm: 是否启用 LLM 抽取。
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
    if not candidates:
        if use_llm and fragments:
            candidates, meta = extract_peer_candidates(symbol, segments or [], fragments, use_llm=True)
            llm_used = True
            if meta.get("note"):
                warnings.append(meta["note"])
        if not candidates:
            warnings.append("无对标组候选（人工白名单与 LLM 抽取均未产出），空对标组（不编造）")
            group = PeerGroup(symbol=symbol, name=name, source="manual", notes=list(warnings))
            store.save(group)
            return group, warnings

    # ── C4：代码规范化 + A 股/境外拆分 ─────────────────────────────
    domestic, international, invalid = split_domestic_international(candidates)
    if invalid:
        warnings.append(f"无法识别交易所的候选（已剔除）: {invalid}")

    # A 股存在性校验（best-effort；失败按格式放行并告警）
    if domestic:
        valid, bad, error = validate_a_share_codes([c["code"] for c in domestic])
        if error:
            warnings.append(error)
        if bad:
            warnings.append(f"A 股代码未通过存在性校验（已剔除）: {bad}")
        domestic = [c for c in domestic if c["code"] in set(valid)]

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
