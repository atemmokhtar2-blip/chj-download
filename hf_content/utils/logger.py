import logging
import os
from logging.handlers import RotatingFileHandler
from config.settings import LOGS_DIR


def setup_logger(name: str, filename: str, level=logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    fh = RotatingFileHandler(
        os.path.join(LOGS_DIR, filename),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8"
    )
    fh.setFormatter(formatter)

    ch = logging.StreamHandler()
    ch.setFormatter(formatter)

    if not logger.handlers:
        logger.addHandler(fh)
        logger.addHandler(ch)

    return logger


download_logger = setup_logger("downloads", "downloads.log")
error_logger = setup_logger("errors", "errors.log", logging.ERROR)
admin_logger = setup_logger("admin", "admin.log")
system_logger = setup_logger("system", "system.log")
security_logger = setup_logger("security", "security.log")
