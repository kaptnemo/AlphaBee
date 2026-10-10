"""``tests/agents/driver_profile`` 的环境适配：保证 HOME 可写。

为什么需要：``alphabee.tools.tushare_query`` → ``alphabee.collectors.tushare``
在 **import 期**调用 ``ts.set_token(...)``，而 tushare 的实现是
``pd.DataFrame(...).to_csv(~​/tk.csv)``（``tushare/util/upass.py``）——HOME 不可写时
连 import 都会抛 ``OSError``。本目录的契约测试要 import ``agents.driver_profile.agent``，
故这里先探测 HOME；不可写则把 HOME 指到临时目录（写进 /tmp 而不是工作区）。

HOME 本来就可写的环境（CI / 开发者机器）保持原样，不做任何改动。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _home_is_writable() -> bool:
    home = Path(os.path.expanduser("~"))
    probe = home / ".alphabee_home_write_probe"
    try:
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
    except OSError:
        return False
    return True


if not _home_is_writable():
    _fallback = Path(tempfile.gettempdir()) / "alphabee_pytest_home"
    _fallback.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(_fallback)
