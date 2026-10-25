from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from suite_core.diagnostic_io import rotating_handler


def configure_logging(root: str | Path, *, name: str = 'tagmanager') -> logging.Logger:
    root_path = Path(root)
    log_dir = root_path / 'logs'
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    target=(log_dir / 'tagmanager.log').resolve()
    existing=next(
        (
            handler
            for handler in logger.handlers
            if isinstance(handler,RotatingFileHandler)
            and Path(getattr(handler,"baseFilename","")).resolve()==target
        ),
        None,
    )
    if existing is None:
        handler = rotating_handler(target, max_bytes=2_000_000, backups=5)
        if handler is not None:
            handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))
            logger.addHandler(handler)
        elif not logger.handlers:
            logger.addHandler(logging.NullHandler())
    logger.propagate=False
    return logger
