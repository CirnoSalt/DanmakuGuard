"""日志配置：控制台 + 轮转文件，敏感信息自动脱敏。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.paths import runtime_path

LOG_DIR = runtime_path("logs")
LOG_FILE = LOG_DIR / "app.log"

# 需要脱敏的密文集合（启动时由 main 注册）
_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """注册需要脱敏的密文，长度过短的不注册以避免误伤。"""
    if value and len(value) > 4:
        _secrets.add(value)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in _secrets:
            if s and s in msg:
                msg = msg.replace(s, "***REDACTED***")
        record.msg = msg
        record.args = ()
        return True


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    # 清理可能存在的旧 handler（热重载场景）
    for h in list(root.handlers):
        root.removeHandler(h)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(RedactFilter())

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(RedactFilter())

    root.addHandler(file_handler)
    root.addHandler(console)

    # 降低第三方库噪声
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)

    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
