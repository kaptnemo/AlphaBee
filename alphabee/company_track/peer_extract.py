"""对标组 LLM 抽取（COMPANY_TRACK Phase C2，agent.peer_group）。

输入：研报/业绩会文本片段（管理层点名的对标公司）+ 业务线构成（参考）；
输出：JSON 对标组候选 ``[{name, code, exchange, reason, source}]``。
失败/无文本/置信低 → 空列表（降级，不编造——绝不在无依据时产出对标组）。
"""

from __future__ import annotations

import json
from typing import Any

from alphabee.company_track.contracts import SegmentSnapshot
from alphabee.company_track.peer_judge import DEFAULT_MIN_OVERLAP, normalize_dims
from alphabee.company_track.peer_judge import coerce_overlap as _coerce_overlap

#: 阈值（``min_overlap``）的**权威定义只有一处** = :data:`alphabee.company_track.peer_judge.DEFAULT_MIN_OVERLAP`；
#: 生产生效值另有配置覆盖（``company_track.peer_quality.min_overlap``），缺段即取该默认。
#: 本模块历史上自带过一份取值 ``0.5`` 的同名常量（看似权威、实际不参与任何判定）——**已删除**，
#: 以免「同名异值」误导文档 / DoD 表述 / 下游 ``import``；此处仅作**向后兼容再导出**
#: （``infer_peer_candidates`` 的 ``min_overlap`` 参数已不参与剔除，判定统一由 Gate 执行）。


def _segment_lines(segments: list[SegmentSnapshot]) -> str:
    return (
        "\n".join(
            f"- {seg.segment_name}（{seg.category or '未分类'}）: "
            f"占比 {seg.revenue_share if seg.revenue_share is not None else '—'}%"
            for seg in segments
        )
        or "（无业务线数据）"
    )


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
    meta: dict[str, Any] = {"note": "", "raw": None, "llm_ok": False}
    if not use_llm:
        meta["note"] = "LLM 抽取关闭"
        return [], meta
    if not fragments:
        meta["note"] = "无研报/业绩会文本，跳过 LLM 抽取（不编造对标组）"
        return [], meta

    try:
        from alphabee.utils.llm import create_chat_model
        from alphabee.utils.pipeline import parse_json

        segment_lines = _segment_lines(segments)
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
        parsed = parse_json(str(raw))
        if not isinstance(parsed, list):
            meta["note"] = "LLM 输出非 JSON 数组"
            return [], meta
        meta["llm_ok"] = True

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


def infer_peer_candidates(
    symbol: str,
    segments: list[SegmentSnapshot],
    business_description: str,
    *,
    industry: str = "",
    use_llm: bool = True,
    min_overlap: float = DEFAULT_MIN_OVERLAP,  # 兼容既有调用签名；判定已移交 Gate（见下）
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """依据**公司业务描述**（本地年报/半年报「管理层讨论与分析」）推断 A 股直接对标。

    与 :func:`extract_peer_candidates`（只抽取文本里**点名**的竞对）不同：公司财报通常只描述
    自身业务、不点名竞对，故这里让 LLM 结合业务描述与行业常识**推断同产业链环节的直接竞对**
    （如深南电路「通信设备 PCB / 封装基板」→ 沪电股份/胜宏科技/兴森科技），并显式**排除上游
    供应商与下游客户**（盈利结构不同，混入会污染同业中位数）。代码仍由 ``build_peer_group``
    经 Tushare 存在性校验，编造代码会在下游被剔除。

    **质量闸**——对标组用于同业中位数，宁可少选：
    1. **结构化维度**：要求 LLM 逐条给出 ``dims``（product/customer/material_tech/business_model，
       各 0–1）与 ``overlap``，随候选返回（归一化只有一份实现）；
       注：**overlap 阈值不再由本函数执行**（见第 3 条）。
    2. **理由非空**：理由缺失者剔除（**不再按理由措辞判定**——中文关键词否决表已删除：
       措辞不可穷举、跨行业不可移植；实质性差异改由 ``peer_group_build.gate_candidates``
       的结构化维度与 verdict 承载）。
    3. **生成器不再按 overlap 自行剔除**：overlap 阈值与维度下限统一由构建阶段的确定性
       Gate 执行（设计 §4「生成器出 dims+overlap、Gate 做剔除」），故本函数**返回 LLM 给出的
       全部候选**（仅去重/去无理由，含 ``overlap`` 与四维 ``dims``），剔除明细由 Gate 落入 notes。

    Args:
        symbol: 标的代码（血缘）。
        segments: 业务线分项（供 LLM 判断赛道定位）。
        business_description: 公司业务描述文本（本地财报「管理层讨论与分析」章节）。
        industry: 行业名（血缘，仅入 prompt）。
        use_llm: 是否启用 LLM 推断。
        min_overlap: 兼容参数（历史签名）；**本函数不再据此剔除**——阈值判定由构建阶段
            :func:`gate_candidates` 统一执行。

    Returns:
        ``(candidates, meta)``：candidates 每条为
        ``{"name", "code", "exchange", "reason", "source": "infer", "overlap", "dims"}``
        （``overlap`` 十进制文本、``dims`` 四维 JSON 文本，均经**单一**归一化实现处理）；
        空 → 不编造。**候选缺 dims** 时由构建阶段 Gate 只按 overlap 阈值判定（旧存量数据兼容）。
        ``meta["dropped"]`` 记录被质量闸剔除的候选（``{name, code, overlap, reason, drop}``）。
    """
    del symbol  # 血缘信息，仅日志用
    meta: dict[str, Any] = {"note": "", "raw": None, "dropped": [], "llm_ok": False}
    if not use_llm:
        meta["note"] = "LLM 推断关闭"
        return [], meta
    if not business_description.strip():
        meta["note"] = "无业务描述文本，跳过 LLM 推断（不编造）"
        return [], meta

    try:
        from alphabee.utils.llm import create_chat_model
        from alphabee.utils.pipeline import parse_json

        prompt = (
            "你是买方研究员。根据下面这家公司的业务描述，识别其在 **A 股市场**中的"
            "**同产业链环节的竞争对手/可比公司**，给出 5–8 家并按可比度降序。\n"
            "要求：\n"
            "1. 排除上游供应商与下游客户（如 PCB 公司不要选上游覆铜板、下游封测）；\n"
            "2. 优先给 A 股上市公司，代码带交易所后缀（如 002463.SZ / 603228.SH）；"
            "不确定的代码填空串，**不要编造**；\n"
            "3. 每条必须诚实给出 `overlap`（0–1，与标的在**同环节业务/产品/客户**上的重叠度）："
            "越接近直接竞对越接近 1.0；若候选主要在材料（如碳钢 vs 不锈钢）、终端"
            "（如半导体/医药洁净 vs 石化/核电）或盈利模式上与标的不同，请**如实给低分**"
            "（由下游按阈值过滤，不要因为拿不准就直接省略）。\n"
            "4. 每条还要给出**结构化维度** `dims`（各自 0–1，按证据独立打分、不要一律同值）："
            "product(产品/服务重叠)、customer(客户/终端重叠)、material_tech(材料/技术路线相近度)、"
            "business_model(盈利模式/业态相近度)。\n"
            "只输出 JSON 数组（确实无候选才输出 []），每条："
            '{"name": "公司名", "code": "股票代码", "exchange": "SH/SZ/BJ", '
            '"overlap": 0.0-1.0, "dims": {"product":0.0,"customer":0.0,"material_tech":0.0,"business_model":0.0}, '
            '"reason": "为什么是同环节竞对（业务/产品/客户重叠）"}。\n'
            f"行业（供参考）: {industry or '未标注'}\n"
            f"业务线构成（供参考）:\n{_segment_lines(segments)}\n\n"
            f"公司业务描述:\n{business_description}"
        )
        model = create_chat_model("agent.peer_group")
        raw = model.invoke(prompt).content
        meta["raw"] = raw
        parsed = parse_json(str(raw))
        if not isinstance(parsed, list):
            meta["note"] = "LLM 输出非 JSON 数组"
            return [], meta
        meta["llm_ok"] = True  # 有效响应（空数组也算「已判定无对标」）

        candidates: list[dict[str, str]] = []
        dropped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in parsed:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            code = str(item.get("code") or "").strip()
            reason = str(item.get("reason") or "").strip()
            if not name or (code and code in seen):
                continue
            if code:
                seen.add(code)

            overlap = _coerce_overlap(item.get("overlap"))
            # 结构化维度（判定 C）：归一化**只有一份实现**（peer_judge.normalize_dims），
            # 缺字段/非法值按 0；候选缺 dims 时 Gate 只按 overlap 阈值判定（旧存量数据兼容）。
            dims = normalize_dims(item.get("dims"))
            drop_reason = ""
            if not reason:
                drop_reason = "无理由"
            # 判定口径统一由 :func:`alphabee.company_track.peer_group_build.gate_candidates`
            # 承担（权重×四维合成 overlap + 四条剔除规则）；生成器**不再自行按 overlap 剔除**，
            # 否则会把候选挡在 Gate 之外、令剔除明细不可审计（设计 §4）。
            if drop_reason:
                dropped.append(
                    {
                        "name": name,
                        "code": code,
                        "overlap": overlap,
                        "dims": dims,
                        "reason": reason,
                        "drop": drop_reason,
                    }
                )
                continue

            candidates.append(
                {
                    "name": name,
                    "code": code,
                    "exchange": str(item.get("exchange") or "").strip().upper(),
                    "reason": reason,
                    "source": "infer",
                    "overlap": "" if overlap is None else f"{overlap:.4f}",
                    "dims": json.dumps(dims, ensure_ascii=False, sort_keys=True),
                }
            )
        meta["dropped"] = dropped
        if not candidates:
            meta["note"] = "LLM 未推断出通过质量闸的同环节对标（不编造）"
        elif dropped:
            meta["note"] = f"质量闸剔除 {len(dropped)} 条非直接对标"
        return candidates, meta
    except Exception as exc:
        meta["note"] = f"LLM 推断失败: {exc}"
        return [], meta


def select_peer_candidates(
    symbol: str,
    segments: list[SegmentSnapshot],
    universe: list[dict[str, str]],
    *,
    industry: str = "",
    use_llm: bool = True,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """从**同行业成分股闭集**中择优选出直接对标（只允许闭集内，杜绝编造）。

    与 :func:`extract_peer_candidates`（从研报文本抽取）不同，本函数给 LLM 一个**封闭候选列表**
    （申万行业成分股，``{code, name}``），要求它结合标的业务线从中选出同产业链环节的真对手。
    LLM 输出中**不在闭集内的代码一律丢弃**（防止幻觉/代码漂移）。

    Args:
        symbol: 标的代码（血缘）。
        segments: 业务线分项（供 LLM 判断赛道定位）。
        universe: 闭集候选（``[{code, name}]``，由 ``peer_universe.build_peer_universe`` 生成）。
        industry: 行业名（血缘，仅入 prompt）。
        use_llm: 是否启用 LLM 择优。

    Returns:
        ``(candidates, meta)``：candidates 每条为
        ``{"name", "code", "reason", "source": "universe"}``；空 → 不编造。
    """
    del symbol  # 血缘信息，仅日志用
    meta: dict[str, Any] = {"note": "", "raw": None, "llm_ok": False}
    if not use_llm:
        meta["note"] = "LLM 择优关闭"
        return [], meta
    allowed = {str(item.get("code") or "").strip().upper() for item in universe if item.get("code")}
    if not allowed:
        meta["note"] = "无同行业候选闭集，跳过 LLM 择优（不编造）"
        return [], meta

    try:
        from alphabee.utils.llm import create_chat_model
        from alphabee.utils.pipeline import parse_json

        universe_lines = "\n".join(
            f"- {str(item.get('code') or '').strip().upper()} {str(item.get('name') or '').strip()}"
            for item in universe
        )
        name_by_code = {
            str(item.get("code") or "").strip().upper(): str(item.get("name") or "").strip() for item in universe
        }
        prompt = (
            "你是买方研究员。下面是标的所属行业"
            f"（{industry or '未标注'}）的候选公司清单（申万行业成分股，格式：代码 名称）。\n"
            "请结合标的业务线构成，从**候选清单中**选出与该标的最直接竞争/可比的 A 股公司"
            "（同一产业链环节的真对手；不要选只是同行业但业务不同者）。\n"
            "**只能从候选清单里选择，不得新增、改动或猜测任何代码**；无合适者输出 []。\n"
            "只输出 JSON 数组（无命中输出 []），每条："
            '{"code": "候选清单中的代码", "reason": "为什么是对标（产业链环节/业务重叠）"}。\n'
            f"标的业务线构成（参考）:\n{_segment_lines(segments)}\n\n候选公司清单:\n{universe_lines}"
        )
        model = create_chat_model("agent.peer_group")
        raw = model.invoke(prompt).content
        meta["raw"] = raw
        parsed = parse_json(str(raw))
        if not isinstance(parsed, list):
            meta["note"] = "LLM 输出非 JSON 数组"
            return [], meta
        meta["llm_ok"] = True

        candidates: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in parsed:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip().upper()
            if code not in allowed or code in seen:
                continue  # 闭集外一律丢弃（防幻觉/代码漂移）
            seen.add(code)
            candidates.append(
                {
                    "name": name_by_code.get(code, ""),
                    "code": code,
                    "reason": str(item.get("reason") or "").strip(),
                    "source": "universe",
                }
            )
        if not candidates:
            meta["note"] = "LLM 未从闭集选出对标（不编造）"
        return candidates, meta
    except Exception as exc:
        meta["note"] = f"LLM 择优失败: {exc}"
        return [], meta
