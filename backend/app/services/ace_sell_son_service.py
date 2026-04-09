import json
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.services.rpc_common import get_rpc_browser_headers
from app.services.session_manager import SessionManager
from app.settings import settings


def resolve_count_from_subaccounts(items: List[Dict[str, Any]], son_id: str) -> Optional[str]:
    """在子账号缓存中按子账号 id 查找数量类字段（如 AceAmount）。"""
    target = str(son_id).strip()
    if not target:
        return None
    for row in items:
        for sid_key in ("SonId", "sonId", "Id", "ID", "SubAccountId", "SubId"):
            v = row.get(sid_key)
            if v is not None and str(v).strip() == target:
                for amt_key in ("AceAmount", "ACEAmount", "aceAmount", "Ace_Count", "Count"):
                    av = row.get(amt_key)
                    if av is not None and str(av).strip():
                        return str(av).strip()
                return None
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
) -> Tuple[bool, int, Any, str]:
    """
    POST ACE_Sell_Son（application/x-www-form-urlencoded）。
    须在已 Login 的同一会话 client 上调用以携带 Cookie。
    """
    client = await sm.client()
    data = {
        "amount": str(amount),
        "password": str(password),
        "sonId": str(son_id),
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
    try:
        r = await client.post(
            settings.ace_sell_son_url,
            headers=get_rpc_browser_headers(),
            data=data,
        )
    except httpx.RequestError as e:
        return False, 0, None, str(e)

    text = ""
    parsed: Any = None
    try:
        parsed = r.json()
        text = json.dumps(parsed, ensure_ascii=False, indent=2)
    except ValueError:
        text = r.text or ""

    return r.is_success, r.status_code, parsed, text


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
