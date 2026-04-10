"""多实例部署时同一 user_id 仅一个 runner 持有租约（共享 DB 时生效）。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.db import AsyncSessionLocal
from app.models import RunnerLease
from app.settings import settings

_holder_singleton: str | None = None


def get_runner_lease_holder_id() -> str:
    global _holder_singleton
    if _holder_singleton is None:
        raw = (settings.runner_lease_holder_id or "").strip()
        _holder_singleton = raw if raw else str(uuid.uuid4())
    return _holder_singleton


async def try_acquire_runner_lease(user_id: int, holder_id: str) -> bool:
    if not settings.runner_lease_enabled:
        return True
    ttl = max(5, int(settings.runner_lease_ttl_seconds or 45))
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=ttl)
    async with AsyncSessionLocal() as session:
        row = await session.get(RunnerLease, user_id)
        if row is None:
            session.add(RunnerLease(user_id=user_id, holder_id=holder_id, expires_at=exp))
            await session.commit()
            return True
        if row.expires_at < now or row.holder_id == holder_id:
            row.holder_id = holder_id
            row.expires_at = exp
            await session.commit()
            return True
        await session.rollback()
        return False


async def renew_runner_lease_if_holder(user_id: int, holder_id: str) -> None:
    if not settings.runner_lease_enabled:
        return
    ttl = max(5, int(settings.runner_lease_ttl_seconds or 45))
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=ttl)
    async with AsyncSessionLocal() as session:
        row = await session.get(RunnerLease, user_id)
        if row is not None and row.holder_id == holder_id:
            row.expires_at = exp
            await session.commit()
        else:
            await session.rollback()
