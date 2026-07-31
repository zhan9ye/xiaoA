"""进程内共享代理运行时：售卖池 / 侦察池 acquire → release(cooldown) + FIFO 等待。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

from app.proxy_lifecycle_log import proxy_lifecycle_log

PoolRole = Literal["sell", "probe"]


@dataclass
class ProxyLease:
    pool_entry_id: int
    proxy_url: str
    label: str
    role: PoolRole
    acquired_at: float = field(default_factory=time.monotonic)


@dataclass
class _ProxySlot:
    pool_entry_id: int
    proxy_url: str
    label: str
    role: PoolRole
    is_active: bool = True
    cool_until: float = 0.0
    in_use: bool = False
    hits_in_burst: int = 0
    max_hits_before_cooldown: int = 2


class SharedProxyRuntime:
    """
    单机进程内代理调度。
    - sell：每次借出 1 条，归还后冷却 sell_cooldown
    - probe：可配置连续 hits 后再强制冷却；也可每次归还都冷却
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cond = asyncio.Condition(self._lock)
        self._slots: Dict[int, _ProxySlot] = {}
        self._sell_cooldown_ms = 11000
        self._probe_cooldown_ms = 3000
        self._probe_max_hits = 2

    def configure_cooldowns(
        self,
        *,
        sell_cooldown_ms: int,
        probe_cooldown_ms: int,
        probe_max_hits: int = 2,
    ) -> None:
        self._sell_cooldown_ms = max(0, int(sell_cooldown_ms))
        self._probe_cooldown_ms = max(0, int(probe_cooldown_ms))
        self._probe_max_hits = max(1, int(probe_max_hits))

    async def sync_from_rows(
        self,
        rows: List[dict],
    ) -> None:
        """
        rows: [{id, proxy_url, label, is_active, pool_role, assignment_allowed?}, ...]
        仅同步元数据；保留 in_use / cool_until。
        """
        async with self._cond:
            seen = set()
            for r in rows:
                pid = int(r["id"])
                seen.add(pid)
                role = (r.get("pool_role") or "sell").strip() or "sell"
                if role not in ("sell", "probe"):
                    role = "sell"
                url = (r.get("proxy_url") or "").strip()
                if not url:
                    continue
                active = bool(r.get("is_active", True))
                # 售卖池：未放行的不进 runtime；侦察池：启用即可（可烧）
                if role == "sell" and not bool(r.get("assignment_allowed", True)):
                    active = False
                lab = (r.get("label") or "").strip()
                cur = self._slots.get(pid)
                if cur is None:
                    self._slots[pid] = _ProxySlot(
                        pool_entry_id=pid,
                        proxy_url=url,
                        label=lab,
                        role=role,  # type: ignore[arg-type]
                        is_active=active,
                        max_hits_before_cooldown=self._probe_max_hits,
                    )
                else:
                    cur.proxy_url = url
                    cur.label = lab
                    cur.role = role  # type: ignore[assignment]
                    cur.is_active = active
                    cur.max_hits_before_cooldown = self._probe_max_hits
            for pid in list(self._slots.keys()):
                if pid not in seen:
                    slot = self._slots[pid]
                    if not slot.in_use:
                        del self._slots[pid]
            self._cond.notify_all()

    def _pick_ready(self, role: PoolRole, now: float) -> Optional[_ProxySlot]:
        candidates = [
            s
            for s in self._slots.values()
            if s.role == role and s.is_active and not s.in_use and s.cool_until <= now and s.proxy_url
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda s: (s.cool_until, s.pool_entry_id))
        return candidates[0]

    def _next_ready_at(self, role: PoolRole, now: float) -> Optional[float]:
        times = [
            s.cool_until
            for s in self._slots.values()
            if s.role == role and s.is_active and not s.in_use and s.proxy_url and s.cool_until > now
        ]
        return min(times) if times else None

    async def acquire(
        self,
        role: PoolRole,
        *,
        stop_event: Optional[asyncio.Event] = None,
        wait: bool = True,
    ) -> Optional[ProxyLease]:
        while True:
            if stop_event is not None and stop_event.is_set():
                return None
            async with self._cond:
                now = time.monotonic()
                slot = self._pick_ready(role, now)
                if slot is not None:
                    slot.in_use = True
                    return ProxyLease(
                        pool_entry_id=slot.pool_entry_id,
                        proxy_url=slot.proxy_url,
                        label=slot.label,
                        role=role,
                    )
                if not wait:
                    return None
                nxt = self._next_ready_at(role, now)
                timeout = 0.05
                if nxt is not None:
                    timeout = max(0.01, min(1.0, nxt - now))
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass

    async def release(
        self,
        lease: ProxyLease,
        *,
        cooldown_ms: Optional[int] = None,
        force_cooldown: bool = False,
        hit_success: bool = True,
    ) -> None:
        async with self._cond:
            slot = self._slots.get(lease.pool_entry_id)
            if slot is None:
                self._cond.notify_all()
                return
            slot.in_use = False
            now = time.monotonic()
            if lease.role == "probe":
                if hit_success:
                    slot.hits_in_burst += 1
                cd = (
                    int(cooldown_ms)
                    if cooldown_ms is not None
                    else int(self._probe_cooldown_ms)
                )
                need_cd = force_cooldown or slot.hits_in_burst >= max(
                    1, slot.max_hits_before_cooldown
                )
                if need_cd:
                    slot.cool_until = now + max(0, cd) / 1000.0
                    slot.hits_in_burst = 0
            else:
                cd = (
                    int(cooldown_ms)
                    if cooldown_ms is not None
                    else int(self._sell_cooldown_ms)
                )
                slot.cool_until = now + max(0, cd) / 1000.0
                slot.hits_in_burst = 0
            self._cond.notify_all()

    async def snapshot(self) -> Dict[str, int]:
        async with self._lock:
            now = time.monotonic()
            sell = probe = 0
            sell_ready = probe_ready = 0
            sell_busy = probe_busy = 0
            for s in self._slots.values():
                if not s.is_active:
                    continue
                if s.role == "sell":
                    sell += 1
                    if s.in_use:
                        sell_busy += 1
                    elif s.cool_until <= now:
                        sell_ready += 1
                else:
                    probe += 1
                    if s.in_use:
                        probe_busy += 1
                    elif s.cool_until <= now:
                        probe_ready += 1
            return {
                "sell_total": sell,
                "sell_ready": sell_ready,
                "sell_busy": sell_busy,
                "probe_total": probe,
                "probe_ready": probe_ready,
                "probe_busy": probe_busy,
            }


_runtime = SharedProxyRuntime()


def get_shared_proxy_runtime() -> SharedProxyRuntime:
    return _runtime


async def reload_shared_proxy_runtime_from_db() -> None:
    from sqlalchemy import select

    from app.db import AsyncSessionLocal
    from app.models import ProxyPoolEntry
    from app.platform_settings_repo import get_sell_open_probe_config

    async with AsyncSessionLocal() as db:
        cfg = await get_sell_open_probe_config(db)
        r = await db.execute(select(ProxyPoolEntry))
        rows = list(r.scalars().all())
    rt = get_shared_proxy_runtime()
    rt.configure_cooldowns(
        sell_cooldown_ms=cfg.sell_cooldown_default_ms,
        probe_cooldown_ms=cfg.probe_cooldown_default_ms,
        probe_max_hits=cfg.probe_per_proxy_max_hits,
    )
    await rt.sync_from_rows(
        [
            {
                "id": e.id,
                "proxy_url": e.proxy_url,
                "label": e.label or "",
                "is_active": bool(e.is_active),
                "pool_role": getattr(e, "pool_role", None) or "sell",
                "assignment_allowed": bool(getattr(e, "assignment_allowed", True)),
            }
            for e in rows
        ]
    )
    snap = await rt.snapshot()
    proxy_lifecycle_log("shared_pool", action="reload", **snap)
