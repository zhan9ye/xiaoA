"""经指定 HTTP 代理对 akapi1 Login 做一次探测 POST（固定假账号），用于判别出口 IP 是否被上游拦截。"""

from __future__ import annotations

from typing import Any, Dict

import httpx

from app.services.session_manager import normalize_proxy_url
from app.settings import settings

AKAPI1_LOGIN_URL = "https://www.akapi1.com/RPC/Login"

# 与 curl 探测一致：假账号口令；上游若路由正常应返回账号错误 JSON。
PROBE_ACCOUNT = "你的账号"
PROBE_PASSWORD = "你的密码"

EXPECTED_WRONG_CREDENTIAL_MSG = "賬戶或密碼不正確"

_DEFAULT_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _body_preview(text: str, limit: int = 480) -> str:
    s = (text or "").strip().replace("\r", " ").replace("\n", " ")
    return s[:limit] + ("…" if len(s) > limit else "")


def _is_expected_wrong_credentials_json(payload: Dict[str, Any]) -> bool:
    if payload.get("Error") is not True:
        return False
    msg = str(payload.get("Msg") or "").strip()
    return msg == EXPECTED_WRONG_CREDENTIAL_MSG


async def probe_akapi1_login_via_proxy(proxy_url_raw: str) -> Dict[str, Any]:
    """
    经由 proxy_url_raw 出站 POST Login。
    - HTTP 403：判定代理出口不可用（易被 WAF 拦）。
    - 200 + 指定 JSON：判定代理通路正常。
    """
    pu = normalize_proxy_url(proxy_url_raw)
    if not pu:
        return {
            "proxy_ok": False,
            "verdict": "invalid_proxy_url",
            "verdict_detail": "代理 URL 无效",
            "http_status": 0,
            "body_preview": "",
        }

    timeout = httpx.Timeout(
        settings.rpc_timeout_seconds,
        connect=min(30.0, float(settings.rpc_timeout_seconds) + 5.0),
    )
    data = {
        "account": PROBE_ACCOUNT,
        "password": PROBE_PASSWORD,
        "client": "WEB",
        "v": "2120",
        "lang": "cn",
    }

    try:
        async with httpx.AsyncClient(
            proxy=pu,
            verify=False,
            trust_env=False,
            timeout=timeout,
        ) as client:
            resp = await client.post(
                AKAPI1_LOGIN_URL,
                headers=dict(_DEFAULT_HEADERS),
                data=data,
            )
    except httpx.RequestError as ex:
        return {
            "proxy_ok": False,
            "verdict": "request_error",
            "verdict_detail": str(ex) or repr(ex),
            "http_status": 0,
            "body_preview": "",
        }

    text = resp.text or ""
    preview = _body_preview(text)

    if resp.status_code == 403:
        return {
            "proxy_ok": False,
            "verdict": "http_403",
            "verdict_detail": "上游返回 403，该代理出口不可用",
            "http_status": 403,
            "body_preview": preview,
        }

    if resp.status_code != 200:
        return {
            "proxy_ok": False,
            "verdict": f"http_{resp.status_code}",
            "verdict_detail": f"非预期 HTTP 状态 {resp.status_code}",
            "http_status": resp.status_code,
            "body_preview": preview,
        }

    try:
        payload = resp.json()
    except Exception:
        return {
            "proxy_ok": False,
            "verdict": "not_json",
            "verdict_detail": "200 但响应体不是 JSON",
            "http_status": 200,
            "body_preview": preview,
        }

    if not isinstance(payload, dict):
        return {
            "proxy_ok": False,
            "verdict": "unexpected_json",
            "verdict_detail": "JSON 根节点不是对象",
            "http_status": 200,
            "body_preview": preview,
        }

    if _is_expected_wrong_credentials_json(payload):
        return {
            "proxy_ok": True,
            "verdict": "wrong_password_json",
            "verdict_detail": "收到假账号错误 JSON，代理可用",
            "http_status": 200,
            "body_preview": preview,
        }

    return {
        "proxy_ok": False,
        "verdict": "unexpected_json_payload",
        "verdict_detail": "200 JSON 内容与预期不符",
        "http_status": 200,
        "body_preview": preview,
    }
