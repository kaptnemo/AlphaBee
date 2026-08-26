import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt


def _ngram_split(text: str, n: int = 3) -> list[str]:
    length = len(text)
    if length < n:
        return [text]
    return [text[i : i + n] for i in range(length - n + 1)]


def _ngram_split_lines(text_lines: list[str], n: int = 3, threshold: int = 2) -> list[str]:
    ngrams: list[str] = []
    for line in text_lines:
        ngrams.extend(_ngram_split(line, n))

    ngram_counts = Counter(ngrams)
    return [ngram for ngram, count in ngram_counts.items() if count > threshold]


def _split_markdown_by_sections(
    md_text: str, file_name: str | None = None, file_type: str | None = None
) -> list[dict[str, Any]]:
    md = MarkdownIt()
    tokens = md.parse(md_text)

    sections: list[dict[str, Any]] = []
    current_section: dict[str, Any] = {
        "metadata": {
            "source": file_name,
            "file_type": file_type,
            "title": "前言",
            "level": 0,
            "section_path": [],
            "section_path_nums": [],
        },
        "title": "前言",
        "level": 0,
        "content": [],
    }

    SECTION_PATTERN = re.compile(r"^(?P<number>\d+(?:\.\d+)*)(?:\s+|[.:;-]+)(?P<title>.+)$")
    section_stack: list[str] = []
    section_numbers_stack: list[str] = []
    for idx, token in enumerate(tokens):
        if token.type == "heading_open":
            if current_section.get("content"):
                current_section["content"] = [
                    line for line in current_section["content"] if line != current_section["title"]
                ]
                sections.append(current_section)

            level = int(token.tag[1])
            title = tokens[idx + 1].content if idx + 1 < len(tokens) else ""
            match = SECTION_PATTERN.match(title)
            if match:
                section_text = match.group("title").strip()
                section_number = match.group("number").strip()
            else:
                section_text = title.strip()
                section_number = ""

            section_stack = section_stack[: level - 1]
            section_stack.append(section_text)

            section_numbers_stack = section_numbers_stack[: level - 1]
            section_numbers_stack.append(section_number)

            current_section = {
                "metadata": {
                    "source": file_name,
                    "file_type": file_type,
                    "title": title,
                    "level": level,
                    "section_path": section_stack.copy(),
                    "section_path_nums": section_numbers_stack.copy(),
                },
                "title": title,
                "level": level,
                "content": [],
            }
        elif token.type == "inline" and token.content:
            current_section.setdefault("content", []).append(token.content)
        elif token.type == "html_block" and token.content:
            current_section.setdefault("content", []).append(token.content)

    if current_section.get("content"):
        current_section["content"] = [line for line in current_section["content"] if line != current_section["title"]]
        sections.append(current_section)

    return sections


def parse_markdown_to_cleaned_sections(
    md_text: str, page_count: int, file_name: str | None, file_type: str | None
) -> list[dict[str, Any]]:
    sections = _split_markdown_by_sections(md_text, file_name=file_name, file_type=file_type)
    if not sections or page_count <= 0:
        return sections

    title_counts: dict[str, int] = {}
    for section in sections:
        title: str = section.get("title", "")
        title_counts[title] = title_counts.get(title, 0) + 1

    # 页眉页脚判定：标题须至少在 2 个页面出现且占比 >= 60%（避免单页报告被整体误删）
    header_footer_titles = [title for title, count in title_counts.items() if count >= 2 and count / page_count >= 0.6]

    content_lines: dict[str, list[str]] = {}
    for section in sections:
        if section.get("title") in header_footer_titles:
            content_lines.setdefault(section["title"], []).extend(section.get("content", []))

    ngram_dict = {
        title: _ngram_split_lines(lines, n=5, threshold=max(1, title_counts[title] // 2))
        for title, lines in content_lines.items()
    }

    ngram_threshold = 3
    cleaned_sections: list[dict[str, Any]] = []
    for section in sections:
        if section.get("title") in header_footer_titles:
            section_ngrams = ngram_dict.get(section["title"], [])
            cleaned_content: list[str] = []
            for content_line in section.get("content", []):
                content_ngrams = _ngram_split(content_line, n=5)
                content_ngrams_set = set(content_ngrams)
                ngram_count = sum(1 for ngram in content_ngrams_set if ngram in section_ngrams)
                # ngram_threshold 根据content_ngrams的长度动态调整
                dynamic_threshold = max(ngram_threshold, len(content_ngrams) // 5)
                if ngram_count < dynamic_threshold:
                    cleaned_content.append(content_line)
            if cleaned_content:
                if cleaned_sections:
                    cleaned_sections[-1].setdefault("content", []).extend(cleaned_content)
                else:
                    cleaned_sections.append(
                        {
                            "metadata": section.get("metadata"),
                            "title": section.get("title"),
                            "level": section.get("level"),
                            "content": cleaned_content,
                        }
                    )
        else:
            cleaned_sections.append(section)

    return cleaned_sections


def parse_sections_to_folder_structure(sections: list[dict[str, Any]], save_dir: Path) -> dict[str, Any]:
    """把sections解析成文件夹结构，并生成对应的文件夹和文件"""

    content_parts: dict[str, list[str]] = {}
    for section in sections:
        section_path = section["metadata"]["section_path"]
        if not section_path:
            continue
        parent_path = save_dir
        for part in section_path[:-1]:
            part = part.strip().replace(" ", "_").replace("/", "_")
            parent_path = parent_path / part
            # 旧叶子文件（同名 .md）转为目录时先删除再建目录，避免 FileExistsError
            if parent_path.is_file():
                parent_path.unlink()
            parent_path.mkdir(parents=True, exist_ok=True)
            if str(parent_path) in content_parts:
                with open(parent_path / "content.md", "w", encoding="utf-8") as f:
                    f.write("\n".join(content_parts[str(parent_path)]))

        last_part = section_path[-1].strip().replace(" ", "_").replace("/", "_")
        content = section.get("content", [])
        if content:
            content_parts["/".join(section_path)] = content
            file_path = parent_path / f"{last_part}.md"
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(content))
            except Exception as e:
                print(f"Error writing to file {file_path}: {e}", file=sys.stderr)

    return {"section_count": len(sections), "content_parts": content_parts}


def _sanitize_report_name(report_name: str) -> str:
    """清洗报告目录名：去扩展名/清洗后缀、去路径分隔符，防止目录穿越。"""
    name = report_name.strip()
    if name.lower().endswith(".md"):
        name = name[:-3]
    if name.endswith(".cleaned"):
        name = name[:-8]
    name = name.replace("/", "_").replace("\\", "_")
    if not name or name in {".", ".."}:
        raise ValueError(f"Invalid report_name: {report_name!r}")
    return name


def _normalize_report_code(code: str) -> str:
    """归一化股票代码：去交易所后缀（``300750.SZ`` → ``300750``）。"""
    code = (code or "").strip().upper()
    if "." in code:
        code = code.split(".")[0]
    return code


def _strip_company_prefix(report_name: str, company_name: str) -> str:
    """去掉报告名里冗余的「公司名：」前缀，只保留报告期+类型部分。"""
    name = report_name.strip()
    if not company_name:
        return name
    for sep in ("：", ":", " "):
        prefix = f"{company_name}{sep}"
        if name.startswith(prefix):
            return name[len(prefix) :].strip()
    return name


def reports_root(save_dir: str | Path | None = None) -> Path:
    """报告根目录：提供 ``save_dir`` 用之，否则默认 ``<PROJECT_ROOT>/reports``。"""
    return Path(save_dir).expanduser().resolve() if save_dir else Path(__file__).resolve().parents[2] / "reports"


def write_markdown_report_folder(
    markdown_text: str,
    report_name: str,
    *,
    company_name: str | None = None,
    company_code: str | None = None,
    page_count: int | None = None,
    save_dir: str | Path | None = None,
    overwrite: bool = True,
    strip_doc_title: bool = True,
) -> Path:
    """把一份财报 Markdown 按章节拆分，写入报告文件夹结构。

    这是「OCR → 解析成文件夹 → 检索问答」链路中的解析步骤：
    - 章节拆分 + 页眉页脚 ngram 去重走 :func:`parse_markdown_to_cleaned_sections`
      （提供 ``page_count`` 时启用去重；否则仅按标题切分）；
    - 文件夹结构由 :func:`parse_sections_to_folder_structure` 生成（父章节为目录、
      叶子章节为 ``.md`` 文件）；
    - 报告目录下同时保留完整全文副本 ``<报告期>.md``。

    目录结构分两种：

    - **新嵌套结构**（提供 ``company_name`` 时）::

          <save_dir>/<公司中文名>(<6位代码>)/财报/<报告期+类型>/

      例：``reports/宁德时代(300750)/财报/2026年半年度报告/``。第三层只保留
      报告期+类型（自动去掉报告名里冗余的「公司名：」前缀）。

    - **旧平铺结构**（未提供 ``company_name``，向后兼容）::

          <save_dir>/<报告名>/

    Args:
        markdown_text: 清洗后的 Markdown 全文（OCR loader 的输出或任意 md）。
        report_name: 报告目录名，如 ``宁德时代：2026年半年度报告``（或仅 ``2026年半年度报告``）。
        company_name: 公司中文简称（如 ``宁德时代``）；提供时启用新嵌套结构。
        company_code: 6 位股票代码（如 ``300750``）；仅在 ``company_name`` 提供时生效，
            拼入目录名 ``<公司名>(<代码>)``。
        page_count: 原始 PDF 页数；提供时启用页眉页脚去重。
        save_dir: 报告根目录（默认 ``<PROJECT_ROOT>/reports``）。
        overwrite: 同名目录已存在时是否覆盖重建（默认 True）。
        strip_doc_title: 是否去掉 markdown 顶层的 ``# <文档标题>`` 行
            （loader 会注入文档标题；报告目录本身即承载标题信息）。

    Returns:
        报告目录绝对路径（写入完成后 ``query_financial_report`` 即可定位检索）。
    """
    report_name = _sanitize_report_name(report_name)
    company_name = (company_name or "").strip() if company_name else ""
    code = _normalize_report_code(company_code) if company_code else ""

    root = reports_root(save_dir)
    root.mkdir(parents=True, exist_ok=True)

    if company_name:
        stripped = _strip_company_prefix(report_name, company_name)
        leaf = _sanitize_report_name(stripped) if stripped else report_name
        company_dir = _sanitize_report_name(company_name)
        if code:
            company_dir = f"{company_dir}({code})"
        target = root / company_dir / "财报" / leaf
    else:
        leaf = report_name
        target = root / report_name

    if target.exists():
        if not overwrite:
            raise FileExistsError(
                f"Report directory already exists: {target}（overwrite=False）。"
                "请换一个 report_name 或使用 overwrite=True 覆盖。"
            )
        import shutil

        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    # loader 会在全文最前注入 "# <文档标题>"，拆分章节前去掉该行，
    # 避免 reports/<报告名>/<报告名>/... 的重复嵌套。
    lines = markdown_text.splitlines()
    if strip_doc_title and lines and lines[0].startswith("# "):
        lines = lines[1:]
    md_text = "\n".join(lines)

    if page_count and page_count > 0:
        sections = parse_markdown_to_cleaned_sections(md_text, page_count, file_name=report_name, file_type="md")
    else:
        sections = _split_markdown_by_sections(md_text, file_name=report_name, file_type="md")

    parse_sections_to_folder_structure(sections, target)
    # 保留完整全文副本，便于整体阅读/其它工具直接读文件
    # (target / f"{leaf}.md").write_text(md_text, encoding="utf-8")

    file_count = sum(1 for p in target.rglob("*") if p.is_file())
    if file_count == 0:
        # 没有任何章节（如空文档），至少保留全文副本，保证目录可用
        file_count = 1

    # 写入报告元数据（与 OCR manifest 配套，供 publish/list 等工具读取）
    (target / ".report_manifest.json").write_text(
        json.dumps(
            {
                "report_name": report_name,
                "company_name": company_name,
                "company_code": code,
                "category": "财报",
                "report_period": leaf,
                "section_count": len(sections),
                "file_count": file_count,
                "page_count": page_count,
                "created_at": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return target


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="清洗 markdown 财务报告，去除重复的页眉页脚")
    parser.add_argument("md_path", help="markdown 文件路径")
    parser.add_argument("--page-count", type=int, required=True, help="原始 PDF 页数，用于判定页眉页脚")
    parser.add_argument("--file-type", default="md", help="文件类型标识")
    args = parser.parse_args()

    file_name = args.md_path.split("/")[-1]
    with open(args.md_path, encoding="utf-8") as f:
        md_text = f.read()

    cleaned_sections = parse_markdown_to_cleaned_sections(
        md_text, args.page_count, file_name=file_name, file_type=args.file_type
    )
    parse_sections_to_folder_structure(cleaned_sections, Path("/data/freedom/AlphaBee/reports"))
