"""上游 RPC JSON 中与登录态相关的通用字段（HTTP 可能仍为 200）。"""

from __future__ import annotations

from typing import Any


def json_indicates_rpc_not_logged_in(parsed: Any) -> bool:
    """
    常见形态：Error=true 且 IsLogin=false（如「用戶未登錄」）。
    与 HTTP 401 区分：部分接口在会话失效时仍返回 200。
    """
    if not isinstance(parsed, dict):
        return False
    return parsed.get("Error") is True and parsed.get("IsLogin") is False
