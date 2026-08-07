"""Pipeline utilities shared across orchestrator, harness, and agents.

Provides three low-level helpers that were previously duplicated in every
module that interacts with LLM output:

- ``extract_text``  — normalise LangChain message content to a plain string
- ``parse_json``    — parse a JSON payload from an LLM response, handling
                      markdown fences and partial wrapping robustly
- ``make_id``       — generate a short ``prefix-<hex12>`` ID
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

try:
    from json_repair import repair_json as _repair_json

    _JSON_REPAIR_AVAILABLE = True
except ImportError:
    _JSON_REPAIR_AVAILABLE = False


def extract_text(content: Any) -> str:
    """Normalise LangChain / OpenAI message content to a plain string.

    Handles the three shapes that appear in practice:

    - ``str``         — returned as-is
    - ``list``        — concatenates ``str`` blocks and ``{"type": "text"|"thinking"}``
                        dict blocks, separated by newlines
    - anything else   — coerced via ``str()``
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in {"text", "thinking"}:
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return str(content)


# =============================================================================
# _extract_balanced_json_candidates — 平衡括号匹配提取
# =============================================================================
#
# 业务背景：
#   LLM（尤其是 deepseek-v4）在有工具的 agent 中，经常在最终回复中输出
#   "分析报告 + 嵌入的 JSON" 的混合内容。典型违规输出：
#
#       基于对英维克的财务数据综合核查，三个假设的验证结果如下：
#
#       ## 核心发现
#       ### conflict_5_h1 → partial
#       ... 大段分析文字 ...
#
#       {"results": [{"id": "v1", "status": "verified", ...}]}
#
#       以上分析仅供参考。
#
#   parse_json 的策略 3（"第一个 { 到最后一个 }"）对这种输出有两种失败模式：
#   1. 如果分析文字中不含 {，则策略 3 等价于取整个文本（失效）；
#   2. 如果分析文字中包含 {（如 Markdown 链接、代码片段），策略 3 会捕获
#      过多的非 JSON 内容，导致 json.loads 失败。
#
#   本函数用字符级状态机精确匹配每一个平衡的顶层 {...} 块，作为策略 3 的
#   增强替代（策略 3b），按长度降序排列让 parse_json 优先尝试最完整的候选。
#
# 状态机设计（三个标志位）：
#   - depth:      括号嵌套深度。{ 时 +1，} 时 -1。depth==0 时遇到的 { 是
#                 顶层 JSON 对象的开始；depth 回到 0 时遇到的 } 是其闭合。
#   - in_string:  是否在双引号字符串字面量内部。在字符串内时，{ 和 } 不参与
#                 括号匹配，避免字符串内容干扰结构判断。
#   - escape:     上一个字符是否是反斜杠。处理 \\"（转义引号）等场景，
#                 确保 \" 不被误判为字符串边界的切换。
#
# 为什么按长度降序排列？
#   最长的平衡块最可能是完整的 JSON 对象。分析文字中也可能出现孤立的
#   花括号片段（如中文语境下的"置信度{high}"），这些片段较短且不是合法
#   JSON。优先尝试最长的候选可以减少不必要的 json.loads 失败。
#


def _extract_balanced_json_candidates(text: str, candidates: list[str]) -> None:
    """Extract balanced ``{…}`` substrings as additional parse candidates.

    When the model outputs analysis text before/after the JSON payload,
    strategy 3's "first ``{`` to last ``}``" may capture too much non-JSON
    content.  This helper walks the string and emits every balanced
    top-level ``{…}`` block, ordered longest-first so the most complete
    candidate is tried first.
    """
    # 快速守卫：如果文本中根本没有 {，直接返回避免无意义扫描
    if "{" not in text:
        return

    brace_starts: list[int] = []  # 顶层 { 的起始位置栈
    brace_pairs: list[tuple[int, int]] = []  # 收集到的 (start, end) 对
    depth = 0  # 当前括号嵌套深度
    in_string = False  # 当前是否在双引号字符串内部
    escape = False  # 上一个字符是否是反斜杠

    for i, ch in enumerate(text):
        # 处理转义：\ 后的字符失去特殊含义（\" 不作为字符串边界）
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue

        # 双引号切换字符串内/外状态
        # 注意：只处理双引号——JSON 标准不允许单引号字符串
        if ch == '"' and not escape:
            in_string = not in_string
            continue

        # 在字符串内部时，{ 和 } 是普通字符，不参与结构匹配
        if in_string:
            continue

        # { → 深度+1；若此前 depth=0，这是一个顶层 JSON 对象的开始
        if ch == "{":
            if depth == 0:
                brace_starts.append(i)
            depth += 1

        # } → 深度-1；若此后 depth=0，闭合了一个顶层 JSON 对象
        elif ch == "}":
            depth -= 1
            if depth == 0 and brace_starts:
                brace_pairs.append((brace_starts.pop(), i))

    # 按长度降序排列：最长的块最可能是完整的 JSON 对象
    # p[0] - p[1] 为负值（start < end），排序后最长的候选在前
    for start, end in sorted(brace_pairs, key=lambda p: p[0] - p[1]):
        candidate = text[start : end + 1].strip()
        # 去重：如果前面的策略已经产生了相同的候选，不重复添加
        if candidate and candidate not in candidates:
            candidates.append(candidate)


# =============================================================================
# parse_json — 五层递进 JSON 提取引擎
# =============================================================================
#
# 防御深度设计（Defense in Depth）：
#
#   LLM 输出 JSON 的方式因模型类型、prompt 质量、上下文长度和任务复杂度
#   而异。单靠一种提取策略无法覆盖所有情况。parse_json 实现了五层递进的
#   提取与修复策略，从最可靠到最激进依次尝试：
#
#   策略 1 — Markdown 栅栏提取：
#     匹配 ```json\n...\n``` 或 ```\n...\n``` 块。
#     最可靠的格式——Claude 和 GPT-4 在要求 JSON 输出时通常严格遵守此规范。
#
#   策略 2 — 原始文本尝试：
#     将整段文本当作 JSON 直接解析。
#     处理模型严格遵循 prompt、输出纯 JSON 的理想情况。
#
#   策略 3 — 首尾括号提取：
#     取第一个 { 或 [ 到最后一个 } 或 ] 之间的内容。
#     处理 JSON 前后有少量自然语言文本的情况（如"以下是结果：{...}"）。
#     缺陷：如果文本中 JSON 前后都有大量非 JSON 内容，会捕获过多噪声。
#
#   策略 3b — 平衡括号匹配（2026-07 新增）：
#     遍历文本，找出所有顶层平衡的 {...} 块，按长度降序依次尝试解析。
#     策略 3 的增强版：专门处理"分析文字 + JSON + 分析文字"的混合输出。
#     详见 _extract_balanced_json_candidates 的文档。
#
#   策略 4 — json_repair 修复：
#     对每个候选字符串，尝试用 json_repair 库修复常见语法错误后解析。
#     处理 JSON 有小缺陷的情况：缺少引号的键名、多余/缺少的逗号、
#     未闭合的括号/引号、截断的 JSON 等。
#
#   候选去重：
#     每个候选在加入列表后会被标记为 seen，避免同一字符串被重复解析。
#     策略 4 独立于 1-3b 的重试循环——修复引擎的执行成本远高于 json.loads，
#     不应在已知不可解析的候选上浪费。
#
#   失败处理：
#     全部策略失败时，截取原始文本前 400 字符写入错误消息，便于通过日志
#     定位是哪个 prompt 或哪个模型出了问题。上层的 _verify_single_conflict
#     会将其包装为 parse_error Issue 并记录到 orchestration state 中。
#


def parse_json(text: str) -> Any:
    """Parse a JSON value from an LLM response string.

    Tries multiple candidate extractions in order:

    1. Markdown fenced block (`` ```json … ``` `` or `` ``` … ``` ``)
    2. The raw text itself
    3. The first ``{…}`` or ``[…]`` substring (outermost braces)
    4. All balanced ``{…}`` substrings (longest-first)

    Candidates are deduplicated and tried in order; the first that parses
    successfully is returned.

    Raises:
        ValueError: If none of the candidates parse as valid JSON.
    """
    text = text.strip()
    if not text:
        raise ValueError("LLM returned empty text instead of JSON.")

    candidates: list[str] = []

    # ── 策略 1: Markdown 栅栏提取 ─────────────────────────────────────
    # 匹配 ```json\n...\n``` 或 ```\n...\n```（无语言标记的栅栏）
    # [\s\S]*? 是非贪婪匹配，避免跨多个栅栏块的错误匹配
    import re as _re

    fence_match = _re.search(r"```(?:json)?\s*\n([\s\S]*?)\n```", text)
    if fence_match:
        candidates.append(fence_match.group(1).strip())
    elif text.startswith("```"):
        # 兼容格式不规范的栅栏：文本以 ``` 开头但正则不匹配
        # 常见原因：缺少结尾的 ```、开头有 json 标记但没有紧随换行等
        # 此时假设第一行和最后一行是栅栏标记，取中间部分
        lines = text.splitlines()
        if len(lines) >= 2:
            fenced = "\n".join(lines[1:-1]).strip()
            if fenced.startswith("json"):
                fenced = fenced[4:].strip()
            candidates.append(fenced)

    # ── 策略 2: 原始文本 ─────────────────────────────────────────────
    # 最佳情况：模型直接输出纯 JSON，没有任何包装
    candidates.append(text)

    # ── 策略 3: 首尾括号提取 ─────────────────────────────────────────
    # 取文本中第一个 { 或 [ 到最后一个 } 或 ] 之间的内容
    # 处理 "以下是结果：{...}" 或 "[...]" 这类简单包装
    start_positions = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if start_positions:
        start = min(start_positions)
        opener = text[start]
        closer = "}" if opener == "{" else "]"
        end = text.rfind(closer)
        if end > start:
            candidates.append(text[start : end + 1])

    # ── 策略 3b: 平衡括号匹配 ───────────────────────────────────────
    # 策略 3 的增强版：处理"分析文字 + JSON + 分析文字"的混合输出
    # 用字符级状态机找出所有平衡的 {...} 块，按长度降序尝试
    _extract_balanced_json_candidates(text, candidates)

    # ── 候选去重 & 解析尝试 ───────────────────────────────────────────
    # 按添加顺序依次尝试（策略 1 → 2 → 3 → 3b），第一个成功即返回
    seen: set[str] = set()
    for candidate in candidates:
        normalised = candidate.strip()
        if not normalised or normalised in seen:
            continue
        seen.add(normalised)
        try:
            return json.loads(normalised)
        except json.JSONDecodeError:
            continue

    # ── 策略 4: json_repair 修复 ─────────────────────────────────────
    # 所有候选都无法直接解析时的最后兜底
    # json_repair 能修复的常见问题：
    # - 缺少引号的键名: {key: "value"} → {"key": "value"}
    # - 多余或缺少的逗号: {"a": 1,} → {"a": 1}
    # - 未闭合的括号/引号（尽力修复）
    # - 截断的 JSON（截断处之前的部分可用）
    # 注意：跳过空字符串/空列表/空对象的修复结果——这些是 json_repair
    # 对完全不可修复文本的降级输出，不应作为有效结果
    if _JSON_REPAIR_AVAILABLE:
        for candidate in candidates:
            normalised = candidate.strip()
            if not normalised:
                continue
            try:
                repaired = _repair_json(normalised, return_objects=True)
                if repaired is not None and repaired != "" and repaired != [] and repaired != {}:
                    return repaired
            except Exception:
                continue

    # ── 全部策略失败 ──────────────────────────────────────────────────
    # 截取前 400 字符转义后写入错误消息，便于通过 Issue 日志定位问题
    # 上层的 _verify_single_conflict 会将此异常捕获为 parse_error Issue
    excerpt = text[:400].replace("\n", "\\n")
    raise ValueError(f"Failed to parse model output as JSON: {excerpt}")


def make_id(prefix: str) -> str:
    """Return a collision-resistant ``prefix-<hex12>`` identifier."""
    return f"{prefix}-{uuid4().hex[:12]}"
