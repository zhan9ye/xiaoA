"""
出站 HTTP（httpx）文件日志：仅当请求主机匹配配置列表（默认 akapi1.com，含 www.akapi1.com）时记录。
正文按长度截断；不做脱敏（日志文件可能含密码、token、助记词等，请限制文件权限并勿外传）。
"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import httpx

from app.settings import settings

_logger: Optional[logging.Logger] = None


def _format_body_for_log(raw: bytes, max_len: int) -> str:
    if not raw:
        return "(empty)"
    total = len(raw)
    truncated = total > max_len
    chunk = raw[:max_len] if truncated else raw
    note = f" [BODY_TRUNCATED total_bytes={total}]" if truncated else ""
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return f"<binary {total} bytes>{note}"
    if not truncated:
        try:
            parsed = json.loads(text)
            return json.dumps(parsed, ensure_ascii=False) + note
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return text + note


def _outbound_host_patterns() -> list[str]:
    raw = (settings.request_log_outbound_hosts or "").strip()
    if not raw:
        return []
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def outbound_host_matches(host: str) -> bool:
    """主机等于某配置项，或为 某配置项 的子域（如 api.ak2018.vip 匹配 ak2018.vip）。"""
    h = (host or "").lower()
    if not h:
        return False
    for p in _outbound_host_patterns():
        if h == p or h.endswith("." + p):
            return True
    return False


def setup_request_file_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    base = Path(settings.request_log_dir or Path(__file__).resolve().parent.parent / "logs")
    base.mkdir(parents=True, exist_ok=True)
    path = base / "http_requests.log"
    lg = logging.getLogger("app.http_requests")
    lg.setLevel(logging.INFO)
    lg.handlers.clear()
    fh = RotatingFileHandler(
        path,
        maxBytes=max(1_048_576, int(settings.request_log_max_bytes)),
        backupCount=max(1, int(settings.request_log_backup_count)),
        encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    lg.addHandler(fh)
    lg.propagate = False
    _logger = lg
    return lg


async def httpx_outbound_response_log_hook(response: httpx.Response) -> None:
    if not settings.request_log_enabled:
        return
    if not _outbound_host_patterns():
        return
    try:
        req = response.request
        host = (req.url.host or "").lower()
    except Exception:
        return
    if not outbound_host_matches(host):
        return

    lg = setup_request_file_logger()
    max_body = max(4096, int(settings.request_log_max_body))

    try:
        await response.aread()
    except Exception:
        pass

    try:
        req_bytes = req.content
    except Exception:
        req_bytes = b""

    req_body = _format_body_for_log(req_bytes, max_body)
    resp_bytes = response.content or b""
    ct = response.headers.get("content-type", "") or ""
    log_resp = _format_body_for_log(resp_bytes, max_body)
    if len(log_resp) > 65536:
        log_resp = log_resp[:65536] + "... [LINE_TRUNCATED]"

    try:
        url_str = str(req.url)
    except Exception:
        url_str = ""

    lg.info(
        "OUTBOUND %s %s | req_body=%s\nRESPONSE status=%s content-type=%s | body=%s",
        req.method,
        url_str,
        req_body,
        response.status_code,
        ct,
        log_resp,
    )
