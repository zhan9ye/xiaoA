import json
from typing import Optional, Tuple

import httpx

from app.schemas import AppConfigIn, LoginResult
from app.services.login_response_parse import merge_from_rpc_login
from app.services.rpc_common import get_rpc_browser_headers
from app.services.session_manager import SessionManager
from app.rpc_v import compute_js_timespan_v
from app.settings import settings


def rpc_login_error_message(login_res: LoginResult) -> str:
    body = (login_res.response_body or "").strip()
    if body:
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                msg = str(data.get("Msg") or "").strip()
                if msg:
                    return msg
        except json.JSONDecodeError:
            pass
    msg = (login_res.message or "").strip()
    return msg or "登录账号或密码不正确"


async def verify_trade_credentials(
    sm: SessionManager,
    account: str,
    password: str,
) -> Tuple[bool, str, LoginResult, AppConfigIn]:
    """调用交易端 Login 校验账号密码；成功时返回合并后的会话字段（Key/UserID 等）。"""
    login_res = await rpc_login(sm, account, password)
    probe = AppConfigIn.model_construct(
        username=(account or "user"),
        password=password or " ",
        key_token="",
        mnemonic="",
        rpc_login_key="",
        rpc_user_id="",
        quantity_start_limit=0,
        request_interval_ms=1000,
        run_period_start="",
        run_period_end="",
        runner_enabled=False,
        sell_start_time="",
        sold_son_ids_json="{}",
        listing_amounts_json="{}",
        sell_sort_field="create_time",
        sell_sort_desc=False,
        main_account_info_json="{}",
    )
    merged, ok = merge_from_rpc_login(probe, login_res.response_body)
    if ok:
        return True, "", login_res, merged
    return False, rpc_login_error_message(login_res), login_res, probe


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
