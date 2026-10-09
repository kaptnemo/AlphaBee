"""对标组端到端构建（COMPANY_TRACK Phase C，C1/C3/C4 汇总）。

来源优先级（C1）：调用方直接给候选（人工/结构化）> 公司业务描述 LLM 推断（本地财报，
最接近人工选股）> 研报/业绩会 LLM 抽取 > 同行业成分股闭集 LLM 择优 > 空。
C4 校验拆分：A 股经 tushare 存在性校验进 ``codes``（基准计算）；
境外代码进 ``international``（仅名单）；无法识别交易所的候选剔除并告警。
C3 持久化：``data/peer_groups/{symbol}.json``（原子写、latest-wins、人工可编辑覆盖）。

判定链（设计 §3.2/§3.6/§4，C/D 分工）：

1. **生成器**（Recall，``peer_extract.infer_peer_candidates``）出候选 + 自评 ``overlap`` + 四维 ``dims``；
2. **独立 judge**（D，``peer_judge.judge_peer_candidates_batched``）：对生成器候选池做一次批量独立评审，
   逐候选取 ``verdict`` + ``dims``（``judge_enabled=false`` 时跳过 ⇒ 判定回到 C 口径）；
3. **确定性 Gate**（C，本模块 :func:`gate_candidates`）：按序 verdict 否决 → 维度下限 → 合成 overlap
   阈值剔除，**采纳决定权在 Gate**（LLM 不做取舍）。

fail-open（设计 §3.6）：judge 失败/超时/非 JSON/漏判 ⇒ 回退生成器 ``dims``/``overlap`` 继续走 Gate，
notes 记 ``judge_degraded``，**绝不**据此置 ``no_peers``；``no_peers=True`` 仅当生成器与 judge
**均有效响应且无保留**（judge 未接线时沿用生成器口径，见 :func:`_run_judge` 与 Gate 清空分支）。

判定 E（分类学，设计 §3.1/§3.3/§6 Step 3）：``peer_taxonomy`` 提供「召回池 + 特征」——
生成器推断路径额外并入同 L3 成分（残差桶 ⇒ 同 L2），并把 ``same_l3`` / ``same_l2`` 作为特征
注入 judge prompt；**E 绝不硬闸**（Gate 内没有任何以分类学取值为条件的剔除分支）。
并入的候选**未经生成器打分** ⇒ ``require_score=True`` 下只有 judge 判分后（``judge_enabled=true``）
才可能进入采纳面；judge 关闭（默认）时它们只提升**候选池召回口径**，并在 notes 聚合留痕一条。

对标组置信度（设计 §3.7/§8 决策 5）：:func:`synthesize_peer_confidence` 由三项确定性信号
（分类学可信度 / judge direct 占比 / 保留项 overlap 均值）合成三档；缺失信号按 0 处理，
不额外调 LLM。写入 ``CompanyTrackArtifact`` 的置信字段与 ``review_notes``（低置信时报告显式提示）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import alphabee.company_track.peer_judge as peer_judge
import alphabee.company_track.peer_taxonomy as peer_taxonomy
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

# ── 判定 E（分类学召回池）与对标组置信度 ─────────────────────────────
#: 质量闸总开关（``company_track.peer_quality.enabled``）停用时的 notes 留痕（**唯一处**，可被测试与审计引用）。
GATE_DISABLED_NOTE = "质量闸已停用（peer_quality.enabled=false），全部候选直接保留、不做任何剔除"

#: 分类学召回池并入的候选来源标记（与生成器/研报/闭集来源区分，用于 notes 聚合与置信度口径）。
TAXONOMY_SOURCE = "taxonomy"
#: 分类学召回池候选**未经判分**时的剔除标记（``require_score=True`` 下不可评估）。
#: notes 只按该标记聚合一行，避免「同 L2 成分上百只」把 notes 撑爆。
DROP_TAXONOMY_UNSCORED = "分类学召回池未判分"
#: 分类兜底（残差桶/快照缺失）在 notes/artifact 的标注前缀（设计 §3.1 要求的显式披露）。
TAXONOMY_FALLBACK_NOTE = "分类兜底，未经业务核验"

#: 对标组置信度三项信号（设计 §3.7）与默认权重/阈值。
#: **唯一权威默认处** = ``company_track.peer_quality.confidence``（此处的常量仅供缺段回落）。
CONFIDENCE_SIGNALS: tuple[str, ...] = ("taxonomy_reliable", "judge_direct_ratio", "mean_overlap")
CONFIDENCE_WEIGHTS_DEFAULT: dict[str, float] = {
    "taxonomy_reliable": 0.4,
    "judge_direct_ratio": 0.3,
    "mean_overlap": 0.3,
}
CONFIDENCE_LOW_DEFAULT = 0.4
CONFIDENCE_MEDIUM_DEFAULT = 0.7
#: 「分类学召回池并入候选」的默认上限（= ``build_peer_universe`` 的默认上限，受其约束）。
TAXONOMY_RECALL_LIMIT_DEFAULT = 200


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
    enabled: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, float], dict[str, dict[str, float]]]:
    """确定性质量闸：按序剔除并返回保留者 + 明细 + 分数/维度持久化表。

    剔除规则（命中即 drop，设计 §3.3）：① ``verdict == "reject"`` ② ``product < product_floor``
    ③ ``customer < customer_floor`` ④ 合成 ``overlap < min_overlap``；E 特征只作提示、不参与硬闸。
    ``bypass=True`` 只归一化/记分不剔除（调用方直传的人工白名单）。

    **总开关**（``company_track.peer_quality.enabled``，设计 §3.5/§3.6）：``enabled=False`` ⇒ 本闸
    **不做任何剔除** —— ``dropped`` 恒为空、``kept`` 等于全部输入候选（含 ``verdict="reject"`` 与低分/缺分
    候选），但**分数/维度照常记录**（``scores`` / ``match_dims`` 语义不变，供审计与对标组置信度使用）；
    调用方应记一行 :data:`GATE_DISABLED_NOTE` 作为可观测留痕。该开关**只**门控本闸的剔除，不影响
    消费侧 ``min_peers`` 闸、分类学开关（``taxonomy_enabled``）与置信度三档（各自独立语义）。

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
        if bypass or not enabled:
            # 人工候选白名单 / 质量闸总开关停用：只归一化与记分，不做任何剔除
            pass
        elif verdict == "reject":
            drop = DROP_JUDGE_REJECT
        elif has_dims and dims["product"] < product_floor:
            drop = DROP_PRODUCT_FLOOR
        elif has_dims and dims["customer"] < customer_floor:
            drop = DROP_CUSTOMER_FLOOR
        elif not has_score and require_score:
            # 不可评估（既无 dims 又无自评分）：与 harness 同口径 ⇒ 剔（不做"缺分即保留"）。
            # 分类学召回池并入的候选天然无分（生成器只给自己提出的候选打分）⇒ 用专用标记，
            # 由调用方聚合成一行 notes（否则「同 L2 成分上百只」会把 notes 撑爆）。
            if str(cand.get("source") or "").strip().lower() == TAXONOMY_SOURCE:
                drop = DROP_TAXONOMY_UNSCORED
            else:
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


@dataclass(frozen=True)
class PeerConfidence:
    """对标组置信度（设计 §3.7）：分数 + 三档 + 原始信号 + 合成口径（可复算、可展示）。"""

    score: float
    level: str  # 低 / 中 / 高
    signals: dict[str, float | None]
    weights: dict[str, float]
    low: float
    medium: float

    @property
    def is_low(self) -> bool:
        return self.level == "低"

    def basis(self) -> str:
        """合成口径一行（写入 artifact/review_notes/报告，注明三档与信号取值）。"""
        parts = []
        for name in CONFIDENCE_SIGNALS:
            value = self.signals.get(name)
            parts.append(f"{name}={'缺失' if value is None else f'{value:.2f}'}")
        return f"置信度 {self.level}（score={self.score:.2f}；{'; '.join(parts)}；权重={self.weights}）"

    def note_line(self) -> str:
        """``review_notes``/报告用的一行（含三档与合成口径）。"""
        return f"对标组{self.basis()}"


def synthesize_peer_confidence(
    *,
    taxonomy_reliable: bool | None = None,
    judge_direct_ratio: float | None = None,
    mean_overlap: float | None = None,
    weights: dict[str, float] | None = None,
    low: float = CONFIDENCE_LOW_DEFAULT,
    medium: float = CONFIDENCE_MEDIUM_DEFAULT,
) -> PeerConfidence:
    """确定性合成对标组置信度三档（设计 §3.7；不额外调 LLM）。

    ``score = Σ w_i · signal_i / Σ w_i``：**缺失信号按 0 处理**（``None`` ⇒ 0），
    权重和不为 1 时按权重和归一并**如实登记**（避免越界）；档位
    ``< low → 低``、``< medium → 中``、否则 ``高``。同输入同输出。

    **D 项口径（`judge_direct_ratio`）**：`judge_enabled=false`（当前默认）或 judge 不可用时，D 项按 **0** 计入 ⇒ 档位只反映 E（`taxonomy_reliable`）与 C（`mean_overlap`）两路信号；实现上即
    ``judge_direct_ratio=None`` ⇒ 该路贡献恰为 0（**不得**伪造成 1 或 0.5 之类"有 judge"的信号，
    也不得把该档位读作「judge 判过但都不是 direct」）。调用方有真实占比时应显式传入
    （离线复算即如此），此时三路齐备。
    """
    active_weights = {
        name: float((weights or CONFIDENCE_WEIGHTS_DEFAULT).get(name, 0.0)) for name in CONFIDENCE_SIGNALS
    }
    ratio = _coerce_ratio(judge_direct_ratio)
    overlap = _coerce_ratio(mean_overlap)
    signals: dict[str, float | None] = {
        "taxonomy_reliable": None if taxonomy_reliable is None else (1.0 if taxonomy_reliable else 0.0),
        "judge_direct_ratio": ratio,
        "mean_overlap": overlap,
    }
    total_weight = sum(active_weights.values())
    raw = sum(active_weights[name] * (signals[name] or 0.0) for name in CONFIDENCE_SIGNALS)
    score = (raw / total_weight) if total_weight > 0 else 0.0
    score = max(0.0, min(1.0, score))
    if score < float(low):
        level = "低"
    elif score < float(medium):
        level = "中"
    else:
        level = "高"
    return PeerConfidence(
        score=round(score, 4),
        level=level,
        signals=signals,
        weights=active_weights,
        low=float(low),
        medium=float(medium),
    )


def _coerce_ratio(value: float | None) -> float | None:
    """占比信号归一到 ``[0, 1]``；``None``/非法 ⇒ ``None``（缺失，按 0 参与合成）。"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return max(0.0, min(1.0, number))


def peer_confidence_from_group(
    group: PeerGroup,
    *,
    taxonomy_reliable: bool | None = None,
    judge_direct_ratio: float | None = None,
    settings: dict[str, Any] | None = None,
) -> PeerConfidence:
    """由已落盘的对标组复算置信度（report/节点侧入口）。

    ``mean_overlap`` 取 ``PeerGroup.scores`` 均值（只有**有分数**候选才落值 ⇒ 空表 = 缺失）；
    权重/阈值取 ``company_track.peer_quality.confidence``（缺段回落默认）。

    **D 项口径（`judge_direct_ratio`）**：`judge_enabled=false`（当前默认）或 judge 不可用时，D 项按 **0** 计入 ⇒ 档位只反映 E（`taxonomy_reliable`）与 C（`mean_overlap`）两路信号。
    """
    cfg = settings if settings is not None else _peer_quality_settings()
    confidence_cfg = cfg.get("confidence") or {}
    weights = confidence_cfg.get("weights")
    low = float(confidence_cfg.get("low", CONFIDENCE_LOW_DEFAULT))
    medium = float(confidence_cfg.get("medium", CONFIDENCE_MEDIUM_DEFAULT))
    scores = [float(value) for value in (group.scores or {}).values()]
    mean_overlap = (sum(scores) / len(scores)) if scores else None
    return synthesize_peer_confidence(
        taxonomy_reliable=taxonomy_reliable,
        judge_direct_ratio=judge_direct_ratio,
        mean_overlap=mean_overlap,
        weights=weights,
        low=low,
        medium=medium,
    )


def _taxonomy_config(gate_cfg: dict[str, Any]) -> dict[str, Any]:
    """分类学（判定 E）运行参数：开关 + 残差桶成分下限 + 召回池上限（缺段一律回落默认）。"""
    return {
        "enabled": bool(gate_cfg.get("taxonomy_enabled", True)),
        "min_constituents": int(gate_cfg.get("residual_l3_min_constituents", peer_taxonomy.RESIDUAL_L3_MIN_FALLBACK)),
        "recall_limit": int(TAXONOMY_RECALL_LIMIT_DEFAULT),
    }


def _merge_taxonomy_recall_pool(
    symbol: str,
    candidates: list[dict[str, Any]],
    warnings: list[str],
    *,
    min_constituents: int,
    recall_limit: int,
    reliable: bool,
) -> list[dict[str, Any]]:
    """E 的**召回池**（设计 §3.1）：并入同 L3 成分（残差桶 ⇒ 同 L2），保证真对标进入候选。

    纪律：只**并入候选池**、绝不作判定依据；去重、排除标的自身、保序稳定（生成器候选在前、
    分类学成分按快照顺序在后），并受 ``build_peer_universe`` 的上限约束。
    并入项 ``source=taxonomy`` 且**未经打分** ⇒ 由 Gate 按不可评估处置（judge 判分后才可能被采纳）。
    """
    pool_codes, level = peer_taxonomy.recall_pool(symbol, min_constituents=min_constituents)
    if not pool_codes or level == "none":
        if not reliable:
            warnings.append(f"{TAXONOMY_FALLBACK_NOTE}：无可用的同 L2 成分（召回池为空）")
        return candidates
    universe = build_peer_universe(list(pool_codes), exclude=symbol, limit=recall_limit)
    existing = {str(item.get("code") or "").strip().upper() for item in candidates}
    level_label = "L3" if level == "l3" else "L2"
    additions = [
        {
            "name": str(item.get("name") or ""),
            "code": str(item.get("code") or ""),
            "reason": f"同 {level_label} 分类学成分（召回池，{TAXONOMY_FALLBACK_NOTE}）",
            "source": TAXONOMY_SOURCE,
        }
        for item in universe
        if str(item.get("code") or "").strip().upper() not in existing
    ]
    if not additions:
        return candidates
    warnings.append(
        f"分类学召回池并入 {len(additions)} 只同 {level_label} 成分"
        f"（去重/排除自身/保序，上限 {recall_limit}）；并入项未判分，须独立评审生效才可能进入采纳面"
    )
    return list(candidates) + additions


def _annotate_taxonomy(
    symbol: str,
    candidates: list[dict[str, Any]],
    *,
    reliable: bool,
) -> list[dict[str, Any]]:
    """E 的**特征**（设计 §3.1）：逐候选挂 ``same_l3`` / ``same_l2``（未知 ⇒ ``None``）。

    残差桶（``reliable=False``）⇒ **特征降权**：``same_l3`` 一律 ``None``（不注入），只保留 ``same_l2``。
    """
    return peer_taxonomy.annotate_same_levels(symbol, candidates, l3_reliable=reliable)


def peer_confidence_for_group(
    symbol: str,
    group: PeerGroup,
    *,
    judge_direct_ratio: float | None = None,
    settings: dict[str, Any] | None = None,
) -> PeerConfidence:
    """按 symbol + 已落盘对标组复算置信度（**节点/报告侧单一入口**，设计 §3.7）。

    - ``taxonomy_reliable``：由静态快照现算（``taxonomy_enabled=false`` ⇒ 缺失 ⇒ 按 0 参与合成）；
    - ``mean_overlap``：``PeerGroup.scores`` 均值（无分数候选 ⇒ 缺失）；
    - ``judge_direct_ratio``：判定 D 的 direct 占比。**持久化层无该数据源**（``PeerGroup`` 只落
      ``scores``/``match_dims``，未存 verdict）⇒ 默认缺失（按 0）；需要时由调用方显式传入。

    **D 项口径（`judge_direct_ratio`）**：`judge_enabled=false`（当前默认）或 judge 不可用时，D 项按 **0** 计入 ⇒ 档位只反映 E（`taxonomy_reliable`）与 C（`mean_overlap`）两路信号。
    """
    cfg = settings if settings is not None else _peer_quality_settings()
    taxonomy_cfg = _taxonomy_config(cfg)
    taxonomy_reliable: bool | None = None
    if taxonomy_cfg["enabled"]:
        taxonomy_reliable = peer_taxonomy.assess_reliability(
            symbol, min_constituents=taxonomy_cfg["min_constituents"]
        ).reliable
    return peer_confidence_from_group(
        group,
        taxonomy_reliable=taxonomy_reliable,
        judge_direct_ratio=judge_direct_ratio,
        settings=cfg,
    )


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


def _judge_pool(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """候选 → judge 入参池（``{code, name}`` + E 特征 ``same_l3`` / ``same_l2``）。

    E 特征只在**有值**时注入（``None`` = 无分类学数据/未知 ⇒ 不注入，judge prompt 忽略该特征，
    设计 §3.6）；特征取自 :func:`_annotate_taxonomy`（残差桶时 ``same_l3`` 一律 ``None`` = 降权）。
    """
    pool: list[dict[str, Any]] = []
    for item in candidates:
        entry: dict[str, Any] = {"code": str(item.get("code") or ""), "name": str(item.get("name") or "")}
        for key in ("same_l3", "same_l2"):
            if item.get(key) is not None:
                entry[key] = bool(item[key])
        pool.append(entry)
    return pool


def _with_judge_verdict(candidates: list[dict[str, Any]], results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """把 judge 的 ``verdict`` / ``dims`` 挂到候选上（**判定 D 生效口径**）。

    只有 ``JudgeReport.ok`` 时才调用本函数（全覆盖：池内每只都有可用判定）；
    生成的候选 dict 供 :func:`gate_candidates` 使用 —— Gate 的规则①消费 ``verdict``、
    合成分消费 ``dims``，从而把「生成者自评」换成「独立评审的维度」。
    """
    merged: list[dict[str, Any]] = []
    for cand in candidates:
        code = str(cand.get("code") or "").strip().upper()
        row = results.get(code)
        if not row:
            merged.append(cand)
            continue
        # ``verdict`` 供 Gate 规则①（先否决）；``dims`` 换成 judge 的四维分（合成分 → scores/match_dims）
        merged.append({**cand, "verdict": row.get("verdict"), "dims": row.get("dims")})
    return merged


def _run_judge(
    candidates: list[dict[str, Any]],
    business_description: str,
    warnings: list[str],
    *,
    batch_size: int = peer_judge.JUDGE_BATCH_SIZE_DEFAULT,
    model: Any = None,
) -> tuple[list[dict[str, Any]], bool, bool]:
    """独立批量评审（判定 D）：返回 ``(候选, judge_ok, judge_called)``。

    - ``judge_ok``：judge 有效响应且**池内全覆盖** ⇒ 候选挂上 judge 的 ``verdict`` / ``dims``；
    - ``judge_called``：是否真的发起了判定（空候选池 / 空业务描述 ⇒ ``False``，此时既未判定
      也不算降级，``no_peers`` 判据按生成器口径处理）。

    fail-open（设计 §3.6）：judge 调用失败 / 超时 / 非 JSON / 漏判 ⇒ **不静默采纳生成分**：
    回退生成器的 ``dims`` / ``overlap`` 继续走 Gate，notes 记 ``judge_degraded``，
    **绝不**据此置 ``no_peers``（可重试）。
    """
    if not candidates or not str(business_description or "").strip():
        return candidates, False, False
    report = peer_judge.judge_peer_candidates_batched(
        business_description, _judge_pool(candidates), batch_size=batch_size, model=model
    )
    if not report.ok:
        note = report.note()
        if note:
            warnings.append(note)
        return candidates, False, report.called
    return _with_judge_verdict(candidates, report.results), True, True


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
            # LLM 有效响应且无候选 ⇒ 判定「确无 A 股直接对标」终态，避免每次分析重复调用 LLM。
            # 判定 D 的 judge 面**不参与**本条：生成器零候选 ⇒ 无候选池可判（``called=False``），
            # 故终态判据沿用生成器口径（judge 的「双有效」口径见下方 Gate 清空分支）。
            no_peers = bool(use_llm and meta.get("llm_ok"))
            group = PeerGroup(symbol=symbol, name=name, source="manual", notes=list(warnings), no_peers=no_peers)
            store.save(group)
            return group, warnings

    # 来源侧（LLM 打分阶段）已剔除的候选也入 notes：与 Gate 的剔除面不同，两者都要可审计
    if candidates:
        _extend_unique(warnings, _format_dropped(meta.get("dropped") or []))

    gate_cfg = _peer_quality_settings()

    # ── E（分类学）：召回池并入 + 特征注入（设计 §3.1/§3.3/§6 Step 3；**绝不硬闸**） ──
    # 召回池只对**生成器推断路径**并入（设计 §3.1 明文：business_description 路径额外并入同 L3 成分）。
    # 特征（same_l3/same_l2 + 残差桶降权）注入给 judge prompt，供其业务判断参考，不作 Gate 条件。
    taxonomy_cfg = _taxonomy_config(gate_cfg)
    taxonomy_reliable: bool | None = None
    if taxonomy_cfg["enabled"] and candidates:
        reliability = peer_taxonomy.assess_reliability(symbol, min_constituents=taxonomy_cfg["min_constituents"])
        taxonomy_reliable = reliability.reliable
        if not reliability.reliable:
            # 不可信（残差桶名 / 成分不足 / 快照缺失）⇒ 显式标注 + 召回降级 L2 + 特征降权
            warnings.append(
                f"{TAXONOMY_FALLBACK_NOTE}：{reliability.reason}；召回改用 "
                f"{'L2' if reliability.recall_level == 'l2' else '无'}、特征降权（same_l3 不注入）"
            )
        if infer_used:
            candidates = _merge_taxonomy_recall_pool(
                symbol,
                list(candidates),
                warnings,
                min_constituents=taxonomy_cfg["min_constituents"],
                recall_limit=taxonomy_cfg["recall_limit"],
                reliable=bool(taxonomy_reliable),
            )
        candidates = _annotate_taxonomy(symbol, list(candidates), reliable=bool(taxonomy_reliable))

    # ── D（判定）：独立 batch judge（生成/判定解耦，贯穿「生成器出分 → judge 复判 → Gate 采纳」） ──
    # 只对**生成器推断路径**（business_description ⇒ infer_peer_candidates）接线：judge 的输入是
    # 「标的业务描述 + 候选池（生成器候选 ∪ 分类学召回池，闭集）+ E 特征」，与生成器同模型同组件
    # （设计 §8 决策 1，无第二模型项）。
    # judge 降级 ⇒ 回退生成器 dims/overlap（判定 C 口径），notes 记 judge_degraded（见 :func:`_run_judge`）。
    judge_called = False
    judge_ok = False
    if infer_used and candidates and bool(gate_cfg.get("judge_enabled", True)):
        candidates, judge_ok, judge_called = _run_judge(
            list(candidates),
            business_description or "",
            warnings,
            batch_size=int(gate_cfg.get("judge_batch_size", peer_judge.JUDGE_BATCH_SIZE_DEFAULT)),
        )

    # ── C（判定）：确定性质量闸（权重/阈值唯一处 = peer_judge） ──
    # 只对 **LLM 产出** 的候选做剔除：``candidates`` 直传是调用方（人工/分析师）给定的白名单，
    # 按来源优先级最高、不经质量闸（否则会反向覆盖人工判断）。
    # 判定 C 打分来源（LLM 推断）⇒ 严格口径（缺分即不可评估，与 harness 同源）；
    # 无分数来源（研报片段抽取 / 闭集择优 / 调用方直传）⇒ require_score=False（保持既有行为）
    scored_source = infer_used
    # 质量闸总开关（``company_track.peer_quality.enabled``，设计 §3.5）：缺段/异常 ⇒ 默认 true（行为不变）；
    # ``false`` ⇒ 闸门不做任何剔除，并在 notes 留一行可观测留痕（「没剔除明细」与「本来就没被剔」必须可区分）。
    gate_enabled = bool(gate_cfg.get("enabled", True))
    if not gate_enabled:
        warnings.append(GATE_DISABLED_NOTE)
    gate_kwargs: dict[str, Any] = {
        "enabled": gate_enabled,
        "require_score": scored_source,
        "weights": gate_cfg.get("weights"),
        "min_overlap": float(gate_cfg.get("min_overlap", DEFAULT_MIN_OVERLAP)),
        "product_floor": float(gate_cfg.get("product_floor", DEFAULT_PRODUCT_FLOOR)),
        "customer_floor": float(gate_cfg.get("customer_floor", DEFAULT_CUSTOMER_FLOOR)),
    }
    if llm_used:
        candidates, gate_dropped, scores, match_dims = gate_candidates(list(candidates), **gate_kwargs)
        # 分类学召回池并入项**未判分**的剔除：聚合一行（同 L2 成分可达上百只，逐条会撑爆 notes）
        taxo_drops = [item for item in gate_dropped if item.get("drop") == DROP_TAXONOMY_UNSCORED]
        other_drops = [item for item in gate_dropped if item.get("drop") != DROP_TAXONOMY_UNSCORED]
        _extend_unique(warnings, _format_dropped(other_drops))
        if taxo_drops:
            warnings.append(
                f"分类学召回池 {len(taxo_drops)} 只未经判分，未进入采纳面"
                f"（仅作召回口径；独立评审生效并判分后才可能纳入）"
            )
    else:
        # 人工候选：仍记录分数/维度（若调用方给了），但不剔除
        candidates, _manual_dropped, scores, match_dims = gate_candidates(list(candidates), bypass=True, **gate_kwargs)
    if not candidates:
        warnings.append("质量闸后无保留候选（不编造），空对标组")
        # 设计 §3.6「全空 ⇒ ``no_peers = 生成器与 judge 均有效响应且无保留``」：Gate 把候选
        # 全部剔除与「生成器零候选」同属**终态空组**，必须同样置位，否则每次分析都会重复
        # 调用 LLM 走在线兜底。判定 D 接通后：judge 面（生成器推断路径 + judge_enabled）要求
        # **judge 也有效响应**；judge 未接线（非推断路径 / 配置关闭 / 空池）则沿用生成器口径。
        no_peers = bool(use_llm and meta.get("llm_ok") and (judge_ok if judge_called else True))
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
