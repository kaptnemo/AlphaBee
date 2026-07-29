import json

from pydantic import BaseModel


def _resolve_ref(ref: str, root_schema: dict) -> dict:
    """解析 JSON Schema 的 ``$ref`` 引用，返回被引用的 schema 片段。"""
    parts = ref.split("/")
    current = root_schema
    for part in parts[1:]:  # 跳过开头的 "#"
        current = current[part]
    return current


def _describe_type(prop_schema: dict, root_schema: dict) -> str:
    """将单个 property schema 转为紧凑的类型描述字符串。"""
    if "$ref" in prop_schema:
        resolved = _resolve_ref(prop_schema["$ref"], root_schema)
        return resolved.get("title", prop_schema["$ref"].split("/")[-1])

    # 处理 Optional 字段的 anyOf: [{"type": "string"}, {"type": "null"}]
    if "anyOf" in prop_schema:
        non_null = [t for t in prop_schema["anyOf"] if t.get("type") != "null"]
        if non_null:
            return _describe_type(non_null[0], root_schema)
        return "any"

    type_ = prop_schema.get("type", "any")

    # 枚举 → 'a'|'b'|'c'
    if "enum" in prop_schema:
        return " | ".join(repr(v) for v in prop_schema["enum"])

    # 数组 → ItemType[]
    if type_ == "array":
        items = prop_schema.get("items", {})
        item_type = _describe_type(items, root_schema)
        return f"{item_type}[]"

    # 基础类型：number / integer / string / boolean
    return type_


def _build_compact_schema(schema: dict) -> str:
    """从 JSON Schema 构建紧凑可读的字段定义文本。"""
    lines: list[str] = []
    seen: set[str] = set()
    _describe_model(schema, schema, lines, seen, indent=0)
    return "\n".join(lines)


def _describe_model(
    model_schema: dict,
    root_schema: dict,
    lines: list[str],
    seen: set[str],
    indent: int,
):
    """递归描述单个 Pydantic 模型的字段，将嵌套模型展开到同级。"""
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

        # 描述
        desc = field_schema.get("description", "")
        desc_str = f" — {desc}" if desc else ""

        # 默认值
        default_val = field_schema.get("default")
        if default_val is not None and field_name not in required_fields:
            desc_str += f" (默认={json.dumps(default_val, ensure_ascii=False)})"

        lines.append(f"{prefix}  {field_name}: {type_desc} ({req}){desc_str}")

        # 递归展开嵌套模型（数组元素或直接引用）
        if "items" in field_schema and "$ref" in field_schema["items"]:
            ref_schema = _resolve_ref(field_schema["items"]["$ref"], root_schema)
            _describe_model(ref_schema, root_schema, lines, seen, indent + 1)
        elif "$ref" in field_schema:
            ref_schema = _resolve_ref(field_schema["$ref"], root_schema)
            _describe_model(ref_schema, root_schema, lines, seen, indent + 1)


def _extract_example(schema: dict) -> dict | None:
    """从 JSON Schema 顶层提取 ``json_schema_extra.example``。"""
    return schema.get("example")


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
