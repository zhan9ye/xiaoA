import json
from typing import Optional

import httpx

from app.schemas import LoginResult
from app.services.rpc_common import get_rpc_browser_headers
from app.services.session_manager import SessionManager
from app.rpc_v import compute_js_timespan_v
from app.settings import settings


async def rpc_login(
    sm: SessionManager,
    account: str,
    password: str,
    v: Optional[str] = None,
) -> LoginResult:
    """
    仅使用账号、密码登录；client/v/lang 为站点常见固定参数，非用户密钥。
    key / UserID 等留给后续抓包后的接口层再带。
    """
    ver = (v or "").strip() or compute_js_timespan_v()
    await sm.reset()
    client = await sm.client()
    data = {
        "account": account,
        "password": password,
        "client": "WEB",
        "v": ver,
        "lang": "cn",
    }
    try:
        r = await client.post(settings.login_url, headers=get_rpc_browser_headers(), data=data)
    except httpx.RequestError as e:
        return LoginResult(ok=False, status_code=0, message=str(e), cookies={}, response_body="")

    cookies = dict(client.cookies)
    body_full = ""
    try:
        body_full = json.dumps(r.json(), ensure_ascii=False, indent=2)
    except ValueError:
        body_full = r.text or ""

    preview = body_full[:500] + ("…" if len(body_full) > 500 else "")
    ok = r.is_success
    msg = f"HTTP {r.status_code} {preview}"
    return LoginResult(
        ok=ok,
        status_code=r.status_code,
        message=msg,
        cookies=cookies,
        response_body=body_full,
    )
