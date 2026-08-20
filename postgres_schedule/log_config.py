from logging.handlers import RotatingFileHandler
import logging
import os
import sys
from dotenv import load_dotenv
from pathlib import Path

ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)
LOG_DIRECTORY = Path(
    os.getenv("LOG_DIRECTORY", "logs/tailscale_scheduled_postgres/")
).expanduser()
INFO_LOG_PATH = LOG_DIRECTORY / "info.log"
ERROR_LOG_PATH = LOG_DIRECTORY / "error.log"
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5

DatabasePosition = tuple[int, int]


def configure_logging() -> logging.Logger:
    """Configure separate rotating files for informational and error logs."""
    logger = logging.getLogger("tailscale_scheduled_postgres")
    if logger.handlers:
        return logger

    LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    info_handler = RotatingFileHandler(
        INFO_LOG_PATH,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(formatter)

    error_handler = RotatingFileHandler(
        ERROR_LOG_PATH,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.addHandler(console_handler)
    logger.addHandler(info_handler)
    logger.addHandler(error_handler)
    logger.propagate = False
    return logger


logger = configure_logging()
