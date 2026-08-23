"""AlphaBee Agent — Task Entry Point

Usage:
    python main.py "帮我分析一下宁德时代的投资价值"
    python main.py --enhance "分析 600519.SH"          # 启用 LLM 增强层
    python main.py --llm-review --enhance "分析比亚迪"  # 全开
    python main.py                                      # 进入多轮对话模式
    python main.py --chat                               # 强制进入多轮对话模式
    python main.py --no-color                           # 禁用终端颜色
    python main.py --log-dir ./logs                     # 指定日志目录

本文件是薄入口：业务逻辑已按 ENGINEERING_ROADMAP Phase E7 拆分到 ``alphabee.apps.cli``
（colors / renderer / parsing / streaming / chat / args / tasks / main）。这里只做两件事：
1. ``load_dotenv()``（必须在 import alphabee 之前，保证 ``.env`` 里的 LLM/Tushare 等环境变量生效）；
2. 转发 ``alphabee.apps.cli.main.main()``。
"""

from dotenv import load_dotenv

load_dotenv()

from alphabee.apps.cli.main import main

if __name__ == "__main__":
    main()
