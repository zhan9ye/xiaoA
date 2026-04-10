import json
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import httpx

from app.services.runner_fetch_guard import assert_sub_fetch_allowed
from app.services.rpc_auth_signals import json_indicates_rpc_not_logged_in
from app.services.rpc_common import get_rpc_browser_headers
from app.services.session_manager import SessionManager
from app.services.subaccount_parse import (
    extract_subaccount_rows,
    normalize_subaccount_row,
    should_request_next_page,
)
from app.settings import settings


class FetchSubaccountsOutcome(NamedTuple):
    items: List[Dict[str, Any]]
    """首页 My_Subaccount 是否 2xx（未请求过首页时为 True）。"""
    first_page_ok: bool
    first_page_status_code: int
    """HTTP 200 但 JSON 为未登录时 True，调用方应重新 Login。"""
    not_logged_in: bool


async def post_my_subaccount_json(
    sm: SessionManager,
    *,
    page: int,
    size: int,
    key: str,
    user_id: str,
    v: str,
    lang: str = "cn",
) -> Tuple[bool, int, Any, str]:
    """
    POST My_Subaccount。
    返回 (是否 2xx, HTTP 状态码, 解析后的 JSON 对象或 None, 格式化正文/错误文本)。
    """
    client = await sm.client()
    data = {
        "p": str(page),
        "size": str(size),
        "key": key,
        "UserID": user_id,
        "v": v,
        "lang": lang,
    }
    try:
        r = await client.post(
            settings.subaccount_url,
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


async def fetch_all_subaccounts(
    sm: SessionManager,
    *,
    key: str,
    user_id: str,
    v: str,
    page_size: int,
    max_pages: int,
    log_push,
    silent: bool = False,
) -> FetchSubaccountsOutcome:
    """
    自动翻页拉取全部子账号。log_push(level_name, msg) 用于日志；silent=True 时不输出子账号接口详情。
    """
    assert_sub_fetch_allowed()
    all_norm: List[Dict[str, Any]] = []
    page = 1
    first_page_ok = True
    first_page_status_code = 0
    not_logged_in = False

    async def _log(level_name: str, msg: str) -> None:
        if silent or log_push is None:
            return
        await log_push(level_name, msg)

    while page <= max_pages:
        ok, code, parsed, raw = await post_my_subaccount_json(
            sm,
            page=page,
            size=page_size,
            key=key,
            user_id=user_id,
            v=v,
            lang="cn",
        )
        if page == 1:
            first_page_ok = ok
            first_page_status_code = int(code)
            if ok and json_indicates_rpc_not_logged_in(parsed):
                not_logged_in = True
                first_page_ok = False
                await _log("warn", "My_Subaccount 返回用戶未登錄，应重新 Login")
                return FetchSubaccountsOutcome(
                    [],
                    first_page_ok,
                    first_page_status_code,
                    True,
                )
        if not ok:
            await _log("error", f"My_Subaccount p={page} 失败 HTTP {code}")
            if not silent:
                await _log("info", raw[:4000] if raw else "")
            break

        rows = extract_subaccount_rows(parsed)
        before = len(all_norm)
        for item in rows:
            all_norm.append(normalize_subaccount_row(item))
        added = len(all_norm) - before

        await _log("info", f"My_Subaccount p={page} HTTP {code}，本页解析 {added} 条，累计 {len(all_norm)} 条")

        cont, reason = should_request_next_page(rows, page_size, parsed, len(all_norm))
        if not cont:
            await _log("success", f"子账号分页结束：{reason}（共 {len(all_norm)} 条）")
            break

        page += 1
    else:
        await _log(
            "warn",
            f"已达最大翻页数 {max_pages}，停止拉取（当前共 {len(all_norm)} 条）",
        )

    return FetchSubaccountsOutcome(all_norm, first_page_ok, first_page_status_code, not_logged_in)
