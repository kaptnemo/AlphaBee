"""对标组端到端构建（COMPANY_TRACK Phase C，C1/C3/C4 汇总）。

来源优先级（C1）：调用方直接给候选（人工/结构化）> 公司业务描述 LLM 推断（本地财报，
最接近人工选股）> 研报/业绩会 LLM 抽取 > 同行业成分股闭集 LLM 择优 > 空。
C4 校验拆分：A 股经 tushare 存在性校验进 ``codes``（基准计算）；
境外代码进 ``international``（仅名单）；无法识别交易所的候选剔除并告警。
C3 持久化：``data/peer_groups/{symbol}.json``（原子写、latest-wins、人工可编辑覆盖）。
"""

from __future__ import annotations

from typing import Any

import alphabee.company_track.peer_judge as peer_judge
from alphabee.company_track.contracts import SegmentSnapshot
from alphabee.company_track.peer_extract import (
    extract_peer_candidates,
    infer_peer_candidates,
    select_peer_candidates,
)
from alphabee.company_track.peer_group_store import PeerGroup, PeerGroupStore
from alphabee.company_track.peer_judge import (
    DEFAULT_CUSTOMER_FLOOR,
    DEFAULT_MIN_OVERLAP,
    DEFAULT_PRODUCT_FLOOR,
    DROP_CUSTOMER_FLOOR,
    DROP_JUDGE_REJECT,
    DROP_PRODUCT_FLOOR,
    MIN_PEERS_DEFAULT,
    _has_dims,
    coerce_overlap,
    format_drop_note,
    normalize_dims,
    overlap_score,
)
from alphabee.company_track.peer_universe import build_peer_universe, resolve_current_code_by_name
from alphabee.company_track.peer_validate import (
    split_domestic_international,
    validate_a_share_codes,
)

# ── 判定 C 公用件（**单一实现**在 peer_judge**）**：此处仅为向后兼容再导出，供既有调用点/测试沿用 ──
#: 四维权重（overlap = Σ w_i·dim_i）——唯一实现在 peer_judge.DEFAULT_WEIGHTS
DEFAULT_WEIGHTS = peer_judge.DEFAULT_WEIGHTS
#: 结构化匹配维度定义——唯一实现在 peer_judge.DIMS
DIMS = peer_judge.DIMS
#: notes 里 reason 截断上限——唯一实现在 peer_judge.REASON_MAX_CHARS
REASON_MAX_CHARS = peer_judge.REASON_MAX_CHARS


# ── 判定 C：确定性质量闸（权重/阈值唯一处 = peer_judge；本模块只做**接线与落盘**） ──
def gate_candidates(
    candidates: list[dict[str, Any]],
    *,
    weights: dict[str, float] | None = None,
    min_overlap: float = DEFAULT_MIN_OVERLAP,
    product_floor: float = DEFAULT_PRODUCT_FLOOR,
    customer_floor: float = DEFAULT_CUSTOMER_FLOOR,
    bypass: bool = False,
    require_score: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, float], dict[str, dict[str, float]]]:
    """确定性质量闸：按序剔除并返回保留者 + 明细 + 分数/维度持久化表。

    剔除规则（命中即 drop，设计 §3.3）：① ``verdict == "reject"`` ② ``product < product_floor``
    ③ ``customer < customer_floor`` ④ 合成 ``overlap < min_overlap``；E 特征只作提示、不参与硬闸。
    ``bypass=True`` 只归一化/记分不剔除（调用方直传的人工白名单）。

    - **合成分**：``overlap`` 一律 = Σ w_i·dim_i（权重唯一处）；候选缺 dims 时回落到其自评
      ``overlap``（历史/手工候选兼容），两者皆缺 ⇒ 0。
    - **维度下限**只在候选携带 dims 时生效（缺 dims 时不因"缺字段"被误杀）。
    - **缺分候选**：``require_score=True``（默认，与评测 harness 同口径）时，既无 dims 也无自评
      ``overlap`` 的候选中视为**不可评估 ⇒ 剔除**；``require_score=False`` 用于**无分数来源**
      （研报片段抽取 ``extract_peer_candidates`` / 闭集择优 ``select_peer_candidates`` / 调用方直传）
      —— 那两条路径本就不产出 dims/overlap，若执行阈值剔除会把整条来源静默清空成空对标组。
    """
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    scores: dict[str, float] = {}
    match_dims: dict[str, dict[str, float]] = {}

    for cand in candidates:
        code = str(cand.get("code") or "")
        raw_dims = cand.get("dims")
        dims = normalize_dims(raw_dims)
        has_dims = _has_dims(raw_dims)
        # 判定 C 的合成口径：overlap 一律由**权重 × 四维**合成（设计 §3.3「权重唯一处」），
        # 与离线评测 harness 的闸门口径一致；生成器自评分只作**血缘留痕**（record.overlap）。
        reported = coerce_overlap(cand.get("overlap"))
        # ``has_score``：候选是否**带分数**（有 dims，或带 LLM 自评 ``overlap``）。
        # **无分数来源不做评分类剔除** —— 研报片段抽取（``extract_peer_candidates``）与闭集择优
        # （``select_peer_candidates``）两条路径的候选不带 dims / overlap，若对它们执行
        # ``effective(0.0) < min_overlap`` 会**整条静默清空成空对标组**（相对 ``cd5e57b`` 的回归）。
        has_score = has_dims or reported is not None
        effective = overlap_score(dims, weights) if has_dims else (reported or 0.0)
        verdict = str(cand.get("verdict") or "").strip().lower()

        drop = ""
        if bypass:
            pass  # 人工候选白名单：只记分不剔除
        elif verdict == "reject":
            drop = DROP_JUDGE_REJECT
        elif has_dims and dims["product"] < product_floor:
            drop = DROP_PRODUCT_FLOOR
        elif has_dims and dims["customer"] < customer_floor:
            drop = DROP_CUSTOMER_FLOOR
        elif not has_score and require_score:
            # 不可评估（既无 dims 又无自评分）：与 harness 同口径 ⇒ 剔（不做"缺分即保留"）
            drop = f"overlap {effective:.2f} < {min_overlap:.2f}"
        elif has_score and effective < min_overlap:
            drop = f"overlap {effective:.2f} < {min_overlap:.2f}"
        record = {
            **cand,
            "overlap": reported if reported is not None else effective,
            "gate_overlap": effective,
            "dims": dims,
            "reason": str(cand.get("reason") or "").strip(),
        }
        if drop:
            dropped.append({**record, "drop": drop})
            continue
        kept.append(record)
        # 只对**有分数**的候选落 ``scores``：未评分来源没有 overlap，写 0.0 会被读成
        # 「重叠度为 0」的假事实；``match_dims`` 同理只在携带 dims 时记录。
        if code and has_score:
            scores[code] = round(effective, 4)
            match_dims[code] = dims

    return kept, dropped, scores, match_dims


def _peer_quality_settings() -> dict[str, Any]:
    """读 ``company_track.peer_quality`` 配置段；**缺段/读取失败一律回落默认值**。

    配置是可选增强：未配置时行为 = 本模块默认常量（旧 config 仍可 import/运行）。
    按 ``Settings`` 对象身份单槽记忆化，避免每次构建都重复 ``model_dump``。
    """
    try:
        from alphabee.config import get_settings

        settings_obj = get_settings()
    except Exception:
        return {}
    cached = _PEER_QUALITY_CACHE.get("value")
    if cached is not None and cached[0] is settings_obj:
        return dict(cached[1])
    try:
        section = getattr(settings_obj, "company_track", None)
        section = getattr(section, "peer_quality", None)
        if section is None:
            return {}
        dumped = {key: value for key, value in section.model_dump().items() if value is not None}
    except Exception:
        return {}
    _PEER_QUALITY_CACHE["value"] = (settings_obj, dict(dumped))
    return dumped


#: ``_peer_quality_settings`` 的单槽记忆化（键 = Settings 实例身份）
_PEER_QUALITY_CACHE: dict[str, Any] = {}


def _extend_unique(target: list[str], extra: list[str]) -> None:
    """按序去重追加（同一候选可被来源侧与 Gate 各剔一次，notes 只留一条）。"""
    seen = set(target)
    for line in extra:
        if line not in seen:
            seen.add(line)
            target.append(line)


def _format_dropped(dropped: list[dict[str, Any]]) -> list[str]:
    """质量闸剔除明细 → 人类可读 notes（code/name/drop/reason）。"""
    lines: list[str] = []
    for item in dropped:
        row = dict(item)
        # drop 文本已含阈值时不重复打印 overlap 数值
        if isinstance(row.get("overlap"), (int, float)) and "overlap" not in str(row.get("drop") or ""):
            row["drop"] = f"{row.get('drop') or ''} overlap={float(row['overlap']):.2f}"
        lines.append(format_drop_note(row))
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
    infer_used = False
    meta: dict[str, Any] = {}
    if not candidates:
        if use_llm and business_description:
            candidates, meta = infer_peer_candidates(
                symbol, segments or [], business_description, industry=industry, use_llm=True
            )
            llm_used = True
            infer_used = True
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

    # 来源侧（LLM 打分阶段）已剔除的候选也入 notes：与 Gate 的剔除面不同，两者都要可审计
    if candidates:
        _extend_unique(warnings, _format_dropped(meta.get("dropped") or []))

    # ── C（判定）：确定性质量闸（权重/阈值唯一处 = peer_extract + 配置） ──
    # 只对 **LLM 产出** 的候选做剔除：``candidates`` 直传是调用方（人工/分析师）给定的白名单，
    # 按来源优先级最高、不经质量闸（否则会反向覆盖人工判断）。
    gate_cfg = _peer_quality_settings()
    # 判定 C 打分来源（LLM 推断）⇒ 严格口径（缺分即不可评估，与 harness 同源）；
    # 无分数来源（研报片段抽取 / 闭集择优 / 调用方直传）⇒ require_score=False（保持既有行为）
    scored_source = infer_used
    gate_kwargs: dict[str, Any] = {
        "require_score": scored_source,
        "weights": gate_cfg.get("weights"),
        "min_overlap": float(gate_cfg.get("min_overlap", DEFAULT_MIN_OVERLAP)),
        "product_floor": float(gate_cfg.get("product_floor", DEFAULT_PRODUCT_FLOOR)),
        "customer_floor": float(gate_cfg.get("customer_floor", DEFAULT_CUSTOMER_FLOOR)),
    }
    if llm_used:
        candidates, gate_dropped, scores, match_dims = gate_candidates(list(candidates), **gate_kwargs)
        _extend_unique(warnings, _format_dropped(gate_dropped))
    else:
        # 人工候选：仍记录分数/维度（若调用方给了），但不剔除
        candidates, _manual_dropped, scores, match_dims = gate_candidates(list(candidates), bypass=True, **gate_kwargs)
    if not candidates:
        warnings.append("质量闸后无保留候选（不编造），空对标组")
        # 设计 §3.6「全空 ⇒ ``no_peers = 生成器与 judge 均有效响应且无保留``」：Gate 把候选
        # 全部剔除与「生成器零候选」同属**终态空组**，必须同样置位，否则每次分析都会重复
        # 调用 LLM 走在线兜底。本步尚无独立 judge ⇒ 以生成器的 ``meta.llm_ok`` 为过渡口径。
        no_peers = bool(use_llm and meta.get("llm_ok"))
        group = PeerGroup(
            symbol=symbol,
            name=name,
            source="llm" if llm_used else "manual",
            notes=list(warnings),
            no_peers=no_peers,
        )
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

    codes = [c["code"] for c in domestic]
    code_set = set(codes)
    min_peers = int(gate_cfg.get("min_peers", MIN_PEERS_DEFAULT))
    if len(codes) < min_peers:
        # 消费侧最小数量闸的前置留痕（节点据此不注入 peer_*、回退 industry）
        warnings.append(f"对标组不足（{len(codes)} < {min_peers}），中位数不可比，回退 industry 基线")

    group = PeerGroup(
        symbol=symbol,
        codes=codes,
        international=[c["code"] for c in international],
        source="llm" if llm_used else "manual",
        name=name,
        notes=list(warnings),
        reason_map=reason_map,
        scores={code: value for code, value in scores.items() if code in code_set},
        match_dims={code: dims for code, dims in match_dims.items() if code in code_set},
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
