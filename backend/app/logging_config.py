import logging
from logging.config import dictConfig
from pathlib import Path

from app.config import get_settings

_LOGGING_CONFIGURED = False


def setup_logging() -> None:
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return

    settings = get_settings()
    level = settings.log_level.upper()

    handlers: dict[str, dict] = {
        "console": {
            "class": "logging.StreamHandler",
            "level": level,
            "formatter": "standard",
            "stream": "ext://sys.stdout",
        }
    }
    root_handlers = ["console"]

    if settings.log_to_file:
        log_file = Path(settings.log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "level": level,
            "formatter": "standard",
            "filename": str(log_file),
            "maxBytes": settings.log_file_max_bytes,
            "backupCount": settings.log_file_backup_count,
            "encoding": "utf-8",
        }
        root_handlers.append("file")

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
                }
            },
            "handlers": handlers,
            "root": {
                "level": level,
                "handlers": root_handlers,
            },
        }
    )

    _LOGGING_CONFIGURED = True
    logging.getLogger(__name__).info(
        "Logging configured. level=%s file=%s",
        level,
        settings.log_file if settings.log_to_file else "disabled",
    )
