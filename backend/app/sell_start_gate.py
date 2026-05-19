"""全站开售时刻相关的启动拦截与禁开窗。"""

from __future__ import annotations

import datetime as dt
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProxyPoolEntry
from app.services.beijing_time import BJ, beijing_now, parse_hhmm

RUN_START_BLOCK_MINUTES_BEFORE = 10
RUN_START_BLOCK_MINUTES_AFTER = 5


def run_start_block_window(sell_hhmm: str) -> Optional[Tuple[dt.datetime, dt.datetime]]:
    """今日禁开窗 [开售 T-10 分钟, 开售 T+5 分钟) 左闭右开，北京时间。"""
    p = parse_hhmm(sell_hhmm)
    if not p:
        return None
    h, mi = p
    now = beijing_now()
    sell = dt.datetime(now.year, now.month, now.day, h, mi, 0, tzinfo=BJ)
    start = sell - dt.timedelta(minutes=RUN_START_BLOCK_MINUTES_BEFORE)
    end = sell + dt.timedelta(minutes=RUN_START_BLOCK_MINUTES_AFTER)
    return start, end


async def user_has_assigned_proxy(db: AsyncSession, user_id: int) -> bool:
    r = await db.execute(
        select(ProxyPoolEntry.id)
        .where(
            ProxyPoolEntry.assigned_user_id == user_id,
            ProxyPoolEntry.is_active.is_(True),
        )
        .limit(1)
    )
    return r.scalar_one_or_none() is not None


async def run_start_block_reason(db: AsyncSession, user_id: int, sell_hhmm: str) -> Optional[str]:
    window = run_start_block_window(sell_hhmm)
    if window is None:
        return None
    start, end = window
    now = beijing_now()
    if now < start or now >= end:
        return None
    if await user_has_assigned_proxy(db, user_id):
        return None
    retry_hhmm = end.strftime("%H:%M")
    return f"当前时间不允许开启售卖任务，请在{retry_hhmm}以后再进行尝试！"
