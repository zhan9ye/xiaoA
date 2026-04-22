"""用户与出站 HTTP 代理静态绑定（proxy_pool_entries）。"""

from __future__ import annotations

from typing import Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import ProxyPoolEntry
from app.settings import settings
from app.user_registry import get_or_create_session_manager


async def _active_pool_count(db: AsyncSession) -> int:
    r = await db.execute(
        select(func.count()).select_from(ProxyPoolEntry).where(ProxyPoolEntry.is_active.is_(True))
    )
    return int(r.scalar_one() or 0)


def _proxy_url_and_label(row: ProxyPoolEntry) -> Tuple[Optional[str], Optional[str]]:
    u = (row.proxy_url or "").strip() or None
    lab = (row.label or "").strip() or None
    return u, lab


async def ensure_proxy_assigned_for_user(db: AsyncSession, user_id: int) -> Tuple[Optional[str], Optional[str]]:
    """
    返回 (proxy_url, proxy_label)；label 来自 proxy_pool_entries.label，直连时为 (None, None)。
    若代理池无任何启用条目：返回 (None, None)。
    若用户已绑定：返回该条目的 url/label（未启用则视为无代理）。
    否则原子领取一条空闲记录；池满时依 settings.proxy_pool_require_available 抛错或返回 (None, None)。
    """
    n = await _active_pool_count(db)
    if n == 0:
        return None, None

    r0 = await db.execute(
        select(ProxyPoolEntry).where(ProxyPoolEntry.assigned_user_id == user_id).limit(1)
    )
    existing = r0.scalar_one_or_none()
    if existing is not None:
        if not existing.is_active:
            return None, None
        return _proxy_url_and_label(existing)

    # 单条 UPDATE 避免并发下两用户抢到同一空闲行
    res = await db.execute(
        text(
            """
            UPDATE proxy_pool_entries SET assigned_user_id = :uid
            WHERE id = (
                SELECT id FROM proxy_pool_entries
                WHERE assigned_user_id IS NULL AND is_active = 1
                ORDER BY id ASC LIMIT 1
            )
            """
        ),
        {"uid": user_id},
    )
    rc = getattr(res, "rowcount", None)
    if rc == 0:
        if settings.proxy_pool_require_available:
            raise HTTPException(
                status_code=503,
                detail="出站代理池已满，无空闲节点可分配，请管理员扩容或释放代理",
            )
        return None, None

    r3 = await db.execute(select(ProxyPoolEntry).where(ProxyPoolEntry.assigned_user_id == user_id))
    row = r3.scalar_one()
    return _proxy_url_and_label(row)


async def get_session_manager_for_user_id(user_id: int):
    """
    解析用户绑定的出站代理并返回 SessionManager（进程内单例、与 proxy_url 变更时重建 client）。
    在独立短会话中 commit 代理领取，避免与调用方长事务冲突。
    """
    async with AsyncSessionLocal() as session:
        try:
            proxy_url, proxy_label = await ensure_proxy_assigned_for_user(session, user_id)
            await session.commit()
        except HTTPException:
            await session.rollback()
            raise
        except Exception:
            await session.rollback()
            raise
    return await get_or_create_session_manager(user_id, proxy_url, proxy_label=proxy_label)


async def release_proxy_binding_for_user(db: AsyncSession, user_id: int) -> None:
    """将池条目的 assigned_user_id 置空（管理员手动回收）。"""
    r = await db.execute(select(ProxyPoolEntry).where(ProxyPoolEntry.assigned_user_id == user_id))
    row = r.scalar_one_or_none()
    if row is not None:
        row.assigned_user_id = None
        await db.flush()
