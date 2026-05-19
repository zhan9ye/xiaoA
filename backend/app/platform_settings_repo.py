"""全站平台配置（开售时间等）读写。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PlatformSettings
from app.schemas import _normalize_hhmm_beijing

DEFAULT_PLATFORM_SELL_START = "12:00"
_sell_time_revision = 0


def platform_sell_time_revision() -> int:
    """全站开售时间变更计数；Runner 等待期间若递增则重算调度。"""
    return int(_sell_time_revision)


def _bump_platform_sell_time_revision() -> None:
    global _sell_time_revision
    _sell_time_revision += 1


async def ensure_platform_settings_row(db: AsyncSession) -> PlatformSettings:
    row = await db.get(PlatformSettings, 1)
    if row is None:
        row = PlatformSettings(id=1, sell_start_time=DEFAULT_PLATFORM_SELL_START)
        db.add(row)
        await db.flush()
    return row


async def get_platform_sell_start_time(db: AsyncSession) -> str:
    row = await ensure_platform_settings_row(db)
    raw = (row.sell_start_time or "").strip()
    return raw or DEFAULT_PLATFORM_SELL_START


async def set_platform_sell_start_time(db: AsyncSession, hhmm: str) -> str:
    normalized = _normalize_hhmm_beijing(hhmm)
    if not normalized:
        raise ValueError("开售时间须为 HH:MM（北京时间）")
    row = await ensure_platform_settings_row(db)
    prev = (row.sell_start_time or "").strip()
    row.sell_start_time = normalized
    await db.flush()
    if prev != normalized:
        _bump_platform_sell_time_revision()
    return normalized
