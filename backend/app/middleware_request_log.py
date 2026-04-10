"""
出站 HTTP（httpx）文件日志：仅当请求主机匹配配置列表（默认 akapi1.com，含 www.akapi1.com）时记录。
每条带 platform_user_id（控制台用户）。请求体仍记录；响应体仅在失败时记录（HTTP 非 2xx 或 JSON Error=true），成功写 (omitted, success)。
正文按长度截断；不做脱敏（日志文件可能含密码、token、助记词等，请限制文件权限并勿外传）。
"""

from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import httpx

from app.settings import settings

_logger: Optional[logging.Logger] = None
# True 仅当 RotatingFileHandler 已成功挂载；否则出站日志不写文件（避免无权限时拖垮进程启动）
_http_request_file_handler_ok: bool = False


def http_request_log_file_ok() -> bool:
    return _http_request_file_handler_ok


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


def _should_log_response_body(status_code: int, resp_bytes: bytes) -> bool:
    """
    失败才落响应正文：HTTP 非 2xx，或 2xx 且 JSON 中 Error=true（上游业务失败）。
    """
    if not (200 <= int(status_code) < 300):
        return True
    raw = resp_bytes or b""
    if not raw.strip():
        return False
    try:
        text = raw.decode("utf-8", errors="replace")
        parsed = json.loads(text)
        if isinstance(parsed, dict) and parsed.get("Error") is True:
            return True
    except (json.JSONDecodeError, TypeError, ValueError, UnicodeError):
        pass
    return False


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
    global _logger, _http_request_file_handler_ok
    if _logger is not None:
        return _logger

    base = Path(settings.request_log_dir or Path(__file__).resolve().parent.parent / "logs")
    path = base / "http_requests.log"
    lg = logging.getLogger("app.http_requests")
    lg.handlers.clear()
    lg.setLevel(logging.INFO)

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
        _http_request_file_handler_ok = True
    except OSError as e:
        _http_request_file_handler_ok = False
        lg.addHandler(logging.NullHandler())
        lg.setLevel(logging.CRITICAL)
        print(
            f"WARNING: 无法写入出站 HTTP 日志文件 {path} ({e!r})。"
            f"已跳过文件日志，应用继续启动。请修正目录权限、"
            f"在 .env 设置 REQUEST_LOG_DIR 指向可写目录，或设置 REQUEST_LOG_ENABLED=false。",
            file=sys.stderr,
        )

    lg.propagate = False
    _logger = lg
    return lg


async def httpx_outbound_response_log_hook(
    response: httpx.Response,
    *,
    platform_user_id: Optional[int] = None,
) -> None:
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

    setup_request_file_logger()
    if not _http_request_file_handler_ok or _logger is None:
        return

    lg = _logger
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
    code = int(response.status_code)
    log_fail = _should_log_response_body(code, resp_bytes)
    if log_fail:
        log_resp = _format_body_for_log(resp_bytes, max_body)
        if len(log_resp) > 65536:
            log_resp = log_resp[:65536] + "... [LINE_TRUNCATED]"
    else:
        log_resp = "(omitted, success)"

    try:
        url_str = str(req.url)
    except Exception:
        url_str = ""

    who = f"platform_user_id={platform_user_id}" if platform_user_id is not None else "platform_user_id=unknown"

    lg.info(
        "%s | OUTBOUND %s %s | req_body=%s\nRESPONSE status=%s content-type=%s | body=%s",
        who,
        req.method,
        url_str,
        req_body,
        code,
        ct,
        log_resp,
    )
