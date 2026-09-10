"""evidence_extractor Stage A：客观事实抽取（E3-1，设计 MIDTERM_EVIDENCE_EXTRACTION.md §4.1/§4.2）。

两阶段抽取的第一阶段（Stage A）：把非结构化文本（财报/预告/研报/公告/新闻窗口）
抽取为客观事实 :class:`FactEvent`（只问「发生了什么」，不问「好不好」），供 Stage B
（相对 thesis 的方向判定）消费。本模块只实现 Stage A。

LLM 边界（§11）：

- Stage A **LLM 必需**：切分事件、抽取客观事实与原文引用；
- **失败降级**：LLM 挂掉 / 输出坏结构 / 解析失败 → 返回 ``[]``（该文本跳过，不中断），
  绝不向上抛异常打断调用方。

防幻觉三道闸之「原文引用闸」（§5）在 Stage A 的落地：prompt 强制每个事件附
``quotes``（逐字原文引用句）支撑 ``description``；数值只抄原文出现的数字、缺失置
``None``；不相关文本输出空列表不得编造。``id`` 由确定性规则（§7 事件签名哈希）在
抽取后回填，LLM 不参与哈希计算。
"""

from __future__ import annotations

import hashlib
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from alphabee.midterm.models import FactEvent
from alphabee.utils.pipeline import parse_json
from alphabee.utils.prompts import json_instruction

_COMPONENT = "evidence_extractor.stage_a"


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


def _build_model() -> Any:
    """复用已有 LLM 实例（§9）：``create_chat_model`` + json_object 容器约束。"""
    from alphabee.utils.llm import create_structured_model

    return create_structured_model(_COMPONENT)


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
