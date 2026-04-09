"""北京时间（Asia/Shanghai）调度辅助。"""

from __future__ import annotations

import datetime as dt
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from app.settings import settings

BJ = ZoneInfo("Asia/Shanghai")


def beijing_now() -> dt.datetime:
    return dt.datetime.now(BJ)


def beijing_today_str() -> str:
    return beijing_now().date().isoformat()


def parse_hhmm(s: str) -> Optional[Tuple[int, int]]:
    raw = (s or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    return h, m


def _combine_today(h: int, mi: int, base: dt.datetime) -> dt.datetime:
    d = base.date()
    return dt.datetime(d.year, d.month, d.day, h, mi, 0, tzinfo=BJ)


def seconds_until_beijing(target: dt.datetime) -> float:
    if target.tzinfo is None:
        target = target.replace(tzinfo=BJ)
    delta = (target - beijing_now()).total_seconds()
    return max(0.0, delta)


def seconds_until_next_beijing_midnight() -> float:
    """当前北京时间至「次日 0:00」的秒数。"""
    now = beijing_now()
    tomorrow = now.date() + dt.timedelta(days=1)
    midnight = dt.datetime(tomorrow.year, tomorrow.month, tomorrow.day, 0, 0, 0, tzinfo=BJ)
    return max(0.0, (midnight - now).total_seconds())


def today_prep_and_start(sell_hhmm: str) -> Optional[Tuple[dt.datetime, dt.datetime]]:
    """
    今日开售的 (T-N 秒准备时刻, 整点开售时刻)，北京时间；N = settings.sell_prep_seconds_before。
    准备时刻到达后会执行登录并全量拉取子账号，更新内存缓存一次。
    若当前已过开售点，调用方仍可用：先等 0s 再登录，再 0s 开售（立即售卖）。
    """
    p = parse_hhmm(sell_hhmm)
    if not p:
        return None
    h, mi = p
    now = beijing_now()
    start = _combine_today(h, mi, now)
    n = max(1, int(getattr(settings, "sell_prep_seconds_before", 30) or 30))
    prep = start - dt.timedelta(seconds=n)
    return prep, start
