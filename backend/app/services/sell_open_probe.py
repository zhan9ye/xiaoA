"""开门侦察：双探 + 短超时；命中则 signal_sell_open。含管理端校准日志缓冲。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from app.db import AsyncSessionLocal
from app.platform_settings_repo import get_platform_sell_start_time, get_sell_open_probe_config
from app.proxy_lifecycle_log import proxy_lifecycle_log
from app.rpc_v import compute_js_timespan_v
from app.services.ace_sell_son_service import post_ace_sell_son
from app.services.beijing_time import BJ, beijing_now, today_prep_and_start
from app.services.channel_closed import response_day_sell_end_reason
from app.services.login_response_parse import merge_from_rpc_login
from app.services.login_service import rpc_login
from app.services.mnemonic_rpc_service import parse_mnemonic_get01_response, post_mnemonic_get01
from app.services.mnemonic_segments import derive_mnemonic_str1
from app.services.sell_open_gate import reset_sell_open_for_today, sell_open_is_set, signal_sell_open
from app.services.sell_open_probe_config import (
    SellOpenProbeConfig,
    effective_open_timeout_ms,
    effective_probe_cooldown_ms,
)
from app.services.session_manager import SessionManager
from app.services.shared_proxy_runtime import (
    ProxyLease,
    get_shared_proxy_runtime,
    reload_shared_proxy_runtime_from_db,
)
from app.services.totp_util import totp_now_from_secret_ex
from app.trading_config_repo import load_trading_config

NOT_OPEN_PREFIX = "当前可售额度已达上限.本日交易通道已关闭"

_calib_logs: Deque[str] = deque(maxlen=500)
_calib_lock = asyncio.Lock()
_rtt_samples_ms: Deque[float] = deque(maxlen=64)
_probe_task: Optional[asyncio.Task] = None
_calib_task: Optional[asyncio.Task] = None
_early_429_alert = False


def _push_calib(msg: str) -> None:
    line = f"{beijing_now().strftime('%H:%M:%S.%f')[:-3]} {msg}"
    _calib_logs.appendleft(line)


def get_calibration_log_lines(limit: int = 200) -> List[str]:
    n = max(1, min(500, int(limit)))
    return list(_calib_logs)[:n]


def early_429_alert_active() -> bool:
    return bool(_early_429_alert)


def clear_early_429_alert() -> None:
    global _early_429_alert
    _early_429_alert = False


def _p95(samples: List[float]) -> Optional[float]:
    if not samples:
        return None
    xs = sorted(samples)
    idx = min(len(xs) - 1, max(0, int(round(0.95 * (len(xs) - 1)))))
    return float(xs[idx])


def _is_not_open_msg(parsed: Any, raw: str) -> bool:
    reason = response_day_sell_end_reason(parsed, raw)
    if reason and ("可售额度" in reason or "交易通道" in reason or "額度" in reason or "通道" in reason):
        return True
    blob = (raw or "") + (str(parsed) if parsed is not None else "")
    return NOT_OPEN_PREFIX in blob or "本日交易通道已關閉" in blob or "本日交易通道已关闭" in blob


def _is_timeout_result(ok: bool, code: int, raw: str) -> bool:
    if (raw or "").startswith("timeout:"):
        return True
    return False


@dataclass
class _ProbeRoundResult:
    opened: bool
    reason: str
    elapsed_ms: List[float] = field(default_factory=list)
    got_429: bool = False
    early_429: bool = False


async def _ensure_probe_session(
    cfg: SellOpenProbeConfig,
) -> Tuple[Optional[SessionManager], Optional[dict], str]:
    """返回 (sm, fields, error)。Login/助记词走侦察池借出的出口（会话保持至探测结束）。"""
    if not cfg.probe_user_id:
        return None, None, "未配置探测账号 probe_user_id"
    async with AsyncSessionLocal() as db:
        tcfg = await load_trading_config(db, int(cfg.probe_user_id))
    if tcfg is None:
        return None, None, f"探测用户 {cfg.probe_user_id} 无交易配置"

    rt = get_shared_proxy_runtime()
    boot_lease = await rt.acquire("probe", wait=False)
    proxy_urls: List[str] = []
    proxy_labels: List[Optional[str]] = []
    if boot_lease is not None:
        proxy_urls = [boot_lease.proxy_url]
        proxy_labels = [boot_lease.label or None]
        # 登录占用整场探测窗口：用较长冷却归还，避免登录期被别人借走同一出口打乱 Cookie 路径
        await rt.release(
            boot_lease,
            cooldown_ms=max(cfg.probe_cooldown_default_ms, 1000),
            force_cooldown=True,
            hit_success=False,
        )

    sm = SessionManager(
        platform_user_id=int(cfg.probe_user_id),
        proxy_urls=proxy_urls,
        proxy_labels=proxy_labels,
    )
    login_res = await rpc_login(sm, tcfg.username, tcfg.password, v=compute_js_timespan_v())
    if not login_res.ok:
        return None, None, f"探测账号 Login 失败 HTTP {login_res.status_code}"
    merged, changed = merge_from_rpc_login(tcfg, login_res.response_body)
    if not changed and not (merged.rpc_login_key or "").strip():
        return None, None, "探测账号 Login 未解析出 Key/UserID"
    rk = (merged.rpc_login_key or "").strip()
    uid = (merged.rpc_user_id or "").strip()
    if not rk or not uid:
        return None, None, "探测账号 Login 后缺少 rpc key/user_id"

    v_m = compute_js_timespan_v()
    ok_m, code_m, parsed_m, _raw_m = await post_mnemonic_get01(
        sm, rpc_key=rk, user_id=uid, v=v_m
    )
    meta = parse_mnemonic_get01_response(parsed_m) if ok_m else None
    if not meta:
        return None, None, f"探测账号 Mnemonic_Get01 失败 HTTP {code_m}"
    mid = meta["mnemonicid1"]
    mkey = meta["mnemonickey"]
    mstr = derive_mnemonic_str1(merged.mnemonic or "", mid)
    if not mstr:
        return None, None, "探测账号无法从助记词推导 mnemonicstr1"
    fields = {
        "password": merged.password,
        "key_token": merged.key_token,
        "rpc_login_key": rk,
        "rpc_user_id": uid,
        "mnemonic_id1": mid,
        "mnemonic_key": mkey,
        "mnemonic_str1": mstr,
    }
    return sm, fields, ""


async def _one_probe_shot(
    sm: SessionManager,
    fields: dict,
    lease: ProxyLease,
    timeout_ms: int,
) -> Tuple[str, float, int]:
    """
    返回 (verdict, elapsed_ms, http_code)
    verdict: not_open | open_msg | open_timeout | error | http_429
    """
    g, g_err = totp_now_from_secret_ex(fields["key_token"])
    if not g:
        return "error", 0.0, 0
    t0 = time.perf_counter()
    ok, code, parsed, raw = await post_ace_sell_son(
        sm,
        amount="1",
        password=fields["password"],
        son_id="",  # 主账户探测，数量 1
        mnemonic_id1=fields["mnemonic_id1"],
        mnemonic_key=fields["mnemonic_key"],
        mnemonic_str1=fields["mnemonic_str1"],
        g_code=g,
        count="1",
        rpc_key=fields["rpc_login_key"],
        user_id=fields["rpc_user_id"],
        v=compute_js_timespan_v(),
        proxy_url_override=lease.proxy_url,
        timeout_seconds=max(0.05, timeout_ms / 1000.0),
    )
    elapsed = (time.perf_counter() - t0) * 1000.0
    if code == 429:
        return "http_429", elapsed, code
    if _is_timeout_result(ok, code, raw):
        return "open_timeout", elapsed, code
    if _is_not_open_msg(parsed, raw):
        return "not_open", elapsed, code
    return "open_msg", elapsed, code


async def _probe_round(
    sm: SessionManager,
    fields: dict,
    cfg: SellOpenProbeConfig,
    *,
    stop_event: Optional[asyncio.Event],
    timeout_ms: int,
    cooldown_ms: int,
) -> _ProbeRoundResult:
    global _early_429_alert
    rt = get_shared_proxy_runtime()
    parallel = max(1, int(cfg.probe_parallel))
    leases: List[ProxyLease] = []
    for _ in range(parallel):
        lease = await rt.acquire("probe", stop_event=stop_event, wait=True)
        if lease is None:
            break
        leases.append(lease)
    if not leases:
        return _ProbeRoundResult(opened=False, reason="no_probe_proxy")

    async def _run(lease: ProxyLease) -> Tuple[ProxyLease, str, float, int]:
        verdict, elapsed, code = await _one_probe_shot(sm, fields, lease, timeout_ms)
        return lease, verdict, elapsed, code

    try:
        results = await asyncio.gather(*[_run(L) for L in leases], return_exceptions=True)
    finally:
        pass

    elapsed_list: List[float] = []
    got_429 = False
    early_429 = False
    any_not_open = False
    any_open = False
    open_reason = ""

    for i, item in enumerate(results):
        lease = leases[i] if i < len(leases) else None
        force_cd = False
        hit_ok = True
        if isinstance(item, Exception):
            if lease:
                await rt.release(lease, cooldown_ms=cooldown_ms, force_cooldown=True)
            continue
        lease, verdict, elapsed, code = item
        elapsed_list.append(elapsed)
        if verdict == "http_429":
            got_429 = True
            # 第 1～2 次突发内就 429：报警
            early_429 = True
            _early_429_alert = True
            force_cd = True
            hit_ok = False
            _push_calib(f"WARN early 429 proxy=#{lease.pool_entry_id} {lease.label}")
            proxy_lifecycle_log(
                "sell_open_probe",
                action="early_429",
                pool_entry_id=lease.pool_entry_id,
                label=lease.label,
            )
        elif verdict == "not_open":
            any_not_open = True
            _rtt_samples_ms.append(elapsed)
        elif verdict in ("open_timeout", "open_msg"):
            any_open = True
            open_reason = verdict
            force_cd = True
        await rt.release(
            lease,
            cooldown_ms=cooldown_ms,
            force_cooldown=force_cd,
            hit_success=hit_ok,
        )

    if any_not_open:
        return _ProbeRoundResult(
            opened=False,
            reason="not_open",
            elapsed_ms=elapsed_list,
            got_429=got_429,
            early_429=early_429,
        )
    if any_open:
        return _ProbeRoundResult(
            opened=True,
            reason=open_reason or "open",
            elapsed_ms=elapsed_list,
            got_429=got_429,
            early_429=early_429,
        )
    # 全部超时且无 not_open → 已开
    if elapsed_list and all(e >= timeout_ms * 0.9 for e in elapsed_list):
        return _ProbeRoundResult(opened=True, reason="all_timeout", elapsed_ms=elapsed_list)
    return _ProbeRoundResult(opened=False, reason="inconclusive", elapsed_ms=elapsed_list)


async def run_sell_open_probe_until_open(
    *,
    stop_event: Optional[asyncio.Event] = None,
    calibration: bool = False,
) -> str:
    """
    从当前时刻起在探测窗口内循环双探，直到开门或窗口结束。
    返回 reason 字符串。
    """
    await reload_shared_proxy_runtime_from_db()
    async with AsyncSessionLocal() as db:
        cfg = await get_sell_open_probe_config(db)
        sell_hhmm = await get_platform_sell_start_time(db)

    if calibration:
        cfg.calibration_enabled = True
    if not cfg.probe_enabled and not calibration:
        return "probe_disabled"

    await reset_sell_open_for_today(force=True)
    sm, fields, err = await _ensure_probe_session(cfg)
    if err or sm is None or fields is None:
        _push_calib(f"ERR {err}")
        proxy_lifecycle_log("sell_open_probe", action="prep_failed", error=err)
        return f"prep_failed:{err}"

    window = max(10, int(cfg.probe_window_seconds))
    deadline = beijing_now().timestamp() + window
    # 若尚未到 T_open，先等到 T_open（校准模式可立即打）
    if not calibration:
        pair = today_prep_and_start(sell_hhmm)
        if pair:
            _prep, start_dt = pair
            while beijing_now() < start_dt:
                if stop_event is not None and stop_event.is_set():
                    return "stopped"
                if sell_open_is_set():
                    return "already_open"
                await asyncio.sleep(0.05)

    _push_calib(
        f"START probe_user={cfg.probe_user_id} parallel={cfg.probe_parallel} "
        f"timeout~{cfg.open_timeout_default_ms}ms cooldown~{cfg.probe_cooldown_default_ms}ms"
    )
    proxy_lifecycle_log(
        "sell_open_probe",
        action="start",
        calibration=calibration,
        probe_user_id=cfg.probe_user_id,
    )

    rounds = 0
    while beijing_now().timestamp() < deadline:
        if stop_event is not None and stop_event.is_set():
            return "stopped"
        if sell_open_is_set() and not calibration:
            return "already_open"

        baseline = _p95(list(_rtt_samples_ms))
        timeout_ms = effective_open_timeout_ms(cfg, baseline)
        cooldown_ms = effective_probe_cooldown_ms(cfg, None)

        result = await _probe_round(
            sm,
            fields,
            cfg,
            stop_event=stop_event,
            timeout_ms=timeout_ms,
            cooldown_ms=cooldown_ms,
        )
        rounds += 1
        _push_calib(
            f"round#{rounds} opened={result.opened} reason={result.reason} "
            f"rtt={','.join(f'{x:.0f}' for x in result.elapsed_ms)} "
            f"timeout={timeout_ms} baseline_p95={baseline}"
        )

        if result.opened and not calibration:
            first = await signal_sell_open(result.reason)
            proxy_lifecycle_log(
                "sell_open_probe",
                action="opened",
                reason=result.reason,
                first=first,
                rounds=rounds,
            )
            _push_calib(f"OPEN signal reason={result.reason}")
            return f"opened:{result.reason}"

        if calibration and rounds >= 20:
            p95 = _p95(list(_rtt_samples_ms))
            _push_calib(
                f"CALIB done rounds={rounds} rtt_p95={p95} "
                f"suggest_open_timeout={effective_open_timeout_ms(cfg, p95)} "
                f"suggest_probe_cooldown={cfg.probe_cooldown_default_ms}"
            )
            return "calibration_done"

        gap = max(0.01, int(cfg.probe_round_gap_ms) / 1000.0)
        await asyncio.sleep(gap)

    if not calibration and not sell_open_is_set():
        # 窗口结束未命中：不在这里 set；由 Runner 时钟兜底
        _push_calib("WINDOW_END no open signal")
        proxy_lifecycle_log("sell_open_probe", action="window_end", rounds=rounds)
        return "window_end"
    return "done"


async def sell_open_probe_scheduler_loop(stop_event: asyncio.Event) -> None:
    """每日在 T_open 前待命，到点启动探测（与 Runner 并行）。"""
    last_day = ""
    while not stop_event.is_set():
        try:
            async with AsyncSessionLocal() as db:
                cfg = await get_sell_open_probe_config(db)
                sell_hhmm = await get_platform_sell_start_time(db)
            if not cfg.probe_enabled or not cfg.probe_user_id:
                await asyncio.wait_for(stop_event.wait(), timeout=5.0)
                continue
            pair = today_prep_and_start(sell_hhmm)
            if not pair:
                await asyncio.wait_for(stop_event.wait(), timeout=5.0)
                continue
            _prep, start_dt = pair
            today = start_dt.date().isoformat()
            now = beijing_now()
            if now.date().isoformat() != today:
                await asyncio.sleep(1.0)
                continue
            if last_day == today:
                # 今日已跑过；睡到次日
                await asyncio.wait_for(stop_event.wait(), timeout=30.0)
                continue
            # 等到 T_open - 1s 再进入探测（探测函数内部也会对齐 T_open）
            wait_s = (start_dt - now).total_seconds() - 1.0
            if wait_s > 0:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=min(wait_s, 30.0))
                    if stop_event.is_set():
                        break
                    continue
                except asyncio.TimeoutError:
                    pass
                continue
            last_day = today
            await reload_shared_proxy_runtime_from_db()
            await run_sell_open_probe_until_open(stop_event=stop_event, calibration=False)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            proxy_lifecycle_log("sell_open_probe", action="scheduler_error", error=repr(ex))
            _push_calib(f"SCHED_ERR {ex!r}")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass


async def start_calibration_background() -> str:
    global _calib_task
    async with AsyncSessionLocal() as db:
        cfg = await get_sell_open_probe_config(db)
    if not cfg.calibration_enabled:
        return "calibration_disabled"
    if _calib_task is not None and not _calib_task.done():
        return "already_running"

    async def _runner() -> None:
        await run_sell_open_probe_until_open(calibration=True)

    _calib_task = asyncio.create_task(_runner())
    return "started"


async def stop_calibration_background() -> str:
    global _calib_task
    if _calib_task is None or _calib_task.done():
        _calib_task = None
        return "not_running"
    _calib_task.cancel()
    try:
        await _calib_task
    except asyncio.CancelledError:
        pass
    _calib_task = None
    _push_calib("CALIB stopped by admin")
    return "stopped"
