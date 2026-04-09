"""RPC 参数 v：与站点 base.js 中 APP.GLOBAL.ajax 一致。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

# base.js 使用 new Date() 的「本地」年月日时分相加；服务端用 Asia/Shanghai，与大陆用户浏览器常见时区一致。
_RPC_V_TZ = ZoneInfo("Asia/Shanghai")


def compute_js_timespan_v(now: datetime | None = None) -> str:
    """
    与 base.js 一致：
    timespan = getFullYear() + getMonth() + getDate() + getHours() + getMinutes()
    其中 getMonth() 为 0～11（一月为 0）。
    """
    if now is None:
        dt = datetime.now(_RPC_V_TZ)
    elif now.tzinfo is None:
        dt = now.replace(tzinfo=_RPC_V_TZ)
    else:
        dt = now.astimezone(_RPC_V_TZ)
    v = dt.year + (dt.month - 1) + dt.day + dt.hour + dt.minute
    return str(v)
