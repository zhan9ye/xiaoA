"""用户与出站 HTTP 代理绑定（proxy_pool_entries）；支持每用户多条（测试：轮询出口）。"""

from __future__ import annotations

from typing import List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import ProxyPoolEntry
from app.services.proxy_auto_purchase import get_auto_purchase_policy
from app.settings import settings
from app.user_registry import get_or_create_session_manager


async def _assignable_idle_count(db: AsyncSession) -> int:
    """可被自动领取的空闲条目：启用 + 已放行分配 + 未绑定。"""
    r = await db.execute(
        select(func.count())
        .select_from(ProxyPoolEntry)
        .where(
            ProxyPoolEntry.is_active.is_(True),
            ProxyPoolEntry.assignment_allowed.is_(True),
            ProxyPoolEntry.assigned_user_id.is_(None),
        )
    )
    return int(r.scalar_one() or 0)


def _proxy_url_and_label(row: ProxyPoolEntry) -> Tuple[Optional[str], Optional[str]]:
    u = (row.proxy_url or "").strip() or None
    lab = (row.label or "").strip() or None
    return u, lab


async def _list_user_proxy_rows(db: AsyncSession, user_id: int) -> List[ProxyPoolEntry]:
    r = await db.execute(
        select(ProxyPoolEntry)
        .where(ProxyPoolEntry.assigned_user_id == user_id)
        .order_by(ProxyPoolEntry.id.asc())
    )
    return list(r.scalars().all())


async def ensure_proxies_for_user(db: AsyncSession, user_id: int) -> List[Tuple[str, Optional[str]]]:
    """
    返回 [(proxy_url, label), ...] 仅含启用条目的非空 URL。
    若无绑定且开启 PROXY_POOL_AUTO_ASSIGN：按自动购机策略倍数领取空闲条目（与 MULTIPLIER 一致，上限 20）；
    若关闭 MULTI_PROXY_PER_USER_ENABLED，仍只领取 1 条（会话单出口）。
    """
    rows = [e for e in await _list_user_proxy_rows(db, user_id) if e.is_active and e.assignment_allowed]
    out: List[Tuple[str, Optional[str]]] = []
    for e in rows:
        u, lab = _proxy_url_and_label(e)
        if u:
            out.append((u, lab))
    if out:
        if not bool(settings.multi_proxy_per_user_enabled) and len(out) > 1:
            return out[:1]
        return out

    if not bool(settings.proxy_pool_auto_assign):
        return []

    policy = await get_auto_purchase_policy()
    want = max(1, min(20, int(policy.get("multiplier") or 1)))
    if not bool(settings.multi_proxy_per_user_enabled):
        want = 1

    assigned = 0
    for _ in range(want):
        res = await db.execute(
            text(
                """
                UPDATE proxy_pool_entries SET assigned_user_id = :uid
                WHERE id = (
                    SELECT id FROM proxy_pool_entries
                    WHERE assigned_user_id IS NULL AND is_active = 1 AND assignment_allowed = 1
                    ORDER BY id ASC LIMIT 1
                )
                """
            ),
            {"uid": user_id},
        )
        rc = int(getattr(res, "rowcount", None) or 0)
        if rc == 0:
            break
        assigned += 1

    if assigned == 0:
        if settings.proxy_pool_require_available:
            raise HTTPException(
                status_code=503,
                detail="出站代理池已满，无空闲节点可分配，请管理员扩容或释放代理",
            )
        return []

    rows2 = [e for e in await _list_user_proxy_rows(db, user_id) if e.is_active and e.assignment_allowed]
    out2: List[Tuple[str, Optional[str]]] = []
    for e in rows2:
        u, lab = _proxy_url_and_label(e)
        if u:
            out2.append((u, lab))
    return out2


async def get_session_manager_for_user_id(user_id: int):
    """
    解析用户绑定的出站代理并返回 SessionManager（进程内单例；多代理时每 POST 轮询出口，共享 Cookie）。
    在独立短会话中 commit 代理领取，避免与调用方长事务冲突。
    """
    async with AsyncSessionLocal() as session:
        try:
            pairs = await ensure_proxies_for_user(session, user_id)
            await session.commit()
        except HTTPException:
            await session.rollback()
            raise
        except Exception:
            await session.rollback()
            raise
    urls = [p[0] for p in pairs]
    labs = [p[1] for p in pairs]
    return await get_or_create_session_manager(user_id, proxy_urls=urls, proxy_labels=labs)


async def release_proxy_binding_for_user(db: AsyncSession, user_id: int) -> None:
    """将该用户在所有池条目上的 assigned_user_id 置空。"""
    r = await db.execute(select(ProxyPoolEntry).where(ProxyPoolEntry.assigned_user_id == user_id))
    for row in r.scalars().all():
        row.assigned_user_id = None
    await db.flush()
