"""uvicorn 启动入口。

使用方式：poetry run uvicorn alphabee.apps.web.server:app --host 0.0.0.0 --port 8010
"""

from __future__ import annotations

# 注意：.env 的加载已在父包 alphabee/apps/web/__init__.py 最顶部完成
# （Python import 时父包 __init__ 先于本模块执行），此处无需重复 load_dotenv。
from alphabee.apps.web.app import app

__all__ = ["app"]

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("alphabee.apps.web.server:app", host="0.0.0.0", port=8010, reload=False)
