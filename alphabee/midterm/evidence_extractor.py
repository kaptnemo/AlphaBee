"""evidence_extractor 两阶段抽取（E3，设计 MIDTERM_EVIDENCE_EXTRACTION.md §4）。

- **Stage A**（§4.1/§4.2）：把非结构化文本（财报/预告/研报/公告/新闻窗口）抽取为
  客观事实 :class:`FactEvent`（只问「发生了什么」，不问「好不好」）。
- **Stage B**（§4.3）：给定 Thesis H，判定每个事实对 H 的方向与强度
  （:class:`EvidenceJudgment`），再组装为 :class:`EvidenceEvent`。

LLM 边界（§11）：

- Stage A / Stage B **LLM 必需**：切分事件、抽取事实与方向判定；
- **失败降级**：Stage A 失败 → 返回 ``[]``（该文本跳过）；Stage B 失败或 thesis 为空
  → 数值类按符号定方向、定性一律 neutral（§8/§11 显式标记）；绝不向上抛异常打断调用方。

防幻觉三道闸之「原文引用闸」（§5）：Stage A 强制 ``quotes`` 逐字引用；Stage B 强制
``reasoning`` 引用事实原文；``confidence_delta`` 只允许离散等级（weak/medium/strong →
0.1/0.3/0.5，上限 0.7），禁止 LLM 连续值。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from alphabee.midterm.models import (
    STRENGTH_DELTA,
    EffectOnThesis,
    EvidenceEvent,
    EvidenceJudgment,
    FactEvent,
    Strength,
)
from alphabee.utils.pipeline import parse_json
from alphabee.utils.prompts import json_instruction

_COMPONENT = "evidence_extractor.stage_a"
_COMPONENT_B = "evidence_extractor.stage_b"


class FactEventList(BaseModel):
    """Stage A LLM 输出契约：客观事实事件列表（``json_instruction`` 的 few-shot 来源）。

    顶层必须是 JSON 对象（``{"events": [...]}``），以匹配 ``response_format=json_object``
    的容器约束；``id`` / ``source_type`` 由系统回填，LLM 输出时留空即可。
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "events": [
                    {
                        "id": "",
                        "date": "2026-06-30",
                        "kind": "expectation",
                        "description": "公司发布业绩预告，预计净利润同比增长 50%",
                        "numbers": {"profit_forecast_min_change": 50.0},
                        "quotes": ["预计净利润同比增长 50%"],
                        "source_refs": [],
                        "source_type": "",
                    }
                ]
            }
        }
    )

    events: list[FactEvent] = Field(default_factory=list)


_STAGE_A_SYSTEM_PROMPT = """你是客观事实抽取器（Stage A）。只报告「发生了什么」，不评价好坏、不预测、不补全。

铁律（防幻觉）：
1. 每个事件必须附 quotes（原文引用句，逐字来自待抽取文本，用于支撑 description）；
2. date 填事件发生日（YYYY-MM-DD），原文没有就填空字符串；
3. kind 只用受控词表：fundamental / expectation / trend / crowding / thesis / price；
4. numbers 只抄原文出现的数字（canonical 字段名 → 数值），原文没有的字段置 null，绝不补全或推算；
5. 禁止评价（好/坏/强/弱）、禁止预测、禁止补全；与文本不相关时输出 events: []，不得编造；
6. id 与 source_type 由系统回填，输出时填空字符串即可。
"""


# ─────────────────────────────────────────────────────────────────────────────
# 基础纯函数
# ─────────────────────────────────────────────────────────────────────────────


def _normalize_texts(texts: str | list[str]) -> str:
    """把单个文本 / 文本窗口列表归一为一段文本（多窗口用分隔线拼接）。"""
    if isinstance(texts, str):
        return texts
    return "\n\n---\n\n".join(str(t) for t in (texts or []))


def _extract_content(raw: Any) -> str:
    """从 LLM 返回对象中提取文本（兼容 AIMessage / str / dict / content blocks）。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    content = getattr(raw, "content", None)
    if content is None and isinstance(raw, dict):
        content = raw.get("content") or raw.get("text") or ""
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
        return "".join(parts)
    return str(content)


def _fmt_num(v: float | None) -> str:
    return "None" if v is None else f"{v:g}"


def _fact_id(date: str, kind: str, symbol: str, description: str, numbers: dict[str, float | None]) -> str:
    """事件签名哈希（§7）：``hash(date+kind+主体+数值)``，主体=股票+描述，数值=canonical 数值。"""
    nums = ",".join(f"{k}={_fmt_num(v)}" for k, v in sorted(numbers.items()))
    raw = f"{date}|{kind}|{symbol}|{description}|{nums}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _dedup(events: list[FactEvent]) -> list[FactEvent]:
    """按 ``id`` 去重（§7）：同 id 合并 quotes/source_refs，同主题只算一次。"""
    merged: dict[str, FactEvent] = {}
    for ev in events:
        if ev.id in merged:
            prev = merged[ev.id]
            quotes = sorted(set(prev.quotes) | set(ev.quotes))
            refs = sorted(set(prev.source_refs) | set(ev.source_refs))
            merged[ev.id] = prev.model_copy(update={"quotes": quotes, "source_refs": refs})
        else:
            merged[ev.id] = ev
    return list(merged.values())


def _finalize(draft: FactEvent, symbol: str, source_type: str) -> FactEvent | None:
    """回填确定性 id / source_type，丢弃无效事实（无 description 防幻觉）。"""
    description = (draft.description or "").strip()
    if not description:
        return None  # 无描述不出数（§3 铁律）
    return FactEvent(
        id=_fact_id(draft.date, draft.kind, symbol, description, draft.numbers),
        date=draft.date,
        kind=draft.kind,
        description=description,
        numbers=draft.numbers,
        quotes=draft.quotes,
        source_refs=draft.source_refs,
        source_type=source_type or draft.source_type or "",
    )


def _build_messages(text: str) -> list[Any]:
    """构造 Stage A 的 messages：系统提示（客观+防幻觉）+ 文本 + 输出格式指令。"""
    return [
        SystemMessage(content=_STAGE_A_SYSTEM_PROMPT),
        HumanMessage(content=f"待抽取文本：\n\n{text}\n\n{json_instruction(FactEventList)}"),
    ]


def _build_model(component: str = _COMPONENT) -> Any:
    """复用已有 LLM 实例（§9）：``create_chat_model`` + json_object 容器约束。"""
    from alphabee.utils.llm import create_structured_model

    return create_structured_model(component)


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────


def extract_facts(
    texts: str | list[str],
    *,
    symbol: str = "",
    source_type: str = "",
    model: Any = None,
) -> list[FactEvent]:
    """Stage A 客观事实抽取：非结构化文本 → FactEvent[]（LLM 必需，失败降级 []）。

    Args:
        texts: 单个文本字符串或文本窗口列表（财报/预告/研报/公告/新闻正文）。
        symbol: 股票代码（用于事件签名主体）。
        source_type: 来源类型提示（financial_report / forecast / research_report /
            announcement / news），回填到 FactEvent.source_type。
        model: 可选注入的 LLM 实例（测试用）；缺省复用 ``create_structured_model``。

    Returns:
        FactEvent[]（去重后；LLM 失败 / 解析失败 / 结构校验失败均返回 ``[]``，
        绝不抛异常中断调用方）。
    """
    text = _normalize_texts(texts)
    if not text.strip():
        return []

    llm = model if model is not None else _build_model()
    try:
        raw = llm.invoke(_build_messages(text))
        parsed = parse_json(_extract_content(raw))
        if isinstance(parsed, list):
            # 兼容 LLM 直接输出数组的情况
            parsed = {"events": parsed}
        flist = FactEventList.model_validate(parsed)
    except Exception:
        # LLM 挂掉 / 输出坏 JSON / 字段校验失败 → 该文本跳过，返回 []（§11 降级不中断）
        return []

    facts = [fact for draft in flist.events if (fact := _finalize(draft, symbol, source_type)) is not None]
    return _dedup(facts)


# ─────────────────────────────────────────────────────────────────────────────
# Stage B：方向判定 + 组装（E3-2，设计 §4.3）
# ─────────────────────────────────────────────────────────────────────────────


class EvidenceJudgmentList(BaseModel):
    """Stage B LLM 输出契约：方向判定列表（``json_instruction`` 的 few-shot 来源）。"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "judgments": [
                    {
                        "fact_id": "f1",
                        "effect_on_thesis": "confirming",
                        "strength": "medium",
                        "reasoning": "营收同比增长与 H 的增长假设一致（引用原文「营收同比增长 10%」）",
                    }
                ]
            }
        }
    )

    judgments: list[EvidenceJudgment] = Field(default_factory=list)


_STAGE_B_SYSTEM_PROMPT = """你是方向判定器（Stage B）。给定 Thesis H（含 invalidation 条件），判定每个事实事件对 H 是证实（confirming）/ 证伪（refuting）/ 无关（neutral）。

铁律（防幻觉）：
1. effect_on_thesis 只允许 confirming / refuting / neutral；
2. strength 只允许 weak / medium / strong 三档，禁止输出任何数字；
3. reasoning 必须引用事实的 quotes（原文引用句），不得脱离原文编造；
4. 逐条对应输入事实，fact_id 必须与输入事实的 id 一致；
5. Thesis 为空时：数值类按符号定方向、定性一律 neutral（显式标记）。
"""


def _first_number(fact: FactEvent) -> float | None:
    """返回事实的第一个非空数值（按键名排序，确定性）；无数值 → None。"""
    for key in sorted(fact.numbers):
        value = fact.numbers[key]
        if value is not None:
            return value
    return None


def _effect_from_sign(value: float) -> EffectOnThesis:
    """数值符号 → 方向（§8 thesis 为空退化）：>0 confirming / <0 refuting / ==0 neutral。"""
    if value > 0.0:
        return EffectOnThesis.CONFIRMING
    if value < 0.0:
        return EffectOnThesis.REFUTING
    return EffectOnThesis.NEUTRAL


def _degrade_single_judgment(fact: FactEvent, reason: str) -> EvidenceJudgment:
    """单条退化（§8/§11）：数值类按符号定方向、定性 neutral，显式标记。"""
    value = _first_number(fact)
    if value is not None:
        effect = _effect_from_sign(value)
        reasoning = f"{reason}：数值类按符号定方向（{_fmt_num(value)} → {effect.value}）"
    else:
        effect = EffectOnThesis.NEUTRAL
        reasoning = f"{reason}：定性事实一律 neutral"
    return EvidenceJudgment(
        fact_id=fact.id,
        effect_on_thesis=effect,
        strength=Strength.WEAK,  # 保守默认（§11）
        reasoning=reasoning,
    )


def _degrade_judgments(facts: list[FactEvent], reason: str) -> list[EvidenceJudgment]:
    """整批退化：thesis 为空 / LLM 失败 → 数值按符号、定性 neutral。"""
    return [_degrade_single_judgment(fact, reason) for fact in facts]


def _strength_to_delta(strength: Strength) -> float:
    """离散等级 → confidence_delta（唯一映射，禁连续值）。"""
    return STRENGTH_DELTA[strength.value]


def _build_stage_b_messages(facts: list[FactEvent], thesis: str) -> list[Any]:
    """构造 Stage B messages：系统提示 + Thesis + 事实列表 + 输出格式指令。"""
    facts_json = json.dumps([f.model_dump(mode="json") for f in facts], ensure_ascii=False, indent=2)
    user = (
        f"## Thesis H（含 invalidation 条件）\n{thesis}\n\n"
        f"## 待判定事实\n{facts_json}\n\n{json_instruction(EvidenceJudgmentList)}"
    )
    return [SystemMessage(content=_STAGE_B_SYSTEM_PROMPT), HumanMessage(content=user)]


def _dedup_events(events: list[EvidenceEvent]) -> list[EvidenceEvent]:
    """按 id 去重（§7）：同 id 合并 source_refs。"""
    merged: dict[str, EvidenceEvent] = {}
    for ev in events:
        if ev.id in merged:
            prev = merged[ev.id]
            refs = sorted(set(prev.source_refs) | set(ev.source_refs))
            merged[ev.id] = prev.model_copy(update={"source_refs": refs})
        else:
            merged[ev.id] = ev
    return list(merged.values())


def judge_facts(
    facts: list[FactEvent],
    thesis: str = "",
    *,
    model: Any = None,
) -> list[EvidenceJudgment]:
    """Stage B 方向判定：FactEvent + Thesis H → EvidenceJudgment[]（LLM 必需，失败降级）。

    - Thesis 非空：调 LLM 判定每条事实对 H 的方向与离散强度；LLM 失败 → 退化
      （数值按符号、定性 neutral）；
    - Thesis 为空：直接退化（不调 LLM），数值按符号、定性 neutral（§8 显式标记）。

    Returns:
        与输入 facts 一一对应的 EvidenceJudgment[]。
    """
    facts = list(facts or [])
    if not facts:
        return []

    thesis_text = (thesis or "").strip()
    if not thesis_text:
        return _degrade_judgments(facts, "thesis 为空")

    llm = model if model is not None else _build_model(_COMPONENT_B)
    try:
        raw = llm.invoke(_build_stage_b_messages(facts, thesis_text))
        parsed = parse_json(_extract_content(raw))
        if isinstance(parsed, list):
            parsed = {"judgments": parsed}
        jlist = EvidenceJudgmentList.model_validate(parsed)
    except Exception:
        return _degrade_judgments(facts, "LLM 失败")

    by_fact_id = {j.fact_id: j for j in jlist.judgments}
    return [by_fact_id.get(fact.id) or _degrade_single_judgment(fact, "LLM 未返回该事实判定") for fact in facts]


def assemble_events(
    facts: list[FactEvent],
    judgments: list[EvidenceJudgment],
) -> list[EvidenceEvent]:
    """把 FactEvent + EvidenceJudgment 组装为 EvidenceEvent[]（§4.3 → §3）。

    - ``id`` 复用 ``FactEvent.id``（事件签名哈希，§7）；
    - ``strength`` → ``confidence_delta`` 离散映射（weak 0.1 / medium 0.3 / strong 0.5，
      上限 0.7），neutral → 0.0（bayes no-op）；
    - ``source_refs`` = ``fact.quotes`` + ``fact.source_refs``（原文引用句 + 来源 URL，§3）。

    Returns:
        去重后的 EvidenceEvent[]；无对应事实的判定丢弃。
    """
    fact_by_id = {f.id: f for f in facts or []}
    events: list[EvidenceEvent] = []
    for judgment in judgments or []:
        fact = fact_by_id.get(judgment.fact_id)
        if fact is None:
            continue
        delta = (
            _strength_to_delta(judgment.strength) if judgment.effect_on_thesis is not EffectOnThesis.NEUTRAL else 0.0
        )
        events.append(
            EvidenceEvent(
                id=fact.id,
                date=fact.date,
                kind=fact.kind,
                description=fact.description,
                effect_on_thesis=judgment.effect_on_thesis,
                confidence_delta=delta,
                source_refs=[*fact.quotes, *fact.source_refs],
            )
        )
    return _dedup_events(events)
