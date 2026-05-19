from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import AdminEcsInstanceLock, ProxyPoolEntry, TradingConfig, User
from app.proxy_lifecycle_log import proxy_lifecycle_log
from app.services.aliyun_ecs_ops import (
    aliyun_ecs_run_configured,
    delete_instance_sync,
    list_ecs_instances_page_sync,
    run_instances_then_poll_public_ips_sync,
)
from app.services.beijing_time import BJ, beijing_now, parse_hhmm
from app.services.login_service import verify_trade_credentials
from app.services.proxy_akapi1_probe import probe_akapi1_login_via_proxy
from app.settings import settings
from app.trading_config_repo import get_active_trading_slot, load_trading_config, persist_trading_config
from app.user_registry import get_or_create_state

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


async def _eligible_runner_users(db: AsyncSession, now_utc: dt.datetime) -> List[Tuple[int, str]]:
    r = await db.execute(
        select(User.id, User.username)
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
        .order_by(User.id.asc())
    )
    return [(int(uid), str(username or "")) for uid, username in r.all()]


def _format_eligible_users(users: List[Tuple[int, str]]) -> str:
    if not users:
        return ""
    return ";".join(f"{uid}:{name}" for uid, name in users)


def _purchase_probe_acceptable(probe_out: Dict[str, Any]) -> bool:
    if probe_out.get("probe_aborted_max_rounds"):
        return False
    if probe_out.get("probe_aborted_no_new_rows"):
        return False
    pending = probe_out.get("pending_unverified") or []
    return len(pending) == 0


async def batch_login_eligible_runner_users(trigger: str = "manual") -> Dict[str, Any]:
    """对当时 runner_enabled=true 且订阅有效的用户批量交易端 Login，以提前领取代理。"""
    from app.proxy_binding import get_session_manager_for_user_id

    now_utc = dt.datetime.now(dt.timezone.utc)
    async with AsyncSessionLocal() as db:
        users = await _eligible_runner_users(db, now_utc)
    proxy_lifecycle_log(
        "pre_login",
        action="start",
        trigger=trigger,
        eligible_users=len(users),
        eligible_users_detail=_format_eligible_users(users),
    )
    ok_n = 0
    fail_n = 0
    skip_n = 0
    for uid, uname in users:
        try:
            async with AsyncSessionLocal() as db:
                cfg = await load_trading_config(db, uid)
            if cfg is None or not (cfg.username or "").strip() or not (cfg.password or "").strip():
                skip_n += 1
                proxy_lifecycle_log(
                    "pre_login",
                    action="skip",
                    trigger=trigger,
                    user_id=uid,
                    username=uname,
                    reason="no_trading_config",
                )
                continue
            sm = await get_session_manager_for_user_id(uid)
            login_ok, login_err, _res, merged = await verify_trade_credentials(
                sm, cfg.username, cfg.password
            )
            if login_ok:
                async with AsyncSessionLocal() as db:
                    slot = await get_active_trading_slot(db, uid)
                    await persist_trading_config(db, uid, slot, merged)
                    await db.commit()
                st = await get_or_create_state(uid)
                st.config = merged
                st.logged_in = True
                ok_n += 1
                proxy_lifecycle_log(
                    "pre_login",
                    action="ok",
                    trigger=trigger,
                    user_id=uid,
                    username=uname,
                )
            else:
                fail_n += 1
                proxy_lifecycle_log(
                    "pre_login",
                    action="fail",
                    trigger=trigger,
                    user_id=uid,
                    username=uname,
                    error=(login_err or "")[:200],
                )
        except Exception as ex:
            fail_n += 1
            proxy_lifecycle_log(
                "pre_login",
                action="error",
                trigger=trigger,
                user_id=uid,
                username=uname,
                error=repr(ex),
            )
    proxy_lifecycle_log(
        "pre_login",
        action="complete",
        trigger=trigger,
        eligible_users=len(users),
        ok=ok_n,
        fail=fail_n,
        skip=skip_n,
    )
    return {
        "pre_login_eligible": len(users),
        "pre_login_ok": ok_n,
        "pre_login_fail": fail_n,
        "pre_login_skip": skip_n,
    }


async def _pool_inventory_snapshot(db: AsyncSession) -> Dict[str, int]:
    assignable_pool = int(
        (
            await db.execute(
                select(func.count())
                .select_from(ProxyPoolEntry)
                .where(
                    ProxyPoolEntry.is_active.is_(True),
                    ProxyPoolEntry.assignment_allowed.is_(True),
                )
            )
        ).scalar_one()
        or 0
    )
    idle_assignable = int(
        (
            await db.execute(
                select(func.count())
                .select_from(ProxyPoolEntry)
                .where(
                    ProxyPoolEntry.is_active.is_(True),
                    ProxyPoolEntry.assignment_allowed.is_(True),
                    ProxyPoolEntry.assigned_user_id.is_(None),
                )
            )
        ).scalar_one()
        or 0
    )
    active_total = int(
        (
            await db.execute(
                select(func.count())
                .select_from(ProxyPoolEntry)
                .where(ProxyPoolEntry.is_active.is_(True))
            )
        ).scalar_one()
        or 0
    )
    return {
        "assignable_pool": assignable_pool,
        "idle_assignable": idle_assignable,
        "active_total": active_total,
    }


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
            proxy_lifecycle_log(
                "probe",
                action="release_skip_locked",
                pool_entry_id=pid,
                instance_id=iid,
            )
            continue
        try:
            await asyncio.to_thread(delete_instance_sync, iid)
            proxy_lifecycle_log(
                "probe",
                action="release_failed_instance",
                pool_entry_id=pid,
                instance_id=iid,
            )
        except Exception as ex:
            proxy_lifecycle_log(
                "probe",
                action="release_failed_instance_error",
                pool_entry_id=pid,
                instance_id=iid,
                error=repr(ex),
            )


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
            proxy_lifecycle_log(
                "probe",
                action="abort_max_rounds",
                trigger=trigger,
                max_rounds=max_rounds,
                pending_pool_entry_ids=",".join(str(x) for x in cur),
            )
            return {
                "probe_rounds": rounds_done - 1,
                "probe_replacements_total": replacements_total,
                "pending_unverified": cur,
                "probe_aborted_max_rounds": True,
            }

        proxy_lifecycle_log(
            "probe",
            action="wait_before_round",
            trigger=trigger,
            round=rounds_done,
            pending_count=len(cur),
            delay_seconds=f"{delay:.0f}",
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
            proxy_lifecycle_log(
                "probe",
                action="abort_missing_rows",
                trigger=trigger,
                round=rounds_done,
                pending_pool_entry_ids=",".join(str(x) for x in cur),
            )
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
        for (pid, url), raw in zip(pairs, raw_results):
            if isinstance(raw, Exception):
                proxy_lifecycle_log(
                    "probe",
                    action="result",
                    trigger=trigger,
                    round=rounds_done,
                    pool_entry_id=pid,
                    proxy_url=url,
                    proxy_ok=False,
                    error=repr(raw),
                )
                failed.append(pid)
                continue
            ok = bool(raw.get("proxy_ok"))
            proxy_lifecycle_log(
                "probe",
                action="result",
                trigger=trigger,
                round=rounds_done,
                pool_entry_id=pid,
                proxy_url=url,
                proxy_ok=ok,
                http_status=int(raw.get("http_status") or 0),
                verdict=str(raw.get("verdict") or ""),
                verdict_detail=str(raw.get("verdict_detail") or ""),
            )
            if ok:
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
            proxy_lifecycle_log(
                "probe",
                action="round_complete",
                trigger=trigger,
                round=rounds_done,
                passed_count=len(passed),
                failed_count=0,
            )
            return {
                "probe_rounds": rounds_done,
                "probe_replacements_total": replacements_total,
                "pending_unverified": [],
                "probe_aborted_max_rounds": False,
            }

        proxy_lifecycle_log(
            "probe",
            action="round_complete",
            trigger=trigger,
            round=rounds_done,
            passed_count=len(passed),
            failed_count=len(failed),
            failed_pool_entry_ids=",".join(str(x) for x in failed),
            replace_count=len(failed),
        )
        await _release_failed_pool_entries_after_probe(failed)
        k = len(failed)
        try:
            new_ids, rep_rid, new_ip_map = await asyncio.to_thread(run_instances_then_poll_public_ips_sync, k)
        except Exception as ex:
            proxy_lifecycle_log(
                "probe",
                action="replace_run_instances_failed",
                trigger=trigger,
                round=rounds_done,
                error=repr(ex),
            )
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
            proxy_lifecycle_log(
                "probe",
                action="replace_pool_incomplete",
                trigger=trigger,
                round=rounds_done,
                want=k,
                got_rows=len(cur2),
                skipped_no_ip=skipped_ni,
                skipped_duplicate=skipped_d,
                request_id=rep_rid,
            )
        if not cur2:
            proxy_lifecycle_log(
                "probe",
                action="abort_no_new_rows",
                trigger=trigger,
                round=rounds_done,
            )
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
    proxy_lifecycle_log("purchase", action="start", trigger=trigger)
    enabled_eff, multiplier = await _effective_auto_purchase_policy()
    if not enabled_eff:
        proxy_lifecycle_log("purchase", action="skip", trigger=trigger, reason="policy_disabled")
        return {"eligible_users": 0, "multiplier": int(multiplier), "to_buy": 0}
    if not aliyun_ecs_run_configured():
        proxy_lifecycle_log("purchase", action="skip", trigger=trigger, reason="aliyun_ecs_not_configured")
        return {"eligible_users": 0, "multiplier": int(multiplier), "to_buy": 0}

    now_utc = dt.datetime.now(dt.timezone.utc)
    pending: List[int] = []

    async with AsyncSessionLocal() as db:
        eligible_users_list = await _eligible_runner_users(db, now_utc)
        eligible_users = len(eligible_users_list)
        pool_inv = await _pool_inventory_snapshot(db)
        assignable_pool = int(pool_inv["assignable_pool"])

        target_total = eligible_users * multiplier
        to_buy = max(0, target_total - assignable_pool)
        proxy_lifecycle_log(
            "purchase",
            action="capacity_check",
            trigger=trigger,
            enabled=enabled_eff,
            multiplier=multiplier,
            eligible_users=eligible_users,
            eligible_users_detail=_format_eligible_users(eligible_users_list),
            assignable_pool=assignable_pool,
            idle_assignable=pool_inv["idle_assignable"],
            active_total=pool_inv["active_total"],
            target_total=target_total,
            to_buy=to_buy,
        )
        if to_buy <= 0:
            proxy_lifecycle_log(
                "purchase",
                action="skip",
                trigger=trigger,
                reason="pool_sufficient",
                eligible_users=eligible_users,
                multiplier=multiplier,
                assignable_pool=assignable_pool,
                target_total=target_total,
                to_buy=0,
            )
            out = {
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
            if eligible_users > 0:
                out.update(await batch_login_eligible_runner_users(trigger=trigger))
            return out

        proxy_lifecycle_log(
            "purchase",
            action="run_instances_start",
            trigger=trigger,
            to_buy=to_buy,
        )
        ids, req_id, ip_map = await asyncio.to_thread(run_instances_then_poll_public_ips_sync, to_buy)
        pending, added, skipped_no_ip, skipped_dup = await _insert_auto_purchase_pool_rows(db, ids, ip_map)
        await db.commit()
        proxy_lifecycle_log(
            "purchase",
            action="run_instances_complete",
            trigger=trigger,
            eligible_users=eligible_users,
            multiplier=multiplier,
            assignable_pool=assignable_pool,
            target_total=target_total,
            to_buy=to_buy,
            created_instances=len(ids),
            instance_ids=",".join(ids),
            added_pending_probe=added,
            skipped_no_ip=skipped_no_ip,
            skipped_duplicate=skipped_dup,
            request_id=req_id,
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
        proxy_lifecycle_log(
            "purchase",
            action="probe_begin",
            trigger=trigger,
            pending_pool_entry_ids=",".join(str(x) for x in pending),
            pending_count=len(pending),
        )
        probe_out = await _probe_replace_until_all_ok(pending, trigger=trigger)
    else:
        probe_out = {
            "probe_rounds": 0,
            "probe_replacements_total": 0,
            "pending_unverified": [],
            "probe_aborted_max_rounds": False,
        }
        if to_buy > 0:
            proxy_lifecycle_log(
                "purchase",
                action="probe_skipped",
                trigger=trigger,
                reason="no_pool_rows_added",
                to_buy=to_buy,
            )

    snapshot.update(probe_out)
    if int(snapshot.get("eligible_users") or 0) > 0 and _purchase_probe_acceptable(probe_out):
        snapshot.update(await batch_login_eligible_runner_users(trigger=trigger))
    proxy_lifecycle_log(
        "purchase",
        action="complete",
        trigger=trigger,
        eligible_users=snapshot.get("eligible_users"),
        multiplier=snapshot.get("multiplier"),
        assignable_pool=snapshot.get("assignable_pool"),
        target_total=snapshot.get("target_total"),
        to_buy=snapshot.get("to_buy"),
        created_instances=snapshot.get("created_instances", 0),
        added=snapshot.get("added", 0),
        skipped_no_ip=snapshot.get("skipped_no_ip", 0),
        skipped_duplicate=snapshot.get("skipped_duplicate", 0),
        request_id=snapshot.get("request_id", ""),
        probe_rounds=snapshot.get("probe_rounds", 0),
        probe_replacements_total=snapshot.get("probe_replacements_total", 0),
        pending_unverified=",".join(str(x) for x in (snapshot.get("pending_unverified") or [])),
    )
    return snapshot


async def auto_release_proxy_servers_once(trigger: str = "daily-1220") -> Dict[str, int]:
    """
    每日释放代理服务器（跳过锁定实例），并清理对应代理池条目。
    规则：遍历当前地域 ECS，遇到未锁定实例即调用 DeleteInstance(force)。
    """
    proxy_lifecycle_log("release", action="start", trigger=trigger)
    if not bool(settings.proxy_auto_release_enabled):
        proxy_lifecycle_log("release", action="skip", trigger=trigger, reason="policy_disabled")
        return {"total": 0, "locked": 0, "released": 0}
    if not aliyun_ecs_run_configured():
        proxy_lifecycle_log("release", action="skip", trigger=trigger, reason="aliyun_ecs_not_configured")
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
            proxy_lifecycle_log(
                "release",
                action="skip_locked",
                trigger=trigger,
                instance_id=iid,
                public_ip=str(row.get("public_ip") or "").strip(),
            )
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
            proxy_lifecycle_log(
                "release",
                action="released",
                trigger=trigger,
                instance_id=iid,
                public_ip=public_ip,
                removed_pool_entries=len(entries),
            )
        except Exception as ex:
            proxy_lifecycle_log(
                "release",
                action="delete_failed",
                trigger=trigger,
                instance_id=iid,
                public_ip=public_ip,
                error=repr(ex),
            )

    proxy_lifecycle_log(
        "release",
        action="complete",
        trigger=trigger,
        total=len(all_rows),
        locked=len(locked),
        released=released,
        removed_pool_entries=removed_pool_entries,
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


PRE_SELL_PURCHASE_MINUTES_BEFORE = 10


def seconds_until_next_pre_sell_purchase(sell_hhmm: str, *, minutes_before: int = PRE_SELL_PURCHASE_MINUTES_BEFORE) -> float:
    """距下一次「全站开售前 N 分钟」补购触发的秒数（北京时间）。"""
    p = parse_hhmm(sell_hhmm)
    if not p:
        return 86400.0
    h, mi = p
    now = beijing_now()
    sell = dt.datetime(now.year, now.month, now.day, h, mi, 0, tzinfo=BJ)
    trigger = sell - dt.timedelta(minutes=max(0, int(minutes_before)))
    if now >= sell:
        trigger = trigger + dt.timedelta(days=1)
    elif now >= trigger:
        return 1.0
    return max(1.0, (trigger - now).total_seconds())
