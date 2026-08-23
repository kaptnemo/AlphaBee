"""AlphaBee CLI 包（ENGINEERING_ROADMAP Phase E7 拆分）。

把根 ``main.py`` 的 1461 行单体拆为按职责划分的模块：

- ``colors``     终端颜色（ANSI）
- ``renderer``   终端渲染/打印（各节点进度 + 最终报告）
- ``parsing``    消息/工具/命名空间/报告负载的解析辅助
- ``streaming``  单次查询的流式执行（``run_query``）
- ``chat``       多轮对话（``run_chat_session``）
- ``args``       命令行参数解析
- ``tasks``      task records 相关 CLI
- ``main``       ``main()`` 入口（编排以上模块）

根目录 ``main.py`` 退化为薄入口，仅做 ``load_dotenv`` + 转发 ``main()``。
"""

from alphabee.apps.cli.main import main

__all__ = ["main"]
