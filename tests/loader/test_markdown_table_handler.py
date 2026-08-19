"""markdown_table_handler 移植功能测试。"""

from __future__ import annotations

from alphabee.loader.markdown_table_handler import (
    covert_markdown_table_to_html,
    extract_tables_from_text,
    merge_markdown_tables,
    merge_tables,
)


def test_merge_markdown_tables_joins_adjacent_rows():
    md = "\n\n".join(
        [
            "| 项目 | 金额 |",
            "|------|------|",
            "| 营收 | 100  |",
            "",
            "| 续行 | 200  |",
            "",
            "普通段落",
        ]
    )
    merged = merge_markdown_tables(md)
    lines = merged.splitlines()
    assert lines.count("| 续行 | 200  |") == 1
    assert lines.index("| 营收 | 100  |") < lines.index("| 续行 | 200  |")
    assert "普通段落" in merged


def test_merge_tables_merges_html_split_tables():
    # 两个同构 HTML 表格（分页拆开）应合并为一个 <table>
    md = (
        "<table><tr><th>项目</th><th>金额</th></tr>"
        "<tr><td>营收</td><td>100</td></tr></table>\n\n"
        "<table><tr><th>项目</th><th>金额</th></tr>"
        "<tr><td>净利</td><td>20</td></tr></table>"
    )
    merged = merge_tables(md)
    assert merged.count("<table") == 1
    assert "净利" in merged


def test_extract_tables_from_text_splits_blocks():
    md = "\n".join(
        [
            "表1 主要财务数据",
            "| 项目 | 金额 |",
            "|------|------|",
            "| 营收 | 100  |",
            "",
            "这是正文段落",
            "",
            "表2 股东信息",
            "<table><tr><td>股东</td><td>比例</td></tr></table>",
        ]
    )
    blocks = extract_tables_from_text(md)
    tables = [b for b in blocks if b["type"] == "table"]
    texts = [b for b in blocks if b["type"] == "text"]
    assert len(tables) == 2
    assert len(texts) >= 1
    assert "<table" in tables[0]["content"]
    assert tables[0]["table_name"] == "表1 主要财务数据"


def test_covert_markdown_table_to_html():
    md_table = "| A | B |\n|---|---|\n| 1 | 2 |"
    html = covert_markdown_table_to_html(md_table)
    assert "<th>A</th>" in html
    assert "<td>1</td>" in html
