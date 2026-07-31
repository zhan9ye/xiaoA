"""全站平台配置（开售时间、开门探测 JSON 等）读写。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PlatformSettings
from app.schemas import _normalize_hhmm_beijing
from app.services.sell_open_probe_config import (
    SellOpenProbeConfig,
    parse_probe_config,
    probe_config_to_dict,
    probe_config_to_json,
)

DEFAULT_PLATFORM_SELL_START = "12:00"
_sell_time_revision = 0
_probe_config_revision = 0


def platform_sell_time_revision() -> int:
    """全站开售时间变更计数；Runner 等待期间若递增则重算调度。"""
    return int(_sell_time_revision)


def sell_open_probe_config_revision() -> int:
    return int(_probe_config_revision)


def _bump_platform_sell_time_revision() -> None:
    global _sell_time_revision
    _sell_time_revision += 1


def _bump_probe_config_revision() -> None:
    global _probe_config_revision
    _probe_config_revision += 1


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


async def get_sell_open_probe_config(db: AsyncSession) -> SellOpenProbeConfig:
    row = await ensure_platform_settings_row(db)
    raw = getattr(row, "sell_open_probe_json", None) or "{}"
    return parse_probe_config(raw)


async def set_sell_open_probe_config(
    db: AsyncSession,
    patch: Dict[str, Any],
    *,
    replace: bool = False,
) -> SellOpenProbeConfig:
    row = await ensure_platform_settings_row(db)
    if replace:
        cfg = parse_probe_config(patch)
    else:
        cur = parse_probe_config(getattr(row, "sell_open_probe_json", None) or "{}")
        merged = {**probe_config_to_dict(cur), **(patch or {})}
        cfg = parse_probe_config(merged)
    row.sell_open_probe_json = probe_config_to_json(cfg)
    await db.flush()
    _bump_probe_config_revision()
    return cfg
