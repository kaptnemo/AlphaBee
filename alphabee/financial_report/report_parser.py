import re
from collections import Counter
from pathlib import Path

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


def _split_markdown_by_sections(md_text: str, file_name: str | None = None, file_type: str | None = None) -> list[dict]:
    md = MarkdownIt()
    tokens = md.parse(md_text)

    sections: list[dict] = []
    current_section = {
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
    section_stack = []
    section_numbers_stack = []
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
) -> list[dict]:
    sections = _split_markdown_by_sections(md_text, file_name=file_name, file_type=file_type)
    if not sections or page_count <= 0:
        return sections

    title_counts: dict[str, int] = {}
    for section in sections:
        title = section.get("title")
        title_counts[title] = title_counts.get(title, 0) + 1

    header_footer_titles = [title for title, count in title_counts.items() if count / page_count >= 0.6]

    content_lines: dict[str, list[str]] = {}
    for section in sections:
        if section.get("title") in header_footer_titles:
            content_lines.setdefault(section["title"], []).extend(section.get("content", []))

    ngram_dict = {
        title: _ngram_split_lines(lines, n=5, threshold=max(1, title_counts[title] // 2))
        for title, lines in content_lines.items()
    }

    ngram_threshold = 3
    cleaned_sections: list[dict] = []
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


def parse_sections_to_folder_structure(sections: list[dict], save_dir: Path) -> dict:
    """把sections解析成文件夹结构，并生成对应的文件夹和文件"""

    content_parts = {}
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
                print(f"Error writing to file {file_path}: {e}")


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
