import json
from pydantic import BaseModel


def json_instruction(schema: type[BaseModel], *, example: str = "") -> str:
    return (
        "输出要求：\n"
        "1. 只返回 JSON，不要 Markdown，不要代码块，不要额外解释。\n"
        "2. 顶层必须严格符合下面给出的结构。\n"
        f"3. 输出示例：{example}\n"
        f"4. JSON Schema:\n{json.dumps(schema.model_json_schema(), ensure_ascii=False, indent=2)}"
    )
