import json
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

from app.middleware_request_log import (
    clear_outbound_request_markers,
    log_httpx_outbound_request_error_sync,
    mark_outbound_request_start,
)
from app.services.rpc_common import get_rpc_browser_headers
from app.services.selling_eligibility import ace_sell_rpc_son_id
from app.services.session_manager import SessionManager
from app.settings import settings


def _ace_amount_str_from_row(row: Dict[str, Any]) -> Optional[str]:
    for amt_key in ("AceAmount", "ACEAmount", "aceAmount", "Ace_Count", "Count"):
        av = row.get(amt_key)
        if av is not None and str(av).strip():
            return str(av).strip()
    return None


def resolve_count_from_subaccounts(items: List[Dict[str, Any]], son_id: str) -> Optional[str]:
    """在子账号缓存中按子账号 id 查找数量类字段（如 AceAmount）；son_id 为空时解析主账户股数。"""
    target = str(son_id).strip()
    if not target:
        for row in items:
            if ace_sell_rpc_son_id(row):
                continue
            got = _ace_amount_str_from_row(row)
            if got:
                return got
        return None
    for row in items:
        for sid_key in ("SonId", "sonId", "Id", "ID", "SubAccountId", "SubId"):
            v = row.get(sid_key)
            if v is not None and str(v).strip() == target:
                return _ace_amount_str_from_row(row)
    return None


async def post_ace_sell_son(
    sm: SessionManager,
    *,
    amount: str,
    password: str,
    son_id: str,
    mnemonic_id1: str,
    mnemonic_key: str,
    mnemonic_str1: str,
    g_code: str,
    count: str,
    rpc_key: str,
    user_id: str,
    v: str,
    lang: str = "cn",
    proxy_pin_index: Optional[int] = None,
    proxy_url_override: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
) -> Tuple[bool, int, Any, str]:
    """
    POST ACE_Sell_Son / ACE_Sell（application/x-www-form-urlencoded）。
    sonId 为空时走主账户接口 ACE_Sell；非空走子账户接口 ACE_Sell_Son。
    须在已 Login 的同一会话 client 上调用以携带 Cookie。

    proxy_url_override：共享池借出的单条代理；用临时 client 携带 sm 的 Cookie 出站。
    timeout_seconds：覆盖单次 POST 超时（开门探测用短超时；超时抛出由调用方捕获）。
    """
    from app.services.session_manager import normalize_proxy_url

    son = str(son_id).strip()
    target_url = settings.ace_sell_main_url if not son else settings.ace_sell_son_url
    data = {
        "amount": str(amount),
        "password": str(password),
        "sonId": son,
        "mnemonicid1": str(mnemonic_id1),
        "mnemonickey": str(mnemonic_key),
        "mnemonicstr1": str(mnemonic_str1),
        "gCode": str(g_code),
        "count": str(count),
        "key": str(rpc_key),
        "UserID": str(user_id),
        "v": str(v),
        "lang": str(lang),
    }

    override = normalize_proxy_url(proxy_url_override)
    if override:
        return await _post_ace_via_override_proxy(
            sm,
            target_url=target_url,
            data=data,
            proxy_url=override,
            timeout_seconds=timeout_seconds,
        )

    client = await sm.client()
    pin_token = None
    if proxy_pin_index is not None and sm.outbound_proxy_count() > 0:
        pin_token = SessionManager.pinned_proxy_index(proxy_pin_index)
    try:
        try:
            if not sm.uses_multi_proxy_dispatch():
                mark_outbound_request_start()
            post_kw: Dict[str, Any] = {
                "headers": get_rpc_browser_headers(),
                "data": data,
            }
            if timeout_seconds is not None:
                post_kw["timeout"] = float(timeout_seconds)
            r = await client.post(target_url, **post_kw)
        except httpx.TimeoutException as e:
            if not sm.uses_multi_proxy_dispatch():
                clear_outbound_request_markers()
            return False, 0, None, f"timeout:{e}"
        except httpx.RequestError as e:
            if not sm.uses_multi_proxy_dispatch():
                try:
                    req_body = urlencode(data, doseq=True)
                except Exception:
                    req_body = str(data)
                log_httpx_outbound_request_error_sync(
                    method="POST",
                    url=target_url,
                    req_body=req_body,
                    err=(str(e) or repr(e)),
                    platform_user_id=sm.platform_user_id,
                    proxy_label=sm.outbound_proxy_log_label(),
                    uses_outbound_proxy=sm.uses_outbound_proxy(),
                )
                clear_outbound_request_markers()
            return False, 0, None, str(e)
        finally:
            if not sm.uses_multi_proxy_dispatch():
                clear_outbound_request_markers()

        text = ""
        parsed: Any = None
        try:
            parsed = r.json()
            text = json.dumps(parsed, ensure_ascii=False, indent=2)
        except ValueError:
            text = r.text or ""

        return r.is_success, r.status_code, parsed, text
    finally:
        if pin_token is not None:
            SessionManager.reset_pinned_proxy_index(pin_token)


async def _post_ace_via_override_proxy(
    sm: SessionManager,
    *,
    target_url: str,
    data: Dict[str, Any],
    proxy_url: str,
    timeout_seconds: Optional[float],
) -> Tuple[bool, int, Any, str]:
    """共享池借出代理：临时 client + 复制会话 Cookie。"""
    from app.services.session_manager import _outbound_verify_ca_bundle

    base = await sm.client()
    cookie_jar = httpx.Cookies()
    try:
        cookie_jar.update(base.cookies)
    except Exception:
        pass

    verify_arg: Any = _outbound_verify_ca_bundle() if settings.outbound_tls_verify else False
    to = float(timeout_seconds) if timeout_seconds is not None else float(settings.rpc_timeout_seconds or 30.0)
    try:
        async with httpx.AsyncClient(
            proxy=proxy_url,
            timeout=to,
            follow_redirects=True,
            verify=verify_arg,
            trust_env=False,
            cookies=cookie_jar,
        ) as client:
            try:
                r = await client.post(
                    target_url,
                    headers=get_rpc_browser_headers(),
                    data=data,
                )
            except httpx.TimeoutException as e:
                return False, 0, None, f"timeout:{e}"
            except httpx.RequestError as e:
                return False, 0, None, str(e)
            text = ""
            parsed: Any = None
            try:
                parsed = r.json()
                text = json.dumps(parsed, ensure_ascii=False, indent=2)
            except ValueError:
                text = r.text or ""
            try:
                base.cookies.update(client.cookies)
            except Exception:
                pass
            return r.is_success, r.status_code, parsed, text
    except httpx.TimeoutException as e:
        return False, 0, None, f"timeout:{e}"
    except Exception as e:
        return False, 0, None, str(e)


def describe_ace_sell_response(status_code: int, parsed: Any, raw_body: str) -> str:
    """
    从 ACE_Sell_Son 响应中提取可读说明（业务 Message、或整段 JSON / 非 JSON 正文）。
    便于日志中查看 Error=true、HTTP 429 等具体原因。
    """
    if isinstance(parsed, dict):
        for k in (
            "Message",
            "message",
            "Msg",
            "msg",
            "ErrorMessage",
            "errorMessage",
            "Description",
            "description",
            "ExceptionMessage",
            "Exception",
            "ResultMessage",
            "Tips",
            "tips",
        ):
            v = parsed.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()[:2500]
        data = parsed.get("Data")
        if isinstance(data, dict):
            for k in ("Message", "message", "Msg", "msg", "ErrorMessage"):
                v = data.get(k)
                if v is not None and str(v).strip():
                    return str(v).strip()[:2500]
        if isinstance(data, str) and data.strip():
            return data.strip()[:2500]
        try:
            return json.dumps(parsed, ensure_ascii=False)[:2500]
        except Exception:
            return str(parsed)[:2000]
    if (raw_body or "").strip():
        return (raw_body or "").strip()[:2500]
    return f"HTTP {status_code}，响应体为空"
