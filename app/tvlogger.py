# tvlogger.py
import logging
import os
from logging.handlers import RotatingFileHandler

LOG_DIR = "../logs"
LOG_FILE = "tradingview_algo.log"

# Ensure logs folder exists
os.makedirs(LOG_DIR, exist_ok=True)

def get_logger(name: str = "tv-algo"):
    """
    Shared logger used by ALL Python files.
    Automatically handles:
    - Rotating log files
    - Console output (optional)
    - Consistent format
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger  # Avoid adding handlers twice
    log_level = os.getenv("LOG_LEVEL").upper()
    logger.setLevel(log_level)

    log_path = os.path.join(LOG_DIR, LOG_FILE)

    # Rotating file handler (5MB per file, keep last 5 files)
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    ))

    # Console handler (DEBUG only)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    ))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger
