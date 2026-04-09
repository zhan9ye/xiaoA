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
    return {
        "mnemonicid1": str(mid).strip(),
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
) -> Tuple[bool, int, Any, str]:
    """POST Mnemonic_Get01，与 Login 同会话 Cookie。"""
    client = await sm.client()
    data = {
        "key": str(rpc_key),
        "UserID": str(user_id),
        "v": str(v),
        "lang": str(lang),
    }
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
