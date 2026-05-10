from __future__ import annotations

import asyncio
import datetime as dt
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select

from app.db import AsyncSessionLocal
from app.models import AdminEcsInstanceLock, ProxyPoolEntry, TradingConfig, User
from app.services.aliyun_ecs_ops import (
    aliyun_ecs_run_configured,
    delete_instance_sync,
    list_ecs_instances_page_sync,
    run_instances_then_poll_public_ips_sync,
)
from app.services.beijing_time import BJ, beijing_now
from app.settings import settings

_policy_lock = asyncio.Lock()
_auto_purchase_enabled_override: Optional[bool] = None
_auto_purchase_multiplier_override: Optional[int] = None


async def get_auto_purchase_policy() -> Dict[str, int]:
    async with _policy_lock:
        enabled_eff = (
            _auto_purchase_enabled_override
            if _auto_purchase_enabled_override is not None
            else bool(settings.proxy_auto_purchase_enabled)
        )
        mul_eff = (
            int(_auto_purchase_multiplier_override)
            if _auto_purchase_multiplier_override is not None
            else int(settings.proxy_auto_purchase_multiplier or 1)
        )
    mul_eff = max(1, mul_eff)
    return {
        "enabled": bool(enabled_eff),
        "multiplier": int(mul_eff),
        "default_enabled": bool(settings.proxy_auto_purchase_enabled),
        "default_multiplier": max(1, int(settings.proxy_auto_purchase_multiplier or 1)),
    }


async def set_auto_purchase_policy(*, enabled: bool, multiplier: int) -> Dict[str, int]:
    global _auto_purchase_enabled_override, _auto_purchase_multiplier_override
    mul = max(1, int(multiplier))
    async with _policy_lock:
        _auto_purchase_enabled_override = bool(enabled)
        _auto_purchase_multiplier_override = mul
    return await get_auto_purchase_policy()


async def _effective_auto_purchase_policy() -> Tuple[bool, int]:
    p = await get_auto_purchase_policy()
    return bool(p["enabled"]), max(1, int(p["multiplier"]))


async def auto_purchase_proxies_once(trigger: str = "manual") -> Dict[str, int]:
    """
    按规则自动购机并写入代理池，返回统计。
    规则：
    - 目标用户：订阅有效 + 未禁用 + 活动槽 runner_enabled=true
    - 目标代理总量：目标用户数 * 倍数
    - 实际购买量：max(0, 目标代理总量 - 当前 active 代理池条目数)
    """
    enabled_eff, multiplier = await _effective_auto_purchase_policy()
    if not enabled_eff:
        return {"eligible_users": 0, "multiplier": int(multiplier), "to_buy": 0}
    if not aliyun_ecs_run_configured():
        return {"eligible_users": 0, "multiplier": int(multiplier), "to_buy": 0}

    now_utc = dt.datetime.now(dt.timezone.utc)
    async with AsyncSessionLocal() as db:
        eligible_q = (
            select(func.count())
            .select_from(User)
            .join(
                TradingConfig,
                (TradingConfig.user_id == User.id) & (TradingConfig.slot == User.active_trading_slot),
            )
            .where(
                User.is_disabled.is_(False),
                User.subscription_end_at.is_not(None),
                User.subscription_end_at > now_utc,
                TradingConfig.runner_enabled.is_(True),
            )
        )
        eligible_users = int((await db.execute(eligible_q)).scalar_one() or 0)
        active_pool_q = (
            select(func.count())
            .select_from(ProxyPoolEntry)
            .where(ProxyPoolEntry.is_active.is_(True))
        )
        active_pool = int((await db.execute(active_pool_q)).scalar_one() or 0)

        target_total = eligible_users * multiplier
        to_buy = max(0, target_total - active_pool)
        if to_buy <= 0:
            print(
                f"[auto-proxy-buy] trigger={trigger} eligible={eligible_users} multiplier={multiplier} "
                f"active_pool={active_pool} target={target_total} to_buy=0"
            )
            return {
                "eligible_users": eligible_users,
                "multiplier": multiplier,
                "active_pool": active_pool,
                "target_total": target_total,
                "to_buy": 0,
                "added": 0,
                "skipped_no_ip": 0,
                "skipped_duplicate": 0,
            }

        ids, req_id, ip_map = await asyncio.to_thread(run_instances_then_poll_public_ips_sync, to_buy)
        added = 0
        skipped_no_ip = 0
        skipped_dup = 0
        for iid in ids:
            ip = (ip_map.get(iid) or "").strip()
            if not ip:
                skipped_no_ip += 1
                continue
            proxy_url = f"http://{ip}:3128"
            dup = await db.execute(
                select(ProxyPoolEntry.id).where(ProxyPoolEntry.proxy_url == proxy_url).limit(1)
            )
            if dup.scalar_one_or_none() is not None:
                skipped_dup += 1
                continue
            db.add(ProxyPoolEntry(proxy_url=proxy_url, label=iid[:128], is_active=True))
            await db.flush()
            added += 1
        await db.commit()
        print(
            f"[auto-proxy-buy] trigger={trigger} eligible={eligible_users} multiplier={multiplier} "
            f"active_pool={active_pool} target={target_total} to_buy={to_buy} created={len(ids)} "
            f"added={added} skipped_no_ip={skipped_no_ip} skipped_dup={skipped_dup} request_id={req_id}"
        )
        return {
            "eligible_users": eligible_users,
            "multiplier": multiplier,
            "active_pool": active_pool,
            "target_total": target_total,
            "to_buy": to_buy,
            "created_instances": len(ids),
            "added": added,
            "skipped_no_ip": skipped_no_ip,
            "skipped_duplicate": skipped_dup,
        }


async def auto_release_proxy_servers_once(trigger: str = "daily-1220") -> Dict[str, int]:
    """
    每日释放代理服务器（跳过锁定实例），并清理对应代理池条目。
    规则：遍历当前地域 ECS，遇到未锁定实例即调用 DeleteInstance(force)。
    """
    if not bool(settings.proxy_auto_release_enabled):
        return {"total": 0, "locked": 0, "released": 0}
    if not aliyun_ecs_run_configured():
        return {"total": 0, "locked": 0, "released": 0}

    all_rows: List[Dict[str, str]] = []
    page = 1
    page_size = 100
    while True:
        rows, total, _ = await asyncio.to_thread(list_ecs_instances_page_sync, page, page_size)
        all_rows.extend(rows)
        if len(all_rows) >= int(total or 0) or not rows:
            break
        page += 1

    instance_ids = [str(r.get("instance_id") or "").strip() for r in all_rows if str(r.get("instance_id") or "").strip()]
    locked: set[str] = set()
    async with AsyncSessionLocal() as db:
        if instance_ids:
            lr = await db.execute(
                select(AdminEcsInstanceLock.instance_id).where(AdminEcsInstanceLock.instance_id.in_(instance_ids))
            )
            locked = {str(x) for x in lr.scalars().all()}

    released = 0
    removed_pool_entries = 0
    for row in all_rows:
        iid = str(row.get("instance_id") or "").strip()
        if not iid:
            continue
        if iid in locked:
            continue
        public_ip = (row.get("public_ip") or "").strip()
        async with AsyncSessionLocal() as db:
            prs = await db.execute(select(ProxyPoolEntry).where(ProxyPoolEntry.label == iid))
            entries = list(prs.scalars().all())
            if (not entries) and public_ip:
                proxy_url = f"http://{public_ip}:3128"
                prs2 = await db.execute(select(ProxyPoolEntry).where(ProxyPoolEntry.proxy_url == proxy_url))
                entries = list(prs2.scalars().all())
            for e in entries:
                await db.delete(e)
            removed_pool_entries += len(entries)
            await db.commit()
        try:
            await asyncio.to_thread(delete_instance_sync, iid)
            released += 1
        except Exception as ex:
            print(f"[auto-proxy-release] delete failed iid={iid}: {ex!r}")

    print(
        f"[auto-proxy-release] trigger={trigger} total={len(all_rows)} locked={len(locked)} "
        f"released={released} removed_pool_entries={removed_pool_entries}"
    )
    return {
        "total": len(all_rows),
        "locked": len(locked),
        "released": released,
        "removed_pool_entries": removed_pool_entries,
    }


def seconds_until_next_auto_buy() -> float:
    now = beijing_now()
    hh = max(0, min(23, int(settings.proxy_auto_purchase_hour or 11)))
    mm = max(0, min(59, int(settings.proxy_auto_purchase_minute or 30)))
    target = dt.datetime(now.year, now.month, now.day, hh, mm, 0, tzinfo=BJ)
    if now >= target:
        target = target + dt.timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def seconds_until_next_auto_release() -> float:
    now = beijing_now()
    hh = max(0, min(23, int(settings.proxy_auto_release_hour or 12)))
    mm = max(0, min(59, int(settings.proxy_auto_release_minute or 20)))
    target = dt.datetime(now.year, now.month, now.day, hh, mm, 0, tzinfo=BJ)
    if now >= target:
        target = target + dt.timedelta(days=1)
    return max(1.0, (target - now).total_seconds())
