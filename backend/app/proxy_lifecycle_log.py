"""代理服务器自动购机、探测、释放等生命周期事件文件日志（默认 backend/logs/proxy_lifecycle.log）。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Mapping, Optional

from app.settings import settings

_logger: Optional[logging.Logger] = None
_proxy_lifecycle_file_handler_ok: bool = False


def proxy_lifecycle_log_file_ok() -> bool:
    return _proxy_lifecycle_file_handler_ok


def setup_proxy_lifecycle_file_logger() -> logging.Logger:
    global _logger, _proxy_lifecycle_file_handler_ok
    if _logger is not None:
        return _logger

    base = Path(settings.request_log_dir or Path(__file__).resolve().parent.parent / "logs")
    path = base / "proxy_lifecycle.log"
    lg = logging.getLogger("app.proxy_lifecycle")
    lg.handlers.clear()
    lg.setLevel(logging.INFO)

    if not bool(settings.proxy_lifecycle_log_enabled):
        lg.addHandler(logging.NullHandler())
        lg.setLevel(logging.CRITICAL)
        lg.propagate = False
        _logger = lg
        return lg

    try:
        base.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            path,
            maxBytes=max(1_048_576, int(settings.request_log_max_bytes)),
            backupCount=max(1, int(settings.request_log_backup_count)),
            encoding="utf-8",
        )
        fh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
        lg.addHandler(fh)
        _proxy_lifecycle_file_handler_ok = True
    except OSError as e:
        _proxy_lifecycle_file_handler_ok = False
        lg.addHandler(logging.NullHandler())
        lg.setLevel(logging.CRITICAL)
        print(
            f"WARNING: 无法写入代理生命周期日志 {path} ({e!r})。"
            f"已跳过文件日志，应用继续启动。请修正目录权限、"
            f"在 .env 设置 REQUEST_LOG_DIR 指向可写目录，或设置 PROXY_LIFECYCLE_LOG_ENABLED=false。",
            file=sys.stderr,
        )

    lg.propagate = False
    _logger = lg
    return lg


def _format_fields(fields: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={value}")
    return " | ".join(parts)


def proxy_lifecycle_log(phase: str, **fields: Any) -> None:
    """写入代理生命周期日志；同时打印到标准输出，便于容器/进程日志检索。"""
    setup_proxy_lifecycle_file_logger()
    line = _format_fields({"phase": phase, **fields})
    print(line, flush=True)
    if _logger is not None and _proxy_lifecycle_file_handler_ok:
        _logger.info(line)
