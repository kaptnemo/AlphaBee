#!/usr/bin/env python3
"""自动生成 docsify 的 ``_sidebar.md`` 与 ``README.md``。

扫描 ``docs/`` 下的 ``*.md`` 文档，读取每个文件的第一个一级标题（``# ...``）
作为显示名称，按分组规则归类后生成侧边栏与首页索引，无需手工维护。

用法::

    python scripts/gen_docs_index.py            # 重新生成 _sidebar.md 与 README.md
    python scripts/gen_docs_index.py --check    # 仅检查是否有变化（用于 CI / pre-commit）
    python scripts/gen_docs_index.py --watch    # 监听 docs/ 变化并自动重新生成
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

# 生成时忽略的文件（README.md 本身就是生成产物；下划线开头的是 docsify 约定文件）
IGNORE = {"README.md", "_sidebar.md", "_navbar.md", "_coverpage.md"}

# 分组规则：按顺序判定，命中第一个组。
#   name_keywords  —— 只对「文件名」做大小写不敏感匹配
#   title_keywords —— 对「标题」做大小写不敏感匹配
# 未命中任何组的文档归入 FALLBACK_GROUP，确保新增文档不会被遗漏。
GROUP_RULES: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    ("路线图", ("roadmap",), ()),
    ("行业语境设计", ("industry",), ("行业", "语境", "产业")),
]
FALLBACK_GROUP = "专项设计"

BANNER = "<!-- 本文件由 scripts/gen_docs_index.py 自动生成，请勿手动编辑。 -->"

README_HEADER = "# AlphaBee Docs\n\n> AlphaBee 多智能体投资分析系统的设计与路线图文档站点。"


def extract_title(md_path: Path) -> str:
    """取文件第一个一级标题作为显示名，没有则退回文件名。"""
    text = md_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        m = re.match(r"^#\s+(.*)$", line)
        if m:
            return m.group(1).strip()
    return md_path.stem


def classify(name: str, title: str) -> str:
    name_l = name.lower()
    title_l = title.lower()
    for group, name_kw, title_kw in GROUP_RULES:
        if any(k in name_l for k in name_kw) or any(k in title_l for k in title_kw):
            return group
    return FALLBACK_GROUP


def collect_docs() -> dict[str, list[tuple[str, str]]]:
    """返回 {组名: [(显示名, 相对路径), ...]}，组内按文件名排序，组顺序按 GROUP_RULES。"""
    collected: dict[str, list[tuple[str, str]]] = {}
    for md in sorted(DOCS.glob("*.md"), key=lambda p: p.name.lower()):
        if md.name in IGNORE or md.name.startswith("_"):
            continue
        title = extract_title(md)
        group = classify(md.name, title)
        collected.setdefault(group, []).append((title, md.name))

    order = [g for g, _, _ in GROUP_RULES] + [FALLBACK_GROUP]
    return {g: collected[g] for g in order if g in collected}


def render_sidebar(groups: dict[str, list[tuple[str, str]]]) -> str:
    parts = [BANNER, "", "- 概览", "  - [首页](/)", ""]
    for group in groups:
        parts.append(f"- {group}")
        for title, rel in groups[group]:
            parts.append(f"  - [{title}]({rel})")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def render_readme(groups: dict[str, list[tuple[str, str]]]) -> str:
    parts = [README_HEADER, "", BANNER, "", "## 文档目录", ""]
    for group in groups:
        parts.append(f"### {group}")
        parts.append("")
        for title, rel in groups[group]:
            parts.append(f"- [{title}]({rel})")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def regenerate() -> str:
    """生成两个文件并返回结果描述。"""
    groups = collect_docs()
    (DOCS / "_sidebar.md").write_text(render_sidebar(groups), encoding="utf-8")
    (DOCS / "README.md").write_text(render_readme(groups), encoding="utf-8")
    total = sum(len(v) for v in groups.values())
    return f"已生成 _sidebar.md 与 README.md（{total} 篇文档，{len(groups)} 个分组）"


def snapshot() -> dict[str, int]:
    """对源文档（不含生成产物）取 mtime_ns 快照，用于监听。"""
    snap: dict[str, int] = {}
    for md in sorted(DOCS.glob("*.md")):
        if md.name in IGNORE or md.name.startswith("_"):
            continue
        snap[md.name] = md.stat().st_mtime_ns
    return snap


def check_only() -> int:
    """--check：内容有差异则返回非 0，不落盘。"""
    groups = collect_docs()
    pending = {
        "_sidebar.md": render_sidebar(groups),
        "README.md": render_readme(groups),
    }
    dirty = []
    for name, rendered in pending.items():
        path = DOCS / name
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if rendered != current:
            dirty.append(name)
    if dirty:
        print("以下文件已过期，请运行 `python scripts/gen_docs_index.py`：" + ", ".join(dirty))
        return 1
    print("_sidebar.md 与 README.md 均为最新。")
    return 0


def watch() -> None:
    print(f"监听 {DOCS} 下的源文档变化（Ctrl+C 退出）…")
    last = snapshot()
    while True:
        time.sleep(2)
        cur = snapshot()
        if cur != last:
            last = cur
            print(f"[{time.strftime('%H:%M:%S')}] " + regenerate())


def main(argv: list[str]) -> int:
    if "--check" in argv:
        return check_only()
    if "--watch" in argv:
        watch()
        return 0
    print(regenerate())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
