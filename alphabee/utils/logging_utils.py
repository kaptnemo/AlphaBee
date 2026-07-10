import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from typing import Any

import structlog

from pathlib import Path
from alphabee.utils.paths import PROJECT_ROOT

_IS_CONFIGURED = False


def configure_logging(
    log_dir: Path | None = None,
    file_name: str = "alpha_arena.log",
    level: int = logging.INFO,
) -> None:
    """Configure structlog for console and daily rotating file logging."""
    global _IS_CONFIGURED
    if _IS_CONFIGURED:
        return

    resolved_log_dir = log_dir or (PROJECT_ROOT / "logs")
    resolved_log_dir.mkdir(parents=True, exist_ok=True)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=False)
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        timestamper,
    ]

    console_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(colors=False),
        foreign_pre_chain=shared_processors,
    )
    file_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_formatter)

    file_handler = TimedRotatingFileHandler(
        filename=str(resolved_log_dir / file_name),
        when="midnight",
        interval=1,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(file_formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            timestamper,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    _IS_CONFIGURED = True


def get_logger(name: str):
    configure_logging()
    return structlog.get_logger(name)
