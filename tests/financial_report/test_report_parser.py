import json
from pathlib import Path

from alphabee.financial_report import report_cache
from alphabee.financial_report.report_parser import (
    _ngram_split,
    _ngram_split_lines,
    _split_markdown_by_sections,
    parse_markdown_to_cleaned_sections,
    write_markdown_report_folder,
)


class TestNgramSplit:
    def test_short_text_returns_whole_text(self):
        assert _ngram_split("abc", 3) == ["abc"]

    def test_short_text_shorter_than_n(self):
        assert _ngram_split("ab", 3) == ["ab"]

    def test_overlapping_ngrams(self):
        assert _ngram_split("abcd", 3) == ["abc", "bcd"]

    def test_split_lines_keeps_repeated_ngrams(self):
        line = "上海临港控股股份有限公司"
        lines = [line, line, line]
        ngrams = _ngram_split_lines(lines, n=5, threshold=2)
        assert "上海临港控" in ngrams
        assert len(ngrams) == len(_ngram_split(line, 5))

    def test_split_lines_filters_rare_ngrams(self):
        lines = ["abcdef", "ghijkl"]
        ngrams = _ngram_split_lines(lines, n=3, threshold=1)
        assert ngrams == []


class TestSplitMarkdownBySections:
    def test_splits_by_headings(self):
        md = "# 第一章\n内容一。\n# 第二章\n内容二。"
        sections = _split_markdown_by_sections(md, "a.md", "md")
        assert [s["title"] for s in sections] == ["第一章", "第二章"]
        assert sections[0]["content"] == ["内容一。"]
        assert sections[1]["content"] == ["内容二。"]

    def test_preamble_becomes_qianyan(self):
        md = "正文开头。\n# 第一章\n内容。"
        sections = _split_markdown_by_sections(md, "a.md", "md")
        assert sections[0]["title"] == "前言"
        assert sections[0]["level"] == 0
        assert sections[0]["content"] == ["正文开头。"]

    def test_numbered_heading_path(self):
        md = "# 1. 公司简介\n内容。"
        sections = _split_markdown_by_sections(md, "a.md", "md")
        section = sections[0]
        assert section["metadata"]["section_path"] == ["公司简介"]
        assert section["metadata"]["section_path_nums"] == ["1"]

    def test_nested_heading_path(self):
        md = "# 1. 公司简介\n内容。\n## 1.1 主要业务\n主营显示面板业务。"
        sections = _split_markdown_by_sections(md, "a.md", "md")
        assert sections[1]["metadata"]["section_path"] == ["公司简介", "主要业务"]
        assert sections[1]["metadata"]["section_path_nums"] == ["1", "1.1"]

    def test_multiline_body_preserved_as_single_line(self):
        md = "# 公司简介\n公司简介\n内容。"
        sections = _split_markdown_by_sections(md, "a.md", "md")
        assert sections[0]["content"] == ["公司简介\n内容。"]

    def test_metadata_source_and_type(self):
        sections = _split_markdown_by_sections("# 标题\n内容。", "a.md", "md")
        assert sections[0]["metadata"]["source"] == "a.md"
        assert sections[0]["metadata"]["file_type"] == "md"


class TestParseMarkdownToCleanedSections:
    def _build_report(self, header_count: int, header_title: str = "江西沃格光电集团股份有限公司") -> str:
        lines = ["# 前言", "本报告正文介绍。", ""]
        for _ in range(header_count):
            lines += [f"# {header_title}", header_title, ""]
        lines += ["# 第一节 公司简介", "公司主营业务说明。", "# 第二节 经营情况", "收入增长。"]
        return "\n".join(lines)

    def test_repeated_header_removed(self):
        header_title = "江西沃格光电集团股份有限公司"
        md = self._build_report(header_count=20, header_title=header_title)
        cleaned = parse_markdown_to_cleaned_sections(md, page_count=30, file_name="a.md", file_type="md")
        titles = [s["title"] for s in cleaned]
        assert titles == ["前言", "第一节 公司简介", "第二节 经营情况"]
        assert all(header_title not in s["content"] for s in cleaned)

    def test_page_count_non_positive_skips_cleanup(self):
        md = self._build_report(header_count=20)
        sections = _split_markdown_by_sections(md, "a.md", "md")
        assert parse_markdown_to_cleaned_sections(md, page_count=0, file_name="a.md", file_type="md") == sections
        assert parse_markdown_to_cleaned_sections(md, page_count=-1, file_name="a.md", file_type="md") == sections

    def test_below_ratio_title_not_treated_as_header(self):
        header_title = "江西沃格光电集团股份有限公司"
        md = self._build_report(header_count=2, header_title=header_title)
        cleaned = parse_markdown_to_cleaned_sections(md, page_count=30, file_name="a.md", file_type="md")
        titles = [s["title"] for s in cleaned]
        assert titles == ["前言", header_title, header_title, "第一节 公司简介", "第二节 经营情况"]

    def test_first_section_header_content_not_lost(self):
        md = (
            "# 江西沃格光电集团股份有限公司\n"
            "第一页的独立说明文字\n"
            "# 江西沃格光电集团股份有限公司\n"
            "江西沃格光电集团股份有限公司\n"
            "# 江西沃格光电集团股份有限公司\n"
            "江西沃格光电集团股份有限公司\n"
            "# 第一节 公司简介\n"
            "正文内容。"
        )
        cleaned = parse_markdown_to_cleaned_sections(md, page_count=3, file_name="a.md", file_type="md")
        assert cleaned[0]["title"] == "江西沃格光电集团股份有限公司"
        assert cleaned[0]["content"] == ["第一页的独立说明文字"]
        assert cleaned[1]["title"] == "第一节 公司简介"


class TestWriteMarkdownReportFolder:
    def test_writes_folder_manifest_and_invalidates_index_cache(self, tmp_path: Path) -> None:
        report_cache.clear_report_caches()
        save_dir = tmp_path / "reports"
        markdown = "# 第一节 公司简介\n公司主营业务。\n# 第二节 财务数据\n营业收入 1 亿元。\n"

        target = write_markdown_report_folder(
            markdown,
            "某公司：2025年年度报告",
            company_name="某公司",
            company_code="600000",
            save_dir=save_dir,
        )

        # 返回类型与目录结构保持原契约
        assert isinstance(target, Path)
        assert target.is_dir()
        assert target == save_dir.resolve() / "某公司(600000)" / "财报" / "2025年年度报告"

        manifest = json.loads((target / ".report_manifest.json").read_text(encoding="utf-8"))
        assert set(manifest) == {
            "report_name",
            "company_name",
            "company_code",
            "category",
            "report_period",
            "section_count",
            "file_count",
            "full_text_path",
            "page_count",
            "created_at",
        }
        assert manifest["report_name"] == "某公司：2025年年度报告"
        assert manifest["company_name"] == "某公司"
        assert manifest["company_code"] == "600000"
        assert manifest["category"] == "财报"
        assert manifest["report_period"] == "2025年年度报告"
        assert manifest["file_count"] == len(list(target.rglob("*.md")))

        # 写入后 index 可正常构建
        first_index = report_cache.get_report_index(target)
        assert first_index.total_files == manifest["file_count"]
        assert report_cache.stats()["entries"] == 1

        # 重写同一份报告：返回前失效该 root 的 index 缓存
        rewritten = write_markdown_report_folder(
            markdown + "# 第三节 其他重要事项\n无。\n",
            "某公司：2025年年度报告",
            company_name="某公司",
            company_code="600000",
            save_dir=save_dir,
        )
        assert rewritten == target
        assert report_cache.stats()["entries"] == 0

        second_index = report_cache.get_report_index(target)
        assert second_index.total_files == len(list(target.rglob("*.md")))
        assert second_index.total_files == first_index.total_files + 1
        report_cache.clear_report_caches()
