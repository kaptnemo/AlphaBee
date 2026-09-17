"""Allow running the tracking CLI via ``python -m alphabee.tracking``.

与 ``alphabee/data_fetch/__main__.py`` 同模式（薄壳，逻辑全在 ``scheduler.main``）；
差别只有一处：把 ``main`` 的返回码透出为进程退出码（``0`` 成功 / ``2`` 用法错误），
便于外部调度器（cron / 任务队列）据退出码告警。
"""

from alphabee.tracking.scheduler import main

raise SystemExit(main())
