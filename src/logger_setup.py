"""Rotating file logger shared by every module in the app."""

import logging
import os
from logging.handlers import RotatingFileHandler

LOGGER_NAME = "bin_scan_app"


def setup_logger(log_directory: str, max_bytes: int = 5_242_880, backup_count: int = 5) -> logging.Logger:
    """Create (or return) the shared application logger.

    Writes to a rotating file inside log_directory. If the directory can't be
    created (e.g. permissions, unreachable path at startup), falls back to
    the current working directory so the app never crashes purely because
    logging couldn't be set up.
    """
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        # Already configured (e.g. re-entrant call) - don't add duplicate handlers.
        return logger

    logger.setLevel(logging.DEBUG)

    try:
        os.makedirs(log_directory, exist_ok=True)
        log_path = os.path.join(log_directory, "bin_scan_app.log")
    except OSError:
        fallback_dir = os.getcwd()
        log_path = os.path.join(fallback_dir, "bin_scan_app.log")

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def get_logger() -> logging.Logger:
    """Fetch the already-configured logger from anywhere in the app."""
    return logging.getLogger(LOGGER_NAME)
