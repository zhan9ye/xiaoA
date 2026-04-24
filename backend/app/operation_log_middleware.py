"""记录控制台用户对 /api/ 的 HTTP 操作（method、path、脱敏参数、成功/失败简述）。
不记录：health、操作日志查询、GET /api/run/status、GET /api/config、GET /api/config/run-params、GET /api/credits/overview（只读拉配置/概览）、
全部 /api/admin/（管理端）、携带管理员 JWT 的请求、以及对交易站出站的接口（/api/trade/*、/api/external/*、/api/subaccounts*，
与 http_requests 等出站日志重叠）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs

import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.auth_jwt import ADMIN_TOKEN_TYP
from app.db import AsyncSessionLocal
from app.models import UserOperationLog
from app.operation_log_summary import business_summary_for_request
from app.settings import settings

_log = logging.getLogger(__name__)

_MAX_BODY_READ = 65_536
_MAX_PARAMS_JSON = 12_000
_MAX_FAILURE_REASON = 512


def _sensitive_key(name: str) -> bool:
    n = (name or "").lower()
    for frag in (
        "password",
        "token",
        "secret",
        "mnemonic",
        "gcode",
        "g_code",
        "authorization",
        "credential",
        "key_token",
        "rpc_login",
    ):
        if frag in n:
            return True
    return False


def sanitize_params(obj: Any, depth: int = 0) -> Any:
    """递归脱敏，用于写入操作日志。"""
    if depth > 24:
        return "<max_depth>"
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if _sensitive_key(str(k)):
                out[str(k)] = "***"
            else:
                out[str(k)] = sanitize_params(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [sanitize_params(x, depth + 1) for x in obj[:200]]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)[:500]


def _parse_query_dict(query_string: str) -> Dict[str, Any]:
    if not (query_string or "").strip():
        return {}
    raw = parse_qs(query_string, keep_blank_values=True)
    out: Dict[str, Any] = {}
    for k, vals in raw.items():
        if not vals:
            out[k] = ""
        elif len(vals) == 1:
            out[k] = vals[0]
        else:
            out[k] = vals
    return sanitize_params(out)  # type: ignore[return-value]


def _parse_json_body(raw: bytes) -> Optional[Any]:
    if not raw or len(raw) > _MAX_BODY_READ:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeError):
        return None


def build_params_payload(request: Request, body_bytes: bytes) -> str:
    parts: Dict[str, Any] = {}
    qd = _parse_query_dict(request.url.query)
    if qd:
        parts["query"] = qd
    if body_bytes and request.method in ("POST", "PUT", "PATCH"):
        ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if "application/json" in ct or ct == "":
            parsed = _parse_json_body(body_bytes)
            if parsed is not None:
                parts["body"] = sanitize_params(parsed)
            else:
                parts["body"] = f"<non-json or large, len={len(body_bytes)}>"
        elif "application/x-www-form-urlencoded" in ct:
            txt = body_bytes[:_MAX_BODY_READ].decode("utf-8", errors="replace")
            fd = _parse_query_dict(txt)
            if fd:
                parts["body"] = fd
            else:
                parts["body"] = "<empty form>"
        else:
            parts["body"] = f"<content-type {ct!r}, len={len(body_bytes)}>"
    try:
        s = json.dumps(parts, ensure_ascii=False, default=str)
    except TypeError:
        s = "{}"
    if len(s) > _MAX_PARAMS_JSON:
        s = s[: _MAX_PARAMS_JSON - 30] + "…[truncated]"
    return s


def decode_actor_from_authorization(header_val: Optional[str]) -> Tuple[Optional[int], bool]:
    """(platform_user_id or None, is_admin_jwt)。"""
    if not header_val or not header_val.lower().startswith("bearer "):
        return None, False
    token = header_val[7:].strip()
    if not token:
        return None, False
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None, False
    if payload.get("typ") == ADMIN_TOKEN_TYP:
        return None, True
    try:
        return int(payload.get("sub")), False
    except (TypeError, ValueError):
        return None, False


def parse_failure_reason(status_code: int, body: bytes) -> str:
    base = f"HTTP {status_code}"
    if not body or len(body) > 8192:
        return base[:_MAX_FAILURE_REASON]
    try:
        j = json.loads(body.decode("utf-8", errors="replace"))
        d = j.get("detail")
        if isinstance(d, str) and d.strip():
            msg = d.strip()
        elif isinstance(d, list) and d:
            msg = "; ".join(str(x) for x in d[:8])
        else:
            msg = str(d) if d is not None else ""
        msg = (msg or base)[:400]
        return f"{base}: {msg}"[:_MAX_FAILURE_REASON]
    except (json.JSONDecodeError, TypeError, UnicodeError):
        tail = body.decode("utf-8", errors="replace")[:200].replace("\n", " ")
        return f"{base}: {tail}"[:_MAX_FAILURE_REASON]


async def _persist_operation_log(
    *,
    user_id: Optional[int],
    is_admin_action: bool,
    method: str,
    path: str,
    business_summary: str,
    params_json: str,
    success: bool,
    failure_reason: Optional[str],
) -> None:
    try:
        async with AsyncSessionLocal() as session:
            row = UserOperationLog(
                user_id=user_id,
                is_admin_action=is_admin_action,
                method=method[:16],
                path=path[:512],
                business_summary=(business_summary or "")[:256],
                params_json=params_json or "{}",
                success=success,
                failure_reason=(failure_reason or None)[:_MAX_FAILURE_REASON] if failure_reason else None,
            )
            session.add(row)
            await session.commit()
    except Exception as e:
        _log.warning("operation log persist failed: %s", e, exc_info=False)


def schedule_operation_log(
    *,
    user_id: Optional[int],
    is_admin_action: bool,
    method: str,
    path: str,
    business_summary: str,
    params_json: str,
    status_code: int,
    failure_body: Optional[bytes],
) -> None:
    ok = 200 <= int(status_code) < 300
    reason: Optional[str] = None
    if not ok:
        reason = parse_failure_reason(int(status_code), failure_body or b"")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(
        _persist_operation_log(
            user_id=user_id,
            is_admin_action=is_admin_action,
            method=method,
            path=path,
            business_summary=business_summary,
            params_json=params_json,
            success=ok,
            failure_reason=reason,
        )
    )


class OperationLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or path == "/api/health" or not path.startswith("/api/"):
            return await call_next(request)
        # 避免「查日志」本身产生无限可写记录（轮询）
        if path.startswith("/api/operation-logs") or path.startswith("/api/admin/operation-logs"):
            return await call_next(request)
        # 前端高频轮询或只读拉交易配置/运行参数，不写操作日志
        if request.method.upper() == "GET":
            if path == "/api/run/status":
                return await call_next(request)
            if path in ("/api/config", "/api/config/run-params", "/api/credits/overview"):
                return await call_next(request)
        # 管理端接口一律不写操作日志（含管理员登录等）
        if path.startswith("/api/admin/"):
            return await call_next(request)
        # 对 akapi 等出站 RPC（已由 request_log / http_requests 等记录），不写操作日志
        if (
            path.startswith("/api/trade/")
            or path.startswith("/api/external/")
            or path.startswith("/api/subaccounts")
        ):
            return await call_next(request)

        body_bytes = b""
        if request.method in ("POST", "PUT", "PATCH"):
            body_bytes = await request.body()

            async def receive():
                return {"type": "http.request", "body": body_bytes, "more_body": False}

            request = Request(request.scope, receive)

        auth = request.headers.get("authorization")
        uid, is_admin = decode_actor_from_authorization(auth)
        if is_admin:
            return await call_next(request)

        params_json = build_params_payload(request, body_bytes)
        biz = business_summary_for_request(request.method, path)

        response = await call_next(request)
        code = int(response.status_code)
        if 200 <= code < 300:
            schedule_operation_log(
                user_id=uid,
                is_admin_action=False,
                method=request.method,
                path=path,
                business_summary=biz,
                params_json=params_json,
                status_code=code,
                failure_body=None,
            )
            return response

        body_out = b""
        async for chunk in response.body_iterator:
            body_out += chunk
        schedule_operation_log(
            user_id=uid,
            is_admin_action=False,
            method=request.method,
            path=path,
            business_summary=biz,
            params_json=params_json,
            status_code=code,
            failure_body=body_out,
        )
        return Response(
            content=body_out,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
