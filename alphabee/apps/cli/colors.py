"""终端颜色（ANSI，无第三方依赖）。

从根 ``main.py`` 拆出（ENGINEERING_ROADMAP Phase E7）：颜色常量 + 上色/分隔线辅助，
供 renderer / streaming / chat / tasks 复用。颜色开关用 ``set_color_enabled`` 控制，
替代原先 main.py 里的模块级全局 ``_USE_COLOR``（可变全局跨模块是坏味道）。
"""

from __future__ import annotations


class Color:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"
    WHITE = "\033[97m"


_color_enabled = True


def set_color_enabled(enabled: bool) -> None:
    """开关终端颜色（``--no-color`` 或非 tty 时关闭）。"""
    global _color_enabled
    _color_enabled = enabled


def color(text: str, *codes: str) -> str:
    """给文本包裹 ANSI 颜色码；颜色关闭时原样返回。"""
    if not _color_enabled:
        return text
    return "".join(codes) + text + Color.RESET


def hr(char: str = "─", width: int = 70, color_code: str = Color.GRAY) -> str:
    return color(char * width, color_code)
