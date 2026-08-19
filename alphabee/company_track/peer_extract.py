"""对标组 LLM 抽取（COMPANY_TRACK Phase C2，agent.peer_group）。

输入：研报/业绩会文本片段（管理层点名的对标公司）+ 业务线构成（参考）；
输出：JSON 对标组候选 ``[{name, code, exchange, reason, source}]``。
失败/无文本/置信低 → 空列表（降级，不编造——绝不在无依据时产出对标组）。
"""

from __future__ import annotations

from typing import Any

from alphabee.company_track.contracts import SegmentSnapshot


def extract_peer_candidates(
    symbol: str,
    segments: list[SegmentSnapshot],
    fragments: list[str],
    *,
    use_llm: bool = True,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """从研报/业绩会文本抽取对标组候选。

    Args:
        symbol: 标的代码（血缘）。
        segments: 业务线分项（最新报告期，供 LLM 参考赛道定位）。
        fragments: 研报/业绩会文本片段列表（管理层点名对标公司的原文）。
        use_llm: 是否启用 LLM 抽取。

    Returns:
        (candidates, meta)：candidates 每条为
        ``{"name", "code", "exchange", "reason", "source"}``；meta 含 ``note`` / ``raw``
        （原始 LLM 输出，血缘审计）。
    """
    del symbol  # 血缘信息，仅日志用
    meta: dict[str, Any] = {"note": "", "raw": None}
    if not use_llm:
        meta["note"] = "LLM 抽取关闭"
        return [], meta
    if not fragments:
        meta["note"] = "无研报/业绩会文本，跳过 LLM 抽取（不编造对标组）"
        return [], meta

    try:
        from alphabee.utils.llm import create_chat_model
        from alphabee.utils.pipeline import parse_json

        segment_lines = (
            "\n".join(
                f"- {seg.segment_name}（{seg.category or '未分类'}）: "
                f"占比 {seg.revenue_share if seg.revenue_share is not None else '—'}%"
                for seg in segments
            )
            or "（无业务线数据）"
        )
        prompt = (
            "你是买方研究员。从以下研报/业绩会文本片段中，提取管理层**直接点名的对标公司/竞争对手**"
            "（同一产业链环节的直接竞对，如工业富联 → 广达/纬创/英业达/华勤技术）。\n"
            "只输出 JSON 数组（无命中输出 []），每条："
            '{"name": "公司名", "code": "股票代码（带交易所后缀，如 002415.SZ / 2382.TW；不确定填空串）", '
            '"exchange": "SH/SZ/BJ/TW/HK/US…", "reason": "为什么是对标（引用原文依据）", '
            '"source": "片段编号如 #0"}。\n'
            f"标的业务线构成（参考）:\n{segment_lines}\n\n研报/业绩会片段:\n"
            + "\n---\n".join(f"#{index} {fragment}" for index, fragment in enumerate(fragments))
        )
        model = create_chat_model("agent.peer_group")
        raw = model.invoke(prompt).content
        meta["raw"] = raw
        parsed = parse_json(raw)
        if not isinstance(parsed, list):
            meta["note"] = "LLM 输出非 JSON 数组"
            return [], meta

        candidates: list[dict[str, str]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            candidates.append(
                {
                    "name": name,
                    "code": str(item.get("code") or "").strip(),
                    "exchange": str(item.get("exchange") or "").strip().upper(),
                    "reason": str(item.get("reason") or "").strip(),
                    "source": str(item.get("source") or "").strip(),
                }
            )
        if not candidates:
            meta["note"] = "LLM 未命中任何对标公司（不编造）"
        return candidates, meta
    except Exception as exc:
        meta["note"] = f"LLM 抽取失败: {exc}"
        return [], meta
