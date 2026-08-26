import json
from typing import Any

from pydantic import BaseModel

# =============================================================================
# 紧凑 Schema 生成子系统
# =============================================================================
#
# 设计目标：
#   传统的做法是在 prompt 里同时塞一个手写的 JSON 示例和一份完整的 JSON Schema
#   （见旧版 json_instruction 的实现）。这有两个问题：
#
#   1. 冗余 — 示例和 Schema 描述的是同一个结构，浪费 token；
#   2. 漂移 — 手写示例只能在 code review 中和 Pydantic 模型对齐，迟早不一致。
#
# 解决方案：
#   以 Pydantic 模型为唯一真实来源（single source of truth）。从
#   model_json_schema() 自动生成两项内容，并注入到系统 prompt 中：
#
#   - 紧凑字段定义（_build_compact_schema）：递归展开所有嵌套模型，每行一个字段，
#     展示字段名、类型、必填/可选、枚举值、默认值、描述。比 JSON Schema 短 60-80%，
#     且 LLM 更容易理解"平铺的字段列表"而非"带 $defs 的嵌套 JSON Schema"。
#   - 输出示例（_extract_example）：从 model_config.json_schema_extra.example
#     中提取，写到 prompt 的末尾，用于 few-shot 引导。
#
# 为什么不直接用 model_json_schema() 原样输出？
#   - JSON Schema 的 $defs 是图结构引用，LLM 在"向前引用"时容易出现幻觉
#   - 包含大量 Pydantic 内部元数据（title、anyOf 展开等），对 LLM 是噪声
#   - 枚举值被表示为 {"enum": ["a","b"]} 而非直观的 'a'|'b'|'c'


def _resolve_ref(ref: str, root_schema: dict[str, Any]) -> dict[str, Any]:
    """解析 JSON Schema 的 ``$ref`` 引用，返回被引用的 schema 片段。

    Pydantic 的 model_json_schema() 对嵌套模型使用 ``$ref`` 引用
    （如 ``#/$defs/ConflictItem``），而非内联展开。本函数沿着 ``/``
    分隔的路径逐级访问，将引用解析为实际的 schema dict，用于后续的
    紧凑类型描述和递归展开。
    """
    parts = ref.split("/")
    current = root_schema
    for part in parts[1:]:  # 跳过开头的 "#"
        current = current[part]
    return current


def _describe_type(prop_schema: dict[str, Any], root_schema: dict[str, Any]) -> str:
    """将单个 property schema 转为紧凑的类型描述字符串。

    典型输出示例：
    - ``string``                                  （基础类型）
    - ``number``                                  （float → JSON Schema number）
    - ``'low' | 'medium' | 'high'``               （枚举，repr() 加引号便于 LLM 区分字面量）
    - ``ConflictItem[]``                          （数组，递归解析 items.$ref 拿到模型名）
    - ``ConflictItem``                            （$ref 直接引用，展开为被引用模型的 title）

    处理 Optional 字段：
    Pydantic 对 ``str | None`` 生成 ``anyOf: [{"type": "string"}, {"type": "null"}]``。
    本函数取第一个非 null 分支的类型作为描述，字段的必填/可选由上层根据
    ``required`` 列表判断（Optional 字段不会出现在 required 中）。
    """
    if "$ref" in prop_schema:
        resolved = _resolve_ref(prop_schema["$ref"], root_schema)
        title = resolved.get("title")
        return title if isinstance(title, str) else prop_schema["$ref"].split("/")[-1]

    # Optional 字段的 anyOf: [{"type": "string"}, {"type": "null"}]
    # 只取非 null 分支作为类型描述，null 分支由 required 列表体现
    if "anyOf" in prop_schema:
        non_null = [t for t in prop_schema["anyOf"] if t.get("type") != "null"]
        if non_null:
            return _describe_type(non_null[0], root_schema)
        return "any"

    type_: str = str(prop_schema.get("type", "any"))

    # 枚举 → 'a'|'b'|'c'
    # 用 repr() 而非 str()：repr('high') → "'high'"，LLM 能明确识别这是字符串字面量
    if "enum" in prop_schema:
        return " | ".join(repr(v) for v in prop_schema["enum"])

    # 数组 → ItemType[]
    # 递归解析 items 的类型，支持嵌套模型数组（ConflictItem[]）和枚举数组（'a'|'b'[]）
    if type_ == "array":
        items = prop_schema.get("items", {})
        item_type = _describe_type(items, root_schema)
        return f"{item_type}[]"

    # 基础类型：number / integer / string / boolean
    # 保留 JSON Schema 的原生类型名（number 而非 float），LLM 对 JSON Schema 术语更敏感
    return type_


def _build_compact_schema(schema: dict[str, Any]) -> str:
    """从 JSON Schema 构建紧凑可读的字段定义文本。

    将顶层模型及其所有嵌套模型展开为平铺的字段列表，每个模型一节，
    用缩进区分层级。典型输出：:

        ConflictAnalysisResult:
          conflicts: ConflictItem[] (必填)
          ConflictItem:
            id: string (必填)
            theme: string (必填)
            severity: 'low'|'medium'|'high'|'critical' (必填)
            status: 'open'|'resolved'|'rejected' (可选) (默认="open")
            hypotheses: HypothesisItem[] (可选)
            HypothesisItem:
              id: string (必填)
              ...

    seen 集合防止同一模型被两个字段引用时重复输出（虽然当前业务模型中
    不会出现循环引用，但防御性编程避免无限递归）。
    """
    lines: list[str] = []
    seen: set[str] = set()
    _describe_model(schema, schema, lines, seen, indent=0)
    return "\n".join(lines)


def _describe_model(
    model_schema: dict[str, Any],
    root_schema: dict[str, Any],
    lines: list[str],
    seen: set[str],
    indent: int,
) -> None:
    """递归描述单个 Pydantic 模型的字段，将嵌套模型展开到同级。

    关键设计决策：嵌套模型不在父字段下缩进多级，而是作为**平级节点**输出。
    例如 ``ConflictItem`` 会在 ``conflicts: ConflictItem[]`` 之后另起一节
    ``  ConflictItem:``，而不是在 conflicts 字段下层层嵌套。原因：

    - LLM 阅读平铺的字段列表比阅读深度嵌套结构更不容易遗漏字段；
    - 减少因缩进过深导致的行宽问题（某些模型对超长行处理不佳）；
    - 与 JSON Schema $defs 的"引用后展开"语义一致。

    seen 集合确保同一模型被多处引用时只展开一次。
    """
    title = model_schema.get("title", "")
    if not title or title in seen:
        return
    seen.add(title)

    prefix = "  " * indent
    lines.append(f"{prefix}{title}:")

    properties = model_schema.get("properties", {})
    required_fields: set[str] = set(model_schema.get("required", []))

    for field_name, field_schema in properties.items():
        type_desc = _describe_type(field_schema, root_schema)
        req = "必填" if field_name in required_fields else "可选"

        # 描述（来自 Field(description=...) 或 field docstring）
        desc = field_schema.get("description", "")
        desc_str = f" — {desc}" if desc else ""

        # 默认值
        # Pydantic 对 Field(default="open") 会在 JSON Schema 中生成 "default": "open"；
        # 对 Field(default_factory=list) 生成 "default": []。
        # 只对非必填字段展示默认值，必填字段的 default 无意义
        default_val = field_schema.get("default")
        if default_val is not None and field_name not in required_fields:
            desc_str += f" (默认={json.dumps(default_val, ensure_ascii=False)})"

        lines.append(f"{prefix}  {field_name}: {type_desc} ({req}){desc_str}")

        # 递归展开嵌套模型（数组元素 $ref 或直接 $ref 引用）
        if "items" in field_schema and "$ref" in field_schema["items"]:
            ref_schema = _resolve_ref(field_schema["items"]["$ref"], root_schema)
            _describe_model(ref_schema, root_schema, lines, seen, indent + 1)
        elif "$ref" in field_schema:
            ref_schema = _resolve_ref(field_schema["$ref"], root_schema)
            _describe_model(ref_schema, root_schema, lines, seen, indent + 1)


def _extract_example(schema: dict[str, Any]) -> dict[str, Any] | None:
    """从 JSON Schema 顶层提取 ``json_schema_extra.example``。

    Pydantic v2 的 ``ConfigDict(json_schema_extra={"example": {...}})``
    会将 example 键合并到 model_json_schema() 的顶层。本函数读取该键
    作为 few-shot 示例的来源。

    注意：只有顶层模型的 example 会被提取，嵌套模型（$defs 中的子模型）
    的 json_schema_extra 对最终输出无影响——所有嵌套字段的示例应通过
    顶层模型的 example 中的嵌套对象来体现。
    """
    return schema.get("example")


# =============================================================================
# 主入口：json_instruction()
# =============================================================================
#
# 本函数生成的 prompt 片段会被拼接到各个 agent 的系统 prompt 末尾，
# 作为"输出格式约束"。它解决的核心问题是：
#
#   大模型（尤其是 deepseek 系列）在拥有工具调用能力后，倾向于在最终回复中
#   输出"分析报告"式的自然语言，而不是纯 JSON 结构。即使系统 prompt 中写了
#   "只返回 JSON"，模型也可能先写一段"根据分析…"的引导语再输出 JSON，
#   或者在 JSON 前后包裹 Markdown 标题和段落。
#
# 为对抗这种行为，输出要求设计了三层约束：
#
#   第1条（JSON 起止边界）：
#     "以 { 开头、以 } 结尾" — 告诉模型整个回复就是一个 JSON 对象，
#     没有前缀也没有后缀。这比"只返回 JSON"更具体、更难被误解。
#
#   第2条（绝对禁止列表）：
#     显式列举所有常见违规形式：自然语言分析、推理过程、摘要、前言、后记、
#     Markdown 标题、代码块标记。模型更擅长遵循"不要做 X"的否定指令
#     而非"只做 Y"的肯定指令——这是 prompt engineering 中的已知经验。
#
#   第3条（引导语禁令）：
#     针对中文模型的常见违规模式："根据分析…""综上…""以下是结果…"。
#     这些引导语在中文训练语料中高频出现，模型在和"分析任务"关联后
#     会自动生成，需要专门的点名禁止。
#
#   第4条（结构约束）：
#     衔接下方自动生成的字段定义，要求严格匹配。
#
# 防御深度（Defense in Depth）：
#   即使 prompt 层失败（模型仍输出分析文本），下游 parse_json() 还有
#   独立的 JSON 提取层：Markdown 栅栏提取 → 原始文本尝试 → 首尾括号
#   提取 → 平衡括号匹配 → json_repair 修复。详见 alphabee/utils/pipeline.py。
#
# 为什么不用 structured output / JSON mode？
#   目前 DeepAgents 框架不直接支持 per-agent 的 response_format 参数透传。
#   这是在框架层面的待改进项。届时 json_instruction() 可以简化甚至废弃。
#
def json_instruction(model: type[BaseModel]) -> str:
    """从 Pydantic 模型生成输出格式指令。

    自动从模型的 ``model_json_schema()`` 生成紧凑的字段定义，
    并从 ``model_config.json_schema_extra.example`` 提取输出示例。

    无需手写 JSON 示例或 JSON Schema —— Pydantic 模型是唯一的真实来源。
    """
    json_schema = model.model_json_schema()
    compact_schema = _build_compact_schema(json_schema)
    example = _extract_example(json_schema)

    parts = [
        "输出要求（严格遵守，违反将导致解析失败）：",
        "1. 你的最终回复必须是**纯 JSON 对象**，以 ``{`` 开头、以 ``}`` 结尾。",
        "2. 绝对禁止：自然语言分析、推理过程、摘要、前言、后记、Markdown 标题、代码块标记。",
        '3. 不要写"根据分析…"、"综上…"、"以下是结果…"等引导语——直接输出 JSON。',
        "4. 顶层结构必须严格匹配下方字段定义。",
        "",
        "## 字段定义",
        compact_schema,
    ]
    if example:
        parts.extend(
            [
                "",
                "## 输出示例",
                json.dumps(example, ensure_ascii=False, indent=2),
            ]
        )
    else:
        parts.append("（未提供示例，请严格按照字段定义输出。）")

    return "\n".join(parts)
