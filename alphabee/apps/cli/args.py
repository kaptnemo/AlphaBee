"""命令行参数解析（从根 main.py 拆出）。"""

from __future__ import annotations

import argparse


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="AlphaBee — AI 投资分析助手",
    )
    parser.add_argument(
        "query",
        nargs="?",
        default=None,
        help="分析问题；不传则进入多轮对话模式",
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        default=False,
        help="进入多轮对话模式；可与初始问题一起使用",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=False,
        help="禁用终端颜色输出",
    )
    parser.add_argument(
        "--log-dir",
        default="./logs",
        help="日志文件目录（默认: ./logs）",
    )
    parser.add_argument(
        "--enhance",
        action="store_true",
        default=False,
        help="启用 LLM 增强层（跨信号模式识别 + 行业/生命周期语境化 + 用户意图适配）",
    )
    parser.add_argument(
        "--llm-review",
        action="store_true",
        default=False,
        dest="llm_review",
        help="启用 LLM 审查层（定性评估证据充分性 / 信号一致性 / 语境合理性）",
    )
    parser.add_argument(
        "--monitor-framework",
        default=None,
        help="观察框架 Markdown 路径。提供后将进入持续跟踪模式。",
    )
    parser.add_argument(
        "--symbol",
        default=None,
        help="监控模式下的股票代码，例如 300760 或 300760.SZ",
    )
    parser.add_argument(
        "--monitor-periods",
        type=int,
        default=8,
        help="监控模式拉取的财报期数（默认: 8）",
    )
    parser.add_argument(
        "--task-stats",
        action="store_true",
        default=False,
        help="输出最近运行记录的统计摘要",
    )
    parser.add_argument(
        "--distill",
        action="store_true",
        default=False,
        help="基于运行记录产出规则蒸馏建议报告（需 LLM）",
    )
    parser.add_argument(
        "--task-history",
        default=None,
        help="查看指定标的的历史运行记录，如 600519.SH",
    )
    parser.add_argument(
        "--task-record",
        default=None,
        help="查看指定 task_id 的完整运行记录",
    )
    return parser.parse_args()


def normalize_query(query: str) -> str:
    """Strip accidental 'key=value' prefix if user ran: python main.py query=..."""
    if "=" in query and query.index("=") < 20 and not query.startswith("http"):
        key, _, rest = query.partition("=")
        if key.strip().isidentifier():
            return rest.strip()
    return query
