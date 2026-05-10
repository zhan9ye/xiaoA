from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import AdminEcsInstanceLock, ProxyPoolEntry, TradingConfig, User
from app.services.aliyun_ecs_ops import (
    aliyun_ecs_run_configured,
    delete_instance_sync,
    list_ecs_instances_page_sync,
    run_instances_then_poll_public_ips_sync,
)
from app.services.proxy_akapi1_probe import probe_akapi1_login_via_proxy
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


async def _insert_auto_purchase_pool_rows(
    db: AsyncSession, ids: List[str], ip_map: Dict[str, str]
) -> Tuple[List[int], int, int, int]:
    """自动购机写入池：assignment_allowed=False，返回待探测的池 id 列表。"""
    pending: List[int] = []
    added = 0
    skipped_no_ip = 0
    skipped_dup = 0
    for iid in ids:
        ip = (ip_map.get(iid) or "").strip()
        if not ip:
            skipped_no_ip += 1
            continue
        proxy_url = f"http://{ip}:3128"
        dup = await db.execute(select(ProxyPoolEntry.id).where(ProxyPoolEntry.proxy_url == proxy_url).limit(1))
        if dup.scalar_one_or_none() is not None:
            skipped_dup += 1
            continue
        row = ProxyPoolEntry(
            proxy_url=proxy_url,
            label=iid[:128],
            is_active=True,
            assignment_allowed=False,
        )
        db.add(row)
        await db.flush()
        pending.append(int(row.id))
        added += 1
    return pending, added, skipped_no_ip, skipped_dup


async def _release_failed_pool_entries_after_probe(failed_pids: List[int]) -> None:
    """探测失败：删池条目并按 label 实例 ID 释放 ECS（跳过锁定实例）。"""
    for pid in failed_pids:
        iid = ""
        async with AsyncSessionLocal() as db:
            row = await db.get(ProxyPoolEntry, pid)
            if row is None:
                continue
            iid = (row.label or "").strip()
            await db.delete(row)
            await db.commit()
        if not iid.startswith("i-"):
            continue
        locked = False
        async with AsyncSessionLocal() as db:
            locked = await db.get(AdminEcsInstanceLock, iid) is not None
        if locked:
            print(f"[auto-proxy-buy] probe_fail: ECS 已锁定，跳过 DeleteInstance {iid}")
            continue
        try:
            await asyncio.to_thread(delete_instance_sync, iid)
        except Exception as ex:
            print(f"[auto-proxy-buy] probe_fail DeleteInstance {iid}: {ex!r}")


async def _probe_replace_until_all_ok(
    initial_pending: List[int],
    *,
    trigger: str,
) -> Dict[str, Any]:
    """
    购入完成后（或每轮补购完成后）：等待配置秒 → 并发 Login 探测 →
    未通过则全部释放并按失败数量补购，再重复直到通过或超限。
    """
    delay = max(10.0, float(settings.proxy_auto_purchase_probe_delay_seconds or 120.0))
    max_rounds = max(1, int(settings.proxy_auto_purchase_probe_max_rounds or 100))

    cur = [int(x) for x in initial_pending if x]
    rounds_done = 0
    replacements_total = 0

    while cur:
        rounds_done += 1
        if rounds_done > max_rounds:
            print(f"[auto-proxy-buy] trigger={trigger} probe 已达最大轮数 {max_rounds}，仍为未放行：{cur}")
            return {
                "probe_rounds": rounds_done - 1,
                "probe_replacements_total": replacements_total,
                "pending_unverified": cur,
                "probe_aborted_max_rounds": True,
            }

        print(
            f"[auto-proxy-buy] trigger={trigger} 购机后等待 {delay:.0f}s 再探测（第 {rounds_done} 轮，共 {len(cur)} 台）"
        )
        await asyncio.sleep(delay)

        async with AsyncSessionLocal() as db:
            pairs: List[Tuple[int, str]] = []
            for pid in cur:
                e = await db.get(ProxyPoolEntry, pid)
                if e is not None and e.is_active:
                    u = (e.proxy_url or "").strip()
                    if u:
                        pairs.append((pid, u))

        if not pairs:
            print(f"[auto-proxy-buy] trigger={trigger} 探测：池条目已不存在 active，结束 pending={cur}")
            return {
                "probe_rounds": rounds_done,
                "probe_replacements_total": replacements_total,
                "pending_unverified": [],
                "probe_aborted_missing_rows": True,
            }

        raw_results = await asyncio.gather(
            *[probe_akapi1_login_via_proxy(url) for _pid, url in pairs],
            return_exceptions=True,
        )

        passed: List[int] = []
        failed: List[int] = []
        for (pid, _u), raw in zip(pairs, raw_results):
            if isinstance(raw, Exception):
                print(f"[auto-proxy-buy] 探测异常 pool_entry_id={pid}: {raw!r}")
                failed.append(pid)
                continue
            if raw.get("proxy_ok"):
                passed.append(pid)
            else:
                failed.append(pid)

        async with AsyncSessionLocal() as db:
            for pid in passed:
                e = await db.get(ProxyPoolEntry, pid)
                if e is not None:
                    e.assignment_allowed = True
            await db.commit()

        if not failed:
            print(
                f"[auto-proxy-buy] trigger={trigger} 探测全部通过：rounds={rounds_done} verified={len(passed)}"
            )
            return {
                "probe_rounds": rounds_done,
                "probe_replacements_total": replacements_total,
                "pending_unverified": [],
                "probe_aborted_max_rounds": False,
            }

        print(
            f"[auto-proxy-buy] trigger={trigger} 第 {rounds_done} 轮：通过 {len(passed)}，失败 {len(failed)}，补购并重测"
        )
        await _release_failed_pool_entries_after_probe(failed)
        k = len(failed)
        try:
            new_ids, rep_rid, new_ip_map = await asyncio.to_thread(run_instances_then_poll_public_ips_sync, k)
        except Exception as ex:
            print(f"[auto-proxy-buy] 补购 RunInstances 失败: {ex!r}")
            return {
                "probe_rounds": rounds_done,
                "probe_replacements_total": replacements_total,
                "pending_unverified": [],
                "probe_aborted_replace_error": str(ex),
            }

        replacements_total += k

        async with AsyncSessionLocal() as db:
            cur2, _added_r, skipped_ni, skipped_d = await _insert_auto_purchase_pool_rows(db, new_ids, new_ip_map)
            await db.commit()

        if len(cur2) < k:
            print(
                f"[auto-proxy-buy] 补购入池不完整 want={k} got_rows={len(cur2)} "
                f"skipped_no_ip={skipped_ni} skipped_dup={skipped_d} rid={rep_rid}"
            )
        if not cur2:
            print("[auto-proxy-buy] 补购未产生可用池条目，停止探测流程")
            return {
                "probe_rounds": rounds_done,
                "probe_replacements_total": replacements_total,
                "pending_unverified": [],
                "probe_aborted_no_new_rows": True,
            }
        cur = cur2

    return {
        "probe_rounds": rounds_done,
        "probe_replacements_total": replacements_total,
        "pending_unverified": [],
        "probe_aborted_max_rounds": False,
    }


async def auto_purchase_proxies_once(trigger: str = "manual") -> Dict[str, Any]:
    """
    按规则自动购机并写入代理池（未探测前 assignment_allowed=false，不可分配给平台用户）。
    全部实例创建并联调公网入库后等待，再探测；失败则释放并循环补购至通过或超限。
    容量口径：仅计 is_active + assignment_allowed 条目。
    """
    enabled_eff, multiplier = await _effective_auto_purchase_policy()
    if not enabled_eff:
        return {"eligible_users": 0, "multiplier": int(multiplier), "to_buy": 0}
    if not aliyun_ecs_run_configured():
        return {"eligible_users": 0, "multiplier": int(multiplier), "to_buy": 0}

    now_utc = dt.datetime.now(dt.timezone.utc)
    pending: List[int] = []

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
        assignable_q = (
            select(func.count())
            .select_from(ProxyPoolEntry)
            .where(
                ProxyPoolEntry.is_active.is_(True),
                ProxyPoolEntry.assignment_allowed.is_(True),
            )
        )
        assignable_pool = int((await db.execute(assignable_q)).scalar_one() or 0)

        target_total = eligible_users * multiplier
        to_buy = max(0, target_total - assignable_pool)
        if to_buy <= 0:
            print(
                f"[auto-proxy-buy] trigger={trigger} eligible={eligible_users} multiplier={multiplier} "
                f"assignable_pool={assignable_pool} target={target_total} to_buy=0"
            )
            return {
                "eligible_users": eligible_users,
                "multiplier": multiplier,
                "assignable_pool": assignable_pool,
                "target_total": target_total,
                "to_buy": 0,
                "added": 0,
                "skipped_no_ip": 0,
                "skipped_duplicate": 0,
                "probe_rounds": 0,
                "probe_replacements_total": 0,
            }

        ids, req_id, ip_map = await asyncio.to_thread(run_instances_then_poll_public_ips_sync, to_buy)
        pending, added, skipped_no_ip, skipped_dup = await _insert_auto_purchase_pool_rows(db, ids, ip_map)
        await db.commit()
        print(
            f"[auto-proxy-buy] trigger={trigger} eligible={eligible_users} multiplier={multiplier} "
            f"assignable_pool={assignable_pool} target={target_total} to_buy={to_buy} created={len(ids)} "
            f"added_pending_probe={added} skipped_no_ip={skipped_no_ip} skipped_dup={skipped_dup} "
            f"request_id={req_id}"
        )
        snapshot = {
            "eligible_users": eligible_users,
            "multiplier": multiplier,
            "assignable_pool": assignable_pool,
            "target_total": target_total,
            "to_buy": to_buy,
            "created_instances": len(ids),
            "added": added,
            "skipped_no_ip": skipped_no_ip,
            "skipped_duplicate": skipped_dup,
            "request_id": req_id,
        }

    if pending:
        probe_out = await _probe_replace_until_all_ok(pending, trigger=trigger)
    else:
        probe_out = {
            "probe_rounds": 0,
            "probe_replacements_total": 0,
            "pending_unverified": [],
            "probe_aborted_max_rounds": False,
        }
        if to_buy > 0:
            print(f"[auto-proxy-buy] trigger={trigger} warning: to_buy={to_buy} but no pool rows added, skip probe")

    snapshot.update(probe_out)
    return snapshot


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
