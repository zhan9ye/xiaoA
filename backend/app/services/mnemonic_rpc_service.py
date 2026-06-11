import json
from typing import Any, Dict, Optional, Tuple

import httpx

from app.services.rpc_common import get_rpc_browser_headers
from app.services.session_manager import SessionManager
from app.settings import settings


def parse_mnemonic_get01_response(parsed: Any) -> Optional[Dict[str, str]]:
    """解析 Mnemonic_Get01 JSON：Error=false 时返回 mnemonicid1、mnemonickey。"""
    if not isinstance(parsed, dict):
        return None
    if parsed.get("Error") is True:
        return None
    k = parsed.get("mnemonickey")
    mid = parsed.get("mnemonicid1")
    if k is None or k == "" or mid is None:
        return None
    mid_s = str(mid).strip()
    try:
        mid_norm = str(int(float(mid_s)))
    except (ValueError, TypeError):
        mid_norm = mid_s
    return {
        "mnemonicid1": mid_norm,
        "mnemonickey": str(k).strip(),
        "mnemonictitle": str(parsed.get("mnemonictitle") or "").strip(),
    }


async def post_mnemonic_get01(
    sm: SessionManager,
    *,
    rpc_key: str,
    user_id: str,
    v: str,
    lang: str = "cn",
    proxy_pin_index: Optional[int] = None,
) -> Tuple[bool, int, Any, str]:
    """POST Mnemonic_Get01，与 Login 同会话 Cookie。多代理时可指定 proxy_pin_index 固定出口（用于预热）。"""
    client = await sm.client()
    pin_token = None
    if proxy_pin_index is not None and sm.outbound_proxy_count() > 0:
        pin_token = SessionManager.pinned_proxy_index(proxy_pin_index)
    data = {
        "key": str(rpc_key),
        "UserID": str(user_id),
        "v": str(v),
        "lang": str(lang),
    }
    try:
        try:
            r = await client.post(
                settings.mnemonic_get01_url,
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
    finally:
        if pin_token is not None:
            SessionManager.reset_pinned_proxy_index(pin_token)


async def fetch_mnemonic_meta(
    sm: SessionManager,
    *,
    rpc_key: str,
    user_id: str,
    v: str,
    lang: str = "cn",
) -> Optional[Dict[str, str]]:
    """请求 Mnemonic_Get01 并解析出 mnemonicid1 / mnemonickey；失败返回 None。"""
    ok, code, parsed, _raw = await post_mnemonic_get01(
        sm, rpc_key=rpc_key, user_id=user_id, v=v, lang=lang
    )
    if not ok:
        return None
    return parse_mnemonic_get01_response(parsed)
