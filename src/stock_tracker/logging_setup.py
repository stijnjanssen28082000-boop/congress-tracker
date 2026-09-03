"""Configures logging to both console and a dated file under logs/."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from stock_tracker.config import REPO_ROOT, Config, load_config

_CONFIGURED = False


def setup_logging(config: Config | None = None) -> logging.Logger:
    global _CONFIGURED

    config = config or load_config()
    log_dir = Path(config.logging.get("dir", "logs"))
    if not log_dir.is_absolute():
        log_dir = REPO_ROOT / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, str(config.logging.get("level", "INFO")).upper(), logging.INFO)
    log_file = log_dir / f"{date.today().isoformat()}.log"

    logger = logging.getLogger("stock_tracker")

    if not _CONFIGURED:
        logger.setLevel(level)
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        _CONFIGURED = True

    return logger
