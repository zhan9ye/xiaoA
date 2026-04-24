import json
from typing import Any, Dict, Optional, Tuple

import httpx

from app.services.rpc_common import get_rpc_browser_headers
from app.services.session_manager import SessionManager
from app.settings import settings


async def post_public_index_data(
    sm: SessionManager,
    *,
    key: str,
    user_id: str,
    v: str,
    lang: str = "cn",
) -> Tuple[bool, int, Any, str]:
    client = await sm.client()
    data = {
        "key": str(key),
        "UserID": str(user_id),
        "v": str(v),
        "lang": str(lang),
    }
    try:
        r = await client.post(
            settings.public_index_data_url,
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


def extract_main_account_info(parsed: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(parsed, dict):
        return None
    if parsed.get("Error") is True:
        return None
    data = parsed.get("Data")
    if not isinstance(data, dict):
        return None
    out: Dict[str, Any] = {}
    for k in (
        "CreateTime",
        "ACECount",
        "TotalACE",
        "WeeklyMoney",
        "SP",
        "TP",
        "EP",
        "RP",
        "AP",
        "LP",
        "ULP",
        "Credit",
        "Rate",
        "Convertbalance",
        "EPToUsdt",
        "CurrentStockPrice",
        "HonorName",
        "LevelNumber",
        "IsService",
    ):
        if k in data:
            out[k] = data.get(k)
    return out if out else None
