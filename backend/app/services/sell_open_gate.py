"""全站开门闸门：探测命中或时钟兜底后释放，唤醒已待命 Runner。"""

from __future__ import annotations

import asyncio
from typing import Optional

from app.services.beijing_time import beijing_today_str

_lock = asyncio.Lock()
_open_event = asyncio.Event()
_open_day = ""
_open_reason = ""
_revision = 0


def sell_open_revision() -> int:
    return int(_revision)


async def reset_sell_open_for_today(*, force: bool = False) -> None:
    """新北京日或强制重置：清空开门信号，供当日重新探测。"""
    global _open_day, _open_reason, _revision
    today = beijing_today_str()
    async with _lock:
        if not force and _open_day == today and not _open_event.is_set():
            _open_day = today
            return
        if not force and _open_day == today and _open_event.is_set():
            return
        _open_day = today
        _open_reason = ""
        _open_event.clear()
        _revision += 1


async def signal_sell_open(reason: str) -> bool:
    """
    标记通道已开。返回 True 表示本次首次置位（可打日志）。
    """
    global _open_reason, _revision, _open_day
    today = beijing_today_str()
    async with _lock:
        _open_day = today
        if _open_event.is_set():
            return False
        _open_reason = (reason or "").strip() or "open"
        _open_event.set()
        _revision += 1
        return True


def sell_open_is_set() -> bool:
    return _open_event.is_set()


def sell_open_reason() -> str:
    return _open_reason


async def wait_sell_open(
    *,
    timeout_seconds: float,
    stop_event: Optional[asyncio.Event] = None,
) -> bool:
    """
    等待开门信号。True=已开门；False=超时或 stop。
    timeout_seconds<=0 表示只轮询当前状态一次。
    """
    if _open_event.is_set():
        return True
    if timeout_seconds <= 0:
        return _open_event.is_set()

    deadline = asyncio.get_running_loop().time() + float(timeout_seconds)

    async def _stop_wait() -> None:
        if stop_event is None:
            await asyncio.sleep(10**9)
            return
        await stop_event.wait()

    while True:
        if stop_event is not None and stop_event.is_set():
            return False
        if _open_event.is_set():
            return True
        rem = deadline - asyncio.get_running_loop().time()
        if rem <= 0:
            return _open_event.is_set()
        open_task = asyncio.create_task(_open_event.wait())
        stop_task = asyncio.create_task(_stop_wait())
        done, pending = await asyncio.wait(
            {open_task, stop_task},
            timeout=min(rem, 0.25),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if open_task in done and _open_event.is_set():
            return True
        if stop_event is not None and stop_event.is_set():
            return False
