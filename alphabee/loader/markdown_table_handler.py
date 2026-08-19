"""Markdown/HTML 表格处理工具：拆分表格识别、跨页表格合并、表格名提取。

从 BidGenius 的 ``markdown_table_handler.py`` 移植并做 AlphaBee 化改造：

- 移除对 ``bidgenius.llm.client`` 的依赖，LLM 表格整理改用 AlphaBee 的 LLM 配置
  （``alphabee.config.settings.llm``），且**默认不调用 LLM**（``llm_format_table=False``）；
- 移除原先"把格式化结果写进临时目录文件"的行为——LLM 整理结果只作为字符串返回，
  由上层决定落盘位置（见 ``alphabee/loader/pdf_ocr_loader.py`` 的持久化工作区）；
- 把面向特种钢投标场景的提示词泛化为通用的财报/研报表格整理规则。

主要函数：

- :func:`merge_tables`：合并 Markdown 中因分页被拆开的表格（markdown 表格 + HTML 表格）；
- :func:`extract_tables_from_text`：从 Markdown 文本中抽取表格块与剩余文本块；
- :func:`llm_format_html_table`：可选地用 LLM 整理 HTML 表格（默认关闭）。
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from openai import OpenAI

from alphabee.config import settings

# ── 表格结构判断 ────────────────────────────────────────────────────────────


def has_header(table) -> bool:
    return bool(table.find("th"))


def col_count(table) -> int:
    first_row = table.find("tr")
    return len(first_row.find_all(["td", "th"])) if first_row else 0


def get_consecutive_html_tables_range_v2(md_lines: list[str], start_idx: int = 0, min_tables: int = 2):
    """从 ``start_idx`` 开始查找连续的 HTML 表格块范围（至少 ``min_tables`` 个 table）。

    兼容 ``<table`` / ``</table>`` 不在行首/行尾、同行出现多次标签等情况。
    返回 ``(start_line, end_line)``，找不到返回 ``None``。
    """
    n = len(md_lines)
    tables: list[tuple[int, int]] = []
    depth = 0
    table_start: int | None = None

    idx = start_idx
    while idx < n:
        raw = md_lines[idx]
        line = raw.strip()

        if line == "" and depth == 0:
            idx += 1
            continue

        open_cnt = raw.count("<table")
        close_cnt = raw.count("</table>")
        has_table_tag = open_cnt > 0 or close_cnt > 0

        if has_table_tag:
            if depth == 0 and open_cnt > 0:
                table_start = idx
            depth += open_cnt
            depth -= close_cnt
            if depth < 0:
                depth = 0
            if depth == 0 and table_start is not None and close_cnt > 0:
                tables.append((table_start, idx))
                table_start = None
            idx += 1
            continue

        if depth > 0:
            idx += 1
            continue

        if len(tables) >= min_tables:
            return (tables[0][0], tables[-1][1])
        tables = []
        idx += 1

    if depth == 0 and len(tables) >= min_tables:
        return (tables[0][0], tables[-1][1])
    return None


def _normalized_col_count(table) -> int:
    """估算表格总列数（尊重 colspan，取前几行的最大值）。"""
    rows = table.find_all("tr")
    if not rows:
        return 0
    max_cols = 0
    for row in rows[:3]:
        cols = 0
        for cell in row.find_all(["td", "th"]):
            span = cell.get("colspan")
            try:
                span_val = int(span) if span is not None else 1
            except (TypeError, ValueError):
                span_val = 1
            cols += max(span_val, 1)
        if cols > max_cols:
            max_cols = cols
    return max_cols


def _tables_compatible(left, right) -> bool:
    """启发式判断两个相邻表格是否可以合并（列数 / 行签名兼容）。"""
    left_cols = _normalized_col_count(left)
    right_cols = _normalized_col_count(right)
    if left_cols == 0 or right_cols == 0:
        return False

    if left_cols != right_cols:
        left_row = left.find_all("tr")[-1:]
        right_row = right.find_all("tr")[:1]
        if left_row and right_row:
            left_signature = tuple(
                (cell.name, cell.get("colspan", "1")) for cell in left_row[0].find_all(["td", "th"])
            )
            right_signature = tuple(
                (cell.name, cell.get("colspan", "1")) for cell in right_row[0].find_all(["td", "th"])
            )
            if left_signature == right_signature:
                return True
            left_logical = tuple(cell.name for cell in left_row[0].find_all(["td", "th"]))
            right_logical = tuple(cell.name for cell in right_row[0].find_all(["td", "th"]))
            if left_logical == right_logical:
                return True
        return False

    def row_signature(row):
        signature = []
        for cell in row.find_all(["td", "th"]):
            span = cell.get("colspan")
            try:
                span_val = int(span) if span is not None else 1
            except (TypeError, ValueError):
                span_val = 1
            signature.append((cell.name, max(span_val, 1)))
        return tuple(signature)

    left_rows = left.find_all("tr")[:10]
    right_rows = right.find_all("tr")[:10]
    left_patterns = {row_signature(row) for row in left_rows if row.find_all(["td", "th"])}
    right_patterns = {row_signature(row) for row in right_rows if row.find_all(["td", "th"])}
    if not left_patterns or not right_patterns:
        return True
    return not left_patterns.isdisjoint(right_patterns)


def merge_split_tables(html: str) -> str:
    """把因分页被拆开的 HTML 表格行合并到前一个表格中，返回合并后的 tables 字符串。"""
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")

    merged_tables = []
    i = 0
    while i < len(tables):
        current = tables[i]
        j = i + 1
        while j < len(tables):
            next_table = tables[j]
            if not _tables_compatible(current, next_table):
                break
            rows = list(next_table.find_all("tr"))
            if has_header(next_table) and rows and rows[0].find("th"):
                rows = rows[1:]
            for row in rows:
                current.append(row.extract())
            next_table.decompose()
            j += 1
        merged_tables.append(current)
        i = j
    return "".join(str(t) for t in merged_tables)


def is_markdown_header(lines: list[str]) -> bool:
    return (
        len(lines) >= 2
        and "|" in lines[0]
        and set(lines[1].replace("|", "").strip()) <= {"-", ":"}
    )


def merge_markdown_tables(md_text: str) -> str:
    """合并相邻的 Markdown 管道表格（表头对齐、数据行连续的段落合并）。"""
    blocks = md_text.strip().split("\n\n")
    result: list[str] = []
    i = 0
    while i < len(blocks):
        block = blocks[i]
        lines = block.splitlines()
        if is_markdown_header(lines):
            merged_lines = lines[:]
            j = i + 1
            while j < len(blocks):
                next_lines = blocks[j].splitlines()
                if not is_markdown_header(next_lines) and next_lines[0].startswith("|"):
                    merged_lines.extend(next_lines)
                    j += 1
                else:
                    break
            result.append("\n".join(merged_lines))
            i = j
        else:
            result.append(block)
            i += 1
    return "\n\n".join(result)


def merge_tables(md_text: str) -> str:
    """合并 Markdown 中的拆分表格（markdown 表格 + HTML 表格两路）。"""
    merged_md_text = merge_markdown_tables(md_text)
    md_text_lines = merged_md_text.splitlines()
    start_idx = 0
    while start_idx < len(md_text_lines):
        tables_range = get_consecutive_html_tables_range_v2(md_text_lines, start_idx)
        if not tables_range:
            break
        start, end = tables_range
        markdown_table_text = "\n".join(md_text_lines[start : end + 1])
        merged_html = merge_split_tables(markdown_table_text)
        md_text_lines[start : end + 1] = [merged_html]
        start_idx = start + 1
    return "\n".join(md_text_lines)


def covert_markdown_table_to_html(md_table: str) -> str:
    """把 Markdown 管道表格转换为 HTML 表格字符串。"""
    lines = md_table.strip().splitlines()
    if len(lines) < 2:
        return ""
    headers = [h.strip() for h in lines[0].strip().strip("|").split("|")]
    html = ["<table>\n<thead>\n<tr>"]
    for header in headers:
        html.append(f"  <th>{header}</th>")
    html.append("</tr>\n</thead>\n<tbody>")
    for line in lines[2:]:
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        html.append("<tr>")
        for col in cols:
            html.append(f"  <td>{col}</td>")
        html.append("</tr>")
    html.append("</tbody>\n</table>")
    return "\n".join(html)


def extract_table_name_from_previous_lines(lines: list[str]) -> tuple[str | None, list[str]]:
    """从当前行之前的文本行中提取表格名称。"""
    for idx, line in enumerate(reversed(lines)):
        stripped = line.strip()
        if stripped:
            if len(stripped) < 100 and ("表" in stripped or "table" in stripped.lower()):
                return stripped, lines[: -(idx + 1)]
            return None, lines
    return None, lines


def extract_table_name_from_post_lines(lines: list[str], start_idx: int) -> tuple[str | None, int]:
    """从当前行之后的文本行中提取表格名称。"""
    total_lines = len(lines)
    for idx in range(start_idx, total_lines):
        stripped = lines[idx].strip()
        if stripped:
            if len(stripped) < 100 and ("表" in stripped or "table" in stripped.lower()):
                return stripped, idx + 1
            return None, start_idx
    return None, start_idx


def extract_tables_from_text(md_text: str) -> list[dict]:
    """从 Markdown 文本中抽取表格块与文本块。

    返回 ``blocks`` 列表，元素为 ``{"type": "table"|"text", "content": str, "table_name"?: str}``。
    表格块内容为 HTML 表格字符串，文本块内容为原始文本。
    """
    blocks: list[dict[str, str]] = []
    text_lines: list[str] = []
    lines = md_text.splitlines()
    idx = 0
    total_lines = len(lines)

    while idx < total_lines:
        line = lines[idx]
        stripped = line.strip()

        if stripped.startswith("|"):
            table_block = [line]
            j = idx + 1
            while j < total_lines and lines[j].strip().startswith("|"):
                table_block.append(lines[j])
                j += 1
            if len(table_block) >= 2 and is_markdown_header(table_block[:2]):
                html_table = covert_markdown_table_to_html("\n".join(table_block))
                table_name, text_lines = extract_table_name_from_previous_lines(text_lines)
                if not table_name:
                    table_name, j = extract_table_name_from_post_lines(lines, j)
                if text_lines:
                    blocks.append({"type": "text", "content": "\n".join(text_lines)})
                    text_lines = []
                blocks.append(
                    {
                        "type": "table",
                        "content": html_table,
                        "table_name": table_name or "未知表格",
                    }
                )
                idx = j
                continue

        if "<table" in stripped:
            html_block = [line]
            j = idx + 1
            while j < total_lines and "</table>" not in lines[j]:
                html_block.append(lines[j])
                j += 1
            if j < total_lines:
                html_block.append(lines[j])
                j += 1
            table_name, text_lines = extract_table_name_from_previous_lines(text_lines)
            if not table_name:
                table_name, j = extract_table_name_from_post_lines(lines, j)
            if text_lines:
                blocks.append({"type": "text", "content": "\n".join(text_lines)})
                text_lines = []
            block_text = "\n".join(html_block)
            soup = BeautifulSoup(block_text, "html.parser")
            for table in soup.find_all("table"):
                blocks.append(
                    {
                        "type": "table",
                        "content": str(table),
                        "table_name": table_name or "未知表格",
                    }
                )
            idx = j
            continue

        text_lines.append(line)
        idx += 1

    if text_lines:
        blocks.append({"type": "text", "content": "\n".join(text_lines)})
    return blocks


# ── 可选的 LLM 表格整理（默认关闭） ───────────────────────────────────────

USER_PROMPT = """你是一名专业的财务报表/研究报告数据处理人员，负责对 HTML 表格进行结构整理与语义优化。

请在严格保持原始信息不变的前提下，对给定的 HTML 表格进行整理和优化。

【总体要求】
1. 如果表格结构和内容完全正确、清晰，则直接原样返回表格，不做任何修改。
2. 必须保持表格的完整性与语义一致性：不得遗漏、增改或扭曲任何原始信息（数值、单位、范围、术语）。
3. 如果表格缺少表头，或表头名称不清晰，请补充或规范表头名称（如：项目、本期金额、上期金额、同比变动等）。

【拆分原则】
- 合并单元格（rowspan/colspan）不允许保留：必须根据语义拆分为多个独立单元格，
  拆分后每一行应构成一条可独立理解的记录。
- 若同一单元格包含多条带编号的内容（如 1)、2)、3) 或 a)、b)、c)），应拆分成多行，
  编号保留，其余列内容复制到新行以保持对齐。
- 同一语义的并列描述（如同义词、等价表达）不应过度拆分。

【输出要求】
- 仅输出整理后的 HTML 表格代码，不要输出任何解释性文字、分析说明或额外内容。

请对以下 HTML 表格进行整理和优化：

{html_table}
"""


def llm_format_html_table(html_table: str, table_name: str = "未知表格") -> str:
    """用 LLM 整理 HTML 表格（默认在 loader/MCP 调用链中关闭，仅按需启用）。

    基于 ``alphabee.config.settings.llm`` 的同步 OpenAI 客户端执行；仅在
    ``PDFOCRLoader(..., llm_format_table=True)`` 时才会被调用。整理结果只作为
    字符串返回，不再写入任何临时文件。
    """
    client = OpenAI(
        api_key=settings.llm.api_key,
        base_url=settings.llm.base_url,
    )
    response = client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=[
            {
                "role": "user",
                "content": USER_PROMPT.format(html_table=html_table),
            }
        ],
        temperature=0,
        max_tokens=4096,
    )
    return response.choices[0].message.content or html_table
