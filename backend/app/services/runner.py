import asyncio
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from app.schemas import AppConfigIn
from app.services.ace_sell_son_service import post_ace_sell_son
from app.services.beijing_time import (
    BJ,
    beijing_now,
    beijing_today_str,
    seconds_until_beijing,
    seconds_until_next_beijing_midnight,
    today_prep_and_start,
    wait_interruptible_until_beijing,
    wait_open_phases_beijing,
)
from app.services.channel_closed import response_indicates_channel_closed
from app.services.login_response_parse import merge_from_rpc_login
from app.services.login_service import rpc_login
from app.services.log_hub import LogHub, LogLevel
from app.services.mnemonic_rpc_service import parse_mnemonic_get01_response, post_mnemonic_get01
from app.services.mnemonic_segments import derive_mnemonic_str1, split_mnemonic_csv
from app.services.runner_fetch_guard import set_sub_fetch_allowed
from app.services.runner_lease import get_runner_lease_holder_id, renew_runner_lease_if_holder, try_acquire_runner_lease
from app.services.rpc_auth_signals import json_indicates_rpc_not_logged_in
from app.services.selling_eligibility import (
    ace_amount_string_for_rpc,
    ace_sell_rpc_son_id,
    ace_sell_track_id,
    no_pending_subaccounts_for_sell,
    parse_main_account_info_json,
    resolve_subaccount_display_name,
    runner_listing_amount_for_row,
    sort_subaccounts_for_sell,
    subaccount_eligible_for_ace_sell,
)
from app.services.sold_son_store import add_sold_son_json, sold_son_ids_for_today
from app.services.subaccount_service import fetch_all_subaccounts
from app.services.totp_util import totp_now_from_secret_ex
from app.rpc_v import compute_js_timespan_v
from app.settings import settings
from app.state import AppState
from app.trading_config_repo import persist_trading_config_standalone


async def _wait_interruptible(state: AppState, seconds: float) -> None:
    try:
        await asyncio.wait_for(state.stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _wait_interruptible_until_sell_resync(
    state: AppState, seconds: float, sell_rev: int
) -> bool:
    """True 表示全站开售时间已变更，调用方应重算调度。"""
    from app.platform_settings_repo import platform_sell_time_revision

    rem = max(0.0, float(seconds))
    while rem > 0 and not state.stop_event.is_set():
        if platform_sell_time_revision() != sell_rev:
            return True
        chunk = min(rem, 2.0)
        await _wait_interruptible(state, chunk)
        rem -= chunk
    return platform_sell_time_revision() != sell_rev


async def _sleep_between_sell_requests(state: AppState, ms: int) -> None:
    await _wait_interruptible(state, max(0.0, float(ms) / 1000.0))


async def _wait_until_scheduled_first_sell_batch(
    state: AppState,
    log_hub: LogHub,
    sell_start_beijing: datetime,
) -> bool:
    """
    定时开售：首轮 ACE 不早于「开售整点 + SELL_CHANNEL_CLOSED_GRACE_RETRY_MS」。
    返回 False 表示任务已停止。
    """
    grace_ms = max(0, int(settings.sell_channel_closed_grace_retry_ms or 0))
    first_at = sell_start_beijing + timedelta(milliseconds=grace_ms)
    if first_at.tzinfo is None:
        first_at = first_at.replace(tzinfo=BJ)
    now = beijing_now()
    if now >= first_at:
        return True
    await log_hub.push(
        LogLevel.info,
        (
            f"等待首轮售卖：北京时间 {first_at.strftime('%H:%M:%S')} 起并发"
            f"（开售 + {grace_ms}ms）"
        ),
    )
    return await wait_interruptible_until_beijing(state.stop_event, first_at)


def _clear_sell_mnemonic_cache(state: AppState) -> None:
    state.sell_mnemonic_id1 = ""
    state.sell_mnemonic_key = ""
    state.sell_mnemonic_str1 = ""


async def _reload_runner_config_from_db(user_id: int, state: AppState) -> Optional[AppConfigIn]:
    """从 DB 重载当前活动槽配置（避免任务常驻内存中的 cfg 与刚保存的不一致）。"""
    from app.db import AsyncSessionLocal
    from app.trading_config_repo import get_active_trading_slot, load_trading_config_slot

    async with AsyncSessionLocal() as session:
        slot = await get_active_trading_slot(session, user_id)
        cfg = await load_trading_config_slot(session, user_id, slot)
    if cfg is not None:
        state.config = cfg
        state.loaded_config_slot = slot
    return cfg


async def _refresh_sell_mnemonic_cache(
    state: AppState,
    sm,
    log_hub: LogHub,
    cfg: AppConfigIn,
) -> bool:
    """子账号列表就绪后调用一次 Mnemonic_Get01，写入 state 缓存。"""
    rk = cfg.rpc_login_key.strip()
    uid = cfg.rpc_user_id.strip()
    if not rk or not uid:
        return False
    v_mn = compute_js_timespan_v()
    ok_m, code_m, parsed_m, _raw_m = await post_mnemonic_get01(
        sm, rpc_key=rk, user_id=uid, v=v_mn, lang="cn"
    )
    if ok_m and json_indicates_rpc_not_logged_in(parsed_m):
        state.logged_in = False
        await log_hub.push(LogLevel.warn, "Mnemonic_Get01 返回用戶未登錄")
        return False
    meta = parse_mnemonic_get01_response(parsed_m) if ok_m else None
    if not meta:
        await log_hub.push(
            LogLevel.warn,
            f"Mnemonic_Get01 失败 HTTP {code_m if ok_m else '?'}，无法缓存助记词",
        )
        return False
    mid1 = str(meta["mnemonicid1"])
    mkey = meta["mnemonickey"]
    mstr = derive_mnemonic_str1(cfg.mnemonic, mid1) or ""
    if not mstr:
        segs = [p for p in split_mnemonic_csv(cfg.mnemonic or "") if p]
        await log_hub.push(
            LogLevel.error,
            (
                "Mnemonic_Get01 接口已成功，但无法从交易配置「助记词/备注」推导 mnemonicstr1："
                f"接口 mnemonicid1={mid1}，配置有效段数={len(segs)}（需覆盖第 {mid1} 段）；"
                "请确认当前活动账户槽已保存 12 段逗号分隔助记词（每段 4 个数字/英文/中文字符），或停止任务后重新「开始」以重载配置"
            ),
        )
        return False
    state.sell_mnemonic_id1 = mid1
    state.sell_mnemonic_key = mkey
    state.sell_mnemonic_str1 = mstr
    return True


async def _ensure_sell_mnemonic_cached(
    state: AppState,
    sm,
    log_hub: LogHub,
    cfg: AppConfigIn,
) -> bool:
    """三者齐全则视为缓存命中；任一缺失则调用 Mnemonic_Get01 一次并重写缓存。"""
    if state.sell_mnemonic_id1 and state.sell_mnemonic_key and state.sell_mnemonic_str1:
        return True
    return await _refresh_sell_mnemonic_cache(state, sm, log_hub, cfg)


async def _rpc_login_merge_config(
    user_id: int,
    state: AppState,
    log_hub: LogHub,
    sm,
    cfg: AppConfigIn,
) -> Tuple[bool, Optional[AppConfigIn], str, str]:
    """RPC Login 并合并 Key/UserID；失败返回 ok=False。"""
    v_login = compute_js_timespan_v()
    login_res = await rpc_login(sm, cfg.username, cfg.password, v=v_login)
    if not login_res.ok:
        state.logged_in = False
        await log_hub.push(LogLevel.error, f"Login 失败 HTTP {login_res.status_code}")
        state.last_runner_error = (login_res.message or "")[:500]
        return False, cfg, "", ""

    merged, _ = merge_from_rpc_login(cfg, login_res.response_body)
    state.config = merged
    state.logged_in = True
    try:
        await persist_trading_config_standalone(user_id, merged)
    except Exception as ex:
        await log_hub.push(LogLevel.warn, f"交易配置写入数据库失败: {ex}")
    rk = merged.rpc_login_key.strip()
    uid = merged.rpc_user_id.strip()
    if not rk or not uid:
        state.logged_in = False
        await log_hub.push(LogLevel.error, "Login 未解析出 Key/UserID")
        return False, merged, rk, uid
    _clear_sell_mnemonic_cache(state)
    return True, merged, rk, uid


async def _full_login_subaccounts_mnemonic_sync(
    user_id: int,
    state: AppState,
    log_hub: LogHub,
    sm,
    cfg: AppConfigIn,
) -> Tuple[bool, List[dict]]:
    """
    强制链路：RPC Login → 全量 My_Subaccount → Mnemonic_Get01 写入助记词缓存。
    用于订阅有效用户任务启动/恢复后、进入售卖前必须刷新内存态（与仅读旧缓存区分）。
    """
    ok_lm, merged, rk, uid = await _rpc_login_merge_config(user_id, state, log_hub, sm, cfg)
    if not ok_lm or merged is None:
        return False, []
    cfg = merged
    v_sub = compute_js_timespan_v()
    sub_out = await fetch_all_subaccounts(
        sm,
        key=rk,
        user_id=uid,
        v=v_sub,
        page_size=settings.subaccount_page_size,
        max_pages=settings.subaccount_max_pages,
        log_push=None,
        silent=True,
    )
    if sub_out.not_logged_in or (not sub_out.first_page_ok and sub_out.first_page_status_code == 401):
        state.logged_in = False
        await log_hub.push(LogLevel.warn, "强制同步：子账号接口未登錄或 HTTP 401")
        return False, []
    if not sub_out.first_page_ok:
        await log_hub.push(
            LogLevel.warn,
            f"强制同步：子账号首页失败 HTTP {sub_out.first_page_status_code}",
        )
        return False, []
    items = list(sub_out.items)
    state.subaccounts_cache = items
    if not await _refresh_sell_mnemonic_cache(state, sm, log_hub, cfg):
        await log_hub.push(LogLevel.error, "强制同步：Mnemonic_Get01 失败")
        return False, []
    await log_hub.push(
        LogLevel.success,
        f"强制同步完成：已登录、子账号 {len(items)} 条、助记词已写入缓存",
    )
    return True, items


async def _fetch_subaccounts_resume_retries(
    user_id: int,
    state: AppState,
    log_hub: LogHub,
    sm,
    cfg: AppConfigIn,
) -> List[dict]:
    """
    停止后再开始等场景：在允许拉取守卫下重试全量 My_Subaccount。
    失败重试 max_attempts 次，间隔 delay_ms（与 settings 一致）。
    """
    max_a = max(1, int(settings.sell_resume_sub_fetch_max_attempts or 6))
    delay_ms = max(50, int(settings.sell_resume_sub_fetch_delay_ms or 500))
    rk = cfg.rpc_login_key.strip()
    uid = cfg.rpc_user_id.strip()
    for attempt in range(1, max_a + 1):
        if state.stop_event.is_set():
            return []
        v_sub = compute_js_timespan_v()
        sub_out = await fetch_all_subaccounts(
            sm,
            key=rk,
            user_id=uid,
            v=v_sub,
            page_size=settings.subaccount_page_size,
            max_pages=settings.subaccount_max_pages,
            log_push=None,
            silent=True,
        )
        if sub_out.not_logged_in or (not sub_out.first_page_ok and sub_out.first_page_status_code == 401):
            state.logged_in = False
            await log_hub.push(
                LogLevel.warn,
                f"补拉子账号未登錄或 HTTP 401（{attempt}/{max_a}）",
            )
        elif sub_out.first_page_ok:
            state.subaccounts_cache = list(sub_out.items)
            await log_hub.push(
                LogLevel.success,
                f"补拉子账号成功，共 {len(sub_out.items)} 条（第 {attempt} 次）",
            )
            if not await _refresh_sell_mnemonic_cache(state, sm, log_hub, cfg):
                await log_hub.push(LogLevel.error, "补拉子账号后 Mnemonic_Get01 失败，返回空列表")
                return []
            return list(sub_out.items)
        else:
            await log_hub.push(
                LogLevel.warn,
                f"补拉子账号失败 HTTP {sub_out.first_page_status_code}（{attempt}/{max_a}）",
            )
        if attempt < max_a:
            await _sleep_between_sell_requests(state, delay_ms)
    await log_hub.push(LogLevel.error, f"补拉子账号已达 {max_a} 次仍失败，结束本轮售卖")
    return []


async def _resolve_items_cache_or_resume_fetch(
    user_id: int,
    state: AppState,
    log_hub: LogHub,
    sm,
    cfg: AppConfigIn,
) -> List[dict]:
    cached = list(state.subaccounts_cache)
    if cached:
        return cached
    await log_hub.push(LogLevel.info, "子账号内存缓存为空，按配置重试拉取 My_Subaccount")
    return await _fetch_subaccounts_resume_retries(user_id, state, log_hub, sm, cfg)


def _runner_main_account_row(cfg: AppConfigIn) -> dict:
    info = parse_main_account_info_json(getattr(cfg, "main_account_info_json", "") or "{}")
    main_id = (cfg.rpc_user_id or "").strip()
    ace = info.get("ACECount")
    ace_str = str(ace).strip() if ace is not None and str(ace).strip() else "0"
    row = {
        "SonId": "",
        "FlowNumber": main_id,
        "MemberNo": "主账户",
        "AceAmount": ace_str,
        "__is_main_account": True,
    }
    ctime = info.get("CreateTime")
    if ctime is not None and str(ctime).strip():
        row["CreateTime"] = str(ctime).strip()
    return row


def _ensure_runner_main_account(items: List[dict], cfg: AppConfigIn) -> List[dict]:
    # 自动售卖中：主账户强制参与，且固定置顶，避免依赖上游是否返回主账户行。
    rest = [dict(r) for r in items if not bool(dict(r).get("__is_main_account"))]
    return [_runner_main_account_row(cfg), *rest]


def _runner_effective_count(cfg: AppConfigIn, row: dict, *, items: List[dict], today: str) -> str:
    only_main = no_pending_subaccounts_for_sell(items, cfg, today)
    return runner_listing_amount_for_row(cfg, row, only_main_remaining=only_main)


async def _run_hot_maybe_recover_relogin(
    user_id: int,
    state: AppState,
    log_hub: LogHub,
    sm,
    items: List[dict],
    *,
    sell_start_beijing: Optional[datetime],
    lease_holder: Optional[str],
) -> Tuple[bool, bool, bool]:
    """
    先直接 HotWindow；若返回需重新登录，则 Login → 读缓存 → 空则补拉（重试）
    → 再跑一轮 HotWindow。返回 (channel_closed, relogin_still_needed, skip_day_outbound)。
    """
    cfg = state.config
    if cfg is None:
        return False, True, False

    items = sort_subaccounts_for_sell(_ensure_runner_main_account(list(items), cfg), cfg)

    if not await _ensure_sell_mnemonic_cached(state, sm, log_hub, cfg):
        await log_hub.push(LogLevel.error, "助记词缓存未就绪，无法进入 HotWindow")
        state.last_runner_error = "助记词缓存失败"
        return False, True, False

    set_sub_fetch_allowed(False)
    try:
        state.hot_sell_window_active = True
        try:
            closed, relogin, skip_day = await _hot_window_sell_session(
                user_id,
                state,
                cfg,
                log_hub,
                sm,
                items,
                sell_start_beijing=sell_start_beijing,
                lease_holder=lease_holder,
            )
        finally:
            state.hot_sell_window_active = False
    finally:
        set_sub_fetch_allowed(True)

    if skip_day:
        return False, False, True
    if closed or not relogin:
        return closed, relogin, False

    ok, cfg2, _, _ = await _rpc_login_merge_config(user_id, state, log_hub, sm, state.config)
    if not ok or cfg2 is None:
        return False, True, False

    set_sub_fetch_allowed(True)
    try:
        items2 = await _resolve_items_cache_or_resume_fetch(user_id, state, log_hub, sm, cfg2)
    finally:
        set_sub_fetch_allowed(True)

    if not items2:
        state.last_runner_error = "Login 后子账号仍为空且补拉失败"
        return False, True, False

    cfg = state.config
    if cfg is None:
        return False, True, False

    items2 = sort_subaccounts_for_sell(_ensure_runner_main_account(items2, cfg), cfg)

    if not await _ensure_sell_mnemonic_cached(state, sm, log_hub, cfg):
        state.last_runner_error = "Login 后助记词缓存失败"
        return False, True, False

    set_sub_fetch_allowed(False)
    try:
        state.hot_sell_window_active = True
        try:
            closed2, relogin2, skip_day2 = await _hot_window_sell_session(
                user_id,
                state,
                cfg,
                log_hub,
                sm,
                items2,
                sell_start_beijing=sell_start_beijing,
                lease_holder=lease_holder,
            )
        finally:
            state.hot_sell_window_active = False
    finally:
        set_sub_fetch_allowed(True)
    return closed2, relogin2, skip_day2


async def _timed_prep_phase(
    user_id: int,
    state: AppState,
    log_hub: LogHub,
    sm,
    start_dt: datetime,
) -> Tuple[bool, List[dict], Optional[AppConfigIn], str, str]:
    """
    Prep：Login → Key/UserID → My_Subaccount 全量；须在 T_open 前完成；带次数上限。
    """
    max_attempts = max(1, int(settings.sell_prep_max_attempts or 8))
    retry_delay = max(0.5, float(settings.sell_prep_retry_delay_seconds or 2.0))
    rk, uid = "", ""
    cfg: Optional[AppConfigIn] = state.config
    if cfg is None:
        return False, [], None, rk, uid

    for attempt in range(1, max_attempts + 1):
        if state.stop_event.is_set():
            return False, [], cfg, rk, uid
        if beijing_now() >= start_dt:
            await log_hub.push(
                LogLevel.error,
                f"开售前准备失败：已到达或超过开售时刻 T_open（尝试 {attempt}/{max_attempts}），中止准备",
            )
            return False, [], cfg, rk, uid

        cfg = await _reload_runner_config_from_db(user_id, state)
        if cfg is None:
            await log_hub.push(
                LogLevel.error,
                "准备阶段：无法从数据库加载交易配置（解密失败或当前账户槽无配置）",
            )
            return False, [], None, rk, uid

        v_login = compute_js_timespan_v()
        login_res = await rpc_login(sm, cfg.username, cfg.password, v=v_login)
        if not login_res.ok:
            state.logged_in = False
            await log_hub.push(
                LogLevel.warn,
                f"准备阶段 Login 失败 HTTP {login_res.status_code}（{attempt}/{max_attempts}）",
            )
            await _wait_interruptible(state, retry_delay)
            continue

        merged, _ = merge_from_rpc_login(cfg, login_res.response_body)
        state.config = merged
        cfg = merged
        state.logged_in = True
        _clear_sell_mnemonic_cache(state)
        rk = cfg.rpc_login_key.strip()
        uid = cfg.rpc_user_id.strip()
        if not rk or not uid:
            await log_hub.push(LogLevel.error, "准备阶段 Login 未解析出 Key/UserID")
            state.logged_in = False
            await _wait_interruptible(state, retry_delay)
            continue

        try:
            await persist_trading_config_standalone(user_id, merged)
        except Exception as ex:
            await log_hub.push(LogLevel.warn, f"交易配置写入数据库失败: {ex}")

        v_sub = compute_js_timespan_v()
        sub_out = await fetch_all_subaccounts(
            sm,
            key=rk,
            user_id=uid,
            v=v_sub,
            page_size=settings.subaccount_page_size,
            max_pages=settings.subaccount_max_pages,
            log_push=None,
            silent=True,
        )
        if sub_out.not_logged_in or (not sub_out.first_page_ok and sub_out.first_page_status_code == 401):
            state.logged_in = False
            await log_hub.push(
                LogLevel.warn,
                f"准备阶段子账号接口未登錄或 401（{attempt}/{max_attempts}）",
            )
            await _wait_interruptible(state, retry_delay)
            continue

        if not sub_out.first_page_ok:
            await log_hub.push(
                LogLevel.warn,
                f"准备阶段子账号首页失败 HTTP {sub_out.first_page_status_code}（{attempt}/{max_attempts}）",
            )
            await _wait_interruptible(state, retry_delay)
            continue

        items = sub_out.items
        state.subaccounts_cache = list(items)
        if not await _refresh_sell_mnemonic_cache(state, sm, log_hub, cfg):
            await log_hub.push(
                LogLevel.error,
                "准备阶段：子账号已拉取但助记词缓存失败（见上一条，未必是接口 HTTP 失败）",
            )
            return False, [], cfg, rk, uid
        return True, items, cfg, rk, uid

    await log_hub.push(LogLevel.error, f"开售前准备失败：已达最大尝试次数 {max_attempts} 仍未成功")
    return False, [], state.config, rk, uid


async def _warmup_all_outbound_proxies(
    sm,
    log_hub: LogHub,
    rpc_key: str,
    user_id: str,
    *,
    log_prefix: str = "开售预热",
) -> None:
    """
    对每条出站代理各发一次 Mnemonic_Get01，完成 TCP/TLS 握手并在 httpx 连接池中保持 keep-alive。
    单代理/直连时只请求一次；多代理时并行预热全部出口。
    """
    v = compute_js_timespan_v()
    n = sm.outbound_proxy_count()

    if not sm.uses_multi_proxy_dispatch() or n <= 1:
        ok, code, _, _ = await post_mnemonic_get01(
            sm, rpc_key=rpc_key, user_id=user_id, v=v, lang="cn"
        )
        if not ok:
            await log_hub.push(
                LogLevel.warn,
                f"{log_prefix}：Mnemonic_Get01 HTTP {code}（将继续重试）",
            )
        elif n == 1:
            await log_hub.push(LogLevel.info, f"{log_prefix}：1 条出口 TCP 已预热")
        return

    async def _one(idx: int) -> Tuple[int, bool, int]:
        ok, code, _, _ = await post_mnemonic_get01(
            sm,
            rpc_key=rpc_key,
            user_id=user_id,
            v=v,
            lang="cn",
            proxy_pin_index=idx,
        )
        return idx, ok, code

    results = await asyncio.gather(
        *[_one(i) for i in range(n)],
        return_exceptions=True,
    )
    ok_n = 0
    for item in results:
        if isinstance(item, BaseException):
            await log_hub.push(
                LogLevel.warn,
                f"{log_prefix}：代理预热异常 {item}",
            )
            continue
        idx, ok, code = item
        if ok:
            ok_n += 1
        else:
            await log_hub.push(
                LogLevel.warn,
                f"{log_prefix}：代理{idx + 1} Mnemonic_Get01 HTTP {code}",
            )
    if ok_n == n:
        await log_hub.push(LogLevel.info, f"{log_prefix}：{n} 条出口 TCP 已预热")
    elif ok_n > 0:
        await log_hub.push(
            LogLevel.warn,
            f"{log_prefix}：{ok_n}/{n} 条出口预热成功",
        )


async def _sell_open_warmup_loop(
    state: AppState,
    sm,
    log_hub: LogHub,
    start_dt: datetime,
    *,
    should_resync=None,
) -> bool:
    """
    与 WaitOpen 并行：在开售整点前若干秒内周期性对全部代理请求 Mnemonic_Get01 预热 TLS/TCP 连接。
    选用 Mnemonic_Get01 而非 My_Subaccount：同域名同端口，返回极小 JSON，无副作用。
    """
    before = max(0, int(settings.sell_warmup_seconds_before_open or 0))
    if before <= 0:
        return True
    ping_sec = max(2.0, float(settings.sell_warmup_ping_interval_seconds or 6.0))
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=BJ)
    first_at = start_dt - timedelta(seconds=before)
    while not state.stop_event.is_set():
        if should_resync is not None and should_resync():
            return False
        now = beijing_now()
        if now >= start_dt:
            return True
        if now < first_at:
            if not await wait_interruptible_until_beijing(
                state.stop_event, first_at, should_resync=should_resync
            ):
                return False
            continue
        cfg = state.config
        if cfg is None:
            return False
        rk = cfg.rpc_login_key.strip()
        uid = cfg.rpc_user_id.strip()
        if not rk or not uid:
            return False
        await _warmup_all_outbound_proxies(sm, log_hub, rk, uid)
        next_deadline = beijing_now() + timedelta(seconds=ping_sec)
        if next_deadline > start_dt:
            next_deadline = start_dt
        if not await wait_interruptible_until_beijing(
            state.stop_event, next_deadline, should_resync=should_resync
        ):
            return False
    return False


async def _sell_start_countdown_logs(
    state: AppState,
    start_dt: datetime,
    log_hub: LogHub,
    *,
    should_resync=None,
) -> bool:
    """开售整点前 60 秒起：每 10 秒一条，最后 10 秒每秒一条。"""
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=BJ)
    milestones = [60, 50, 40, 30, 20, 10] + list(range(9, 0, -1))
    for d in milestones:
        if state.stop_event.is_set():
            return False
        if should_resync is not None and should_resync():
            return False
        target = start_dt - timedelta(seconds=d)
        now = beijing_now()
        if now >= start_dt:
            return True
        if now >= target:
            continue
        if not await wait_interruptible_until_beijing(
            state.stop_event, target, should_resync=should_resync
        ):
            return False
        if state.stop_event.is_set():
            return False
        if should_resync is not None and should_resync():
            return False
        if beijing_now() >= start_dt:
            return True
        await log_hub.push(LogLevel.info, f"距离开售还有约 {d} 秒")
    return True


def _sell_attempt_is_timeout(ok_s: bool, code_s: int, raw_out: str) -> bool:
    if ok_s:
        return False
    if int(code_s or 0) == 0:
        return True
    blob = (raw_out or "").lower()
    return "timeout" in blob or "timed out" in blob


def _hot_window_pending_rows(items: List[dict], cfg: AppConfigIn, today: str) -> List[dict]:
    sold_ids = sold_son_ids_for_today(cfg.sold_son_ids_json, today)
    out: List[dict] = []
    for row in items:
        is_main = bool(row.get("__is_main_account"))
        rpc_son = ace_sell_rpc_son_id(row)
        if not is_main:
            ok_el, _ = subaccount_eligible_for_ace_sell(row, cfg)
            if not ok_el:
                continue
        if not is_main and not rpc_son:
            continue
        tid = ace_sell_track_id(row)
        if not tid or tid in sold_ids:
            continue
        if not _runner_effective_count(cfg, row, items=items, today=today):
            continue
        out.append(row)
    return out


def _pick_next_hot_row(
    items: List[dict],
    cfg: AppConfigIn,
    today: str,
    *,
    busy_tracks: set,
    skip_track: Optional[str] = None,
) -> Optional[dict]:
    sold_ids = sold_son_ids_for_today(cfg.sold_son_ids_json, today)
    for row in items:
        is_main = bool(row.get("__is_main_account"))
        rpc_son = ace_sell_rpc_son_id(row)
        if not is_main:
            ok_el, _ = subaccount_eligible_for_ace_sell(row, cfg)
            if not ok_el:
                continue
        if not is_main and not rpc_son:
            continue
        tid = ace_sell_track_id(row)
        if not tid or tid in sold_ids or tid in busy_tracks:
            continue
        if skip_track and tid == skip_track:
            continue
        if not _runner_effective_count(cfg, row, items=items, today=today):
            continue
        return row
    return None


async def _hot_slot_sell_loop(
    user_id: int,
    state: AppState,
    log_hub: LogHub,
    sm,
    items: List[dict],
    *,
    proxy_index: int,
    slot_label: str,
    sell_start_beijing: Optional[datetime],
    lease_holder: Optional[str],
    busy_tracks: set,
    busy_lock: asyncio.Lock,
    cfg_lock: asyncio.Lock,
    skip_day_ev: asyncio.Event,
    true_channel_closed_ev: asyncio.Event,
) -> None:
    """
    单槽售卖：await 每次 ACE 结果后重试或换号。
    出口固定为 proxy_index（多代理时）；无多代理时不 pin。
    """
    if skip_day_ev.is_set() or state.stop_event.is_set():
        return

    proxy_count = sm.outbound_proxy_count()
    pin_index: Optional[int] = None
    if proxy_count > 1:
        pin_index = proxy_index % proxy_count
    elif proxy_count == 1:
        pin_index = 0

    prep_max = max(1, int(settings.sell_prep_max_attempts or 8))
    grace_ms = max(0, int(settings.sell_channel_closed_grace_retry_ms or 0))
    trust_after = max(0, int(settings.sell_channel_closed_trust_after_seconds or 0))
    grace_deadline = (
        sell_start_beijing + timedelta(seconds=trust_after)
        if sell_start_beijing is not None
        else None
    )

    current_row: Optional[dict] = None
    current_track: Optional[str] = None
    prep_fail_streak = 0
    not_login_streak = 0
    logged_grace_channel = False

    async def release_track() -> None:
        nonlocal current_track
        if not current_track:
            return
        async with busy_lock:
            busy_tracks.discard(current_track)
        current_track = None

    async def acquire_row(row: dict) -> None:
        nonlocal current_row, current_track
        await release_track()
        current_row = row
        current_track = ace_sell_track_id(row)
        if current_track:
            async with busy_lock:
                busy_tracks.add(current_track)

    while not state.stop_event.is_set() and not skip_day_ev.is_set():
        if true_channel_closed_ev.is_set():
            await release_track()
            return

        cfg = state.config
        if cfg is None:
            break
        today = beijing_today_str()

        if current_row is None:
            row = _pick_next_hot_row(
                items,
                cfg,
                today,
                busy_tracks=busy_tracks,
            )
            if row is None:
                if not _hot_window_pending_rows(items, cfg, today):
                    return
                await _sleep_between_sell_requests(state, max(grace_ms, 200))
                continue
            await acquire_row(row)
            prep_fail_streak = 0
            not_login_streak = 0
            logged_grace_channel = False

        assert current_row is not None and current_track
        row = current_row
        track_id = current_track
        rpc_son_id = ace_sell_rpc_son_id(row)
        cnt = _runner_effective_count(cfg, row, items=items, today=today)
        if not cnt:
            await release_track()
            current_row = None
            continue

        m_a, m_b, m_c = state.sell_mnemonic_id1, state.sell_mnemonic_key, state.sell_mnemonic_str1
        if not m_a or not m_b or not m_c:
            await _sleep_between_sell_requests(state, max(grace_ms, 200))
            continue

        cfg_row = state.config
        if cfg_row is None:
            break
        g, g_err = totp_now_from_secret_ex(cfg_row.key_token)
        if not g:
            await log_hub.push(
                LogLevel.error,
                f"[{slot_label}] 无法生成 TOTP（track={track_id}）：{g_err or '请检查 key_token'}",
            )
            await release_track()
            current_row = None
            continue

        rk = cfg_row.rpc_login_key.strip()
        uid = cfg_row.rpc_user_id.strip()
        if not rk or not uid:
            await release_track()
            current_row = None
            continue

        v_ace = compute_js_timespan_v()
        ok_s, code_s, parsed, raw_out = await post_ace_sell_son(
            sm,
            amount=cnt,
            password=cfg_row.password,
            son_id=rpc_son_id,
            mnemonic_id1=m_a,
            mnemonic_key=m_b,
            mnemonic_str1=m_c,
            g_code=g,
            count=cnt,
            rpc_key=rk,
            user_id=uid,
            v=v_ace,
            proxy_pin_index=pin_index,
        )

        if lease_holder:
            await renew_runner_lease_if_holder(user_id, lease_holder)

        sub_name = (
            "主账户"
            if not rpc_son_id
            else (resolve_subaccount_display_name(row) or track_id)
        )

        if response_indicates_channel_closed(parsed, raw_out):
            in_trust = grace_deadline is not None and beijing_now() < grace_deadline
            if in_trust:
                if not logged_grace_channel:
                    logged_grace_channel = True
                    await log_hub.push(
                        LogLevel.warn,
                        f"[{slot_label}] {sub_name}，售卖失败，通道尚未开放！",
                    )
                if grace_ms > 0:
                    await _sleep_between_sell_requests(state, grace_ms)
                continue
            if not true_channel_closed_ev.is_set():
                true_channel_closed_ev.set()
                await log_hub.push(
                    LogLevel.warn,
                    "本日交易通道已關閉（任一槽位确认），全部售卖槽停止",
                )
            await log_hub.push(
                LogLevel.warn,
                f"[{slot_label}] {sub_name}，本日交易通道已關閉，本槽停止",
            )
            await release_track()
            return

        if json_indicates_rpc_not_logged_in(parsed):
            state.logged_in = False
            not_login_streak += 1
            await log_hub.push(
                LogLevel.warn,
                f"[{slot_label}] {sub_name}，未登录（{not_login_streak}/{prep_max}）",
            )
            if not_login_streak >= prep_max:
                skip_day_ev.set()
                state.last_runner_error = "ACE_Sell_Son 未登錄"
                await log_hub.push(
                    LogLevel.error,
                    f"[{slot_label}] 未登录已达 {prep_max} 次，本日跳过对外售卖",
                )
                await release_track()
                return
            if grace_ms > 0:
                await _sleep_between_sell_requests(state, grace_ms)
            continue

        not_login_streak = 0
        json_err = isinstance(parsed, dict) and parsed.get("Error") is True
        is_429 = int(code_s or 0) == 429
        is_timeout = _sell_attempt_is_timeout(ok_s, code_s, raw_out)

        if ok_s and not json_err and not is_429:
            if not rpc_son_id:
                ok_msg = f"恭喜主账户，售卖成功！售卖数量：{cnt}"
            else:
                ok_msg = f"恭喜子账户：{sub_name}，售卖成功！售卖数量：{cnt}"
            await log_hub.push(LogLevel.success, f"[{slot_label}] {ok_msg}")
            async with cfg_lock:
                cfg_cur = state.config
                if cfg_cur is not None:
                    new_json = add_sold_son_json(cfg_cur.sold_son_ids_json, today, track_id)
                    new_cfg = cfg_cur.model_copy(update={"sold_son_ids_json": new_json})
                    state.config = new_cfg
                    try:
                        await persist_trading_config_standalone(user_id, new_cfg)
                    except Exception as ex:
                        await log_hub.push(
                            LogLevel.warn,
                            f"已售子账号写入数据库失败: {ex}",
                        )
            await release_track()
            current_row = None
            prep_fail_streak = 0
            logged_grace_channel = False
            continue

        if is_429:
            await log_hub.push(LogLevel.warn, f"[{slot_label}] {sub_name}，售卖失败，限流！")
            if grace_ms > 0:
                await _sleep_between_sell_requests(state, grace_ms)
            continue

        if json_err:
            await log_hub.push(LogLevel.error, f"[{slot_label}] {sub_name}，售卖失败，参数不正确！")
        elif is_timeout:
            await log_hub.push(
                LogLevel.error,
                f"[{slot_label}] {sub_name}，售卖失败，请求超时！",
            )
        elif int(code_s or 0) == 0:
            await log_hub.push(
                LogLevel.error,
                f"[{slot_label}] {sub_name}，售卖失败，服务器没有响应！",
            )
        else:
            await log_hub.push(LogLevel.error, f"[{slot_label}] {sub_name}，售卖失败，服务器忙！")

        prep_fail_streak += 1
        if prep_fail_streak >= prep_max:
            await release_track()
            cfg_now = state.config
            if cfg_now is None:
                return
            nxt = _pick_next_hot_row(
                items,
                cfg_now,
                today,
                busy_tracks=busy_tracks,
                skip_track=track_id,
            )
            if nxt is not None:
                await acquire_row(nxt)
                prep_fail_streak = 0
                logged_grace_channel = False
            else:
                await acquire_row(row)
                prep_fail_streak = 0
            if grace_ms > 0:
                await _sleep_between_sell_requests(state, grace_ms)
            continue

        if grace_ms > 0:
            await _sleep_between_sell_requests(state, grace_ms)


async def _hot_window_sell_session(
    user_id: int,
    state: AppState,
    cfg: AppConfigIn,
    log_hub: LogHub,
    sm,
    items: List[dict],
    *,
    sell_start_beijing: Optional[datetime] = None,
    lease_holder: Optional[str] = None,
) -> Tuple[bool, bool, bool]:
    """
    HotWindow：代理数 × HOT_WINDOW_CONCURRENCY 个售卖槽；每槽 await 结果后重试/换号。
    总槽位 = 实际绑定代理数 × HOT_WINDOW_CONCURRENCY（无代理时按 1 条出口计）。
    返回 (channel_closed, relogin_recommended, skip_day_outbound)。
    """
    today = beijing_today_str()
    slots_per_proxy = max(1, int(settings.hot_window_concurrency or 1))
    proxy_count = max(1, sm.outbound_proxy_count())
    total_slots = proxy_count * slots_per_proxy

    pending = _hot_window_pending_rows(items, cfg, today)
    if not pending:
        await log_hub.push(LogLevel.info, "当日待售账号已处理完毕：子账号与主账户均无可发起项")
        return False, False, False

    rk0 = cfg.rpc_login_key.strip()
    uid0 = cfg.rpc_user_id.strip()
    if not rk0 or not uid0:
        await log_hub.push(LogLevel.error, "配置缺少 Key/UserID，中止 HotWindow")
        return False, False, False

    if not (
        state.sell_mnemonic_id1
        and state.sell_mnemonic_key
        and state.sell_mnemonic_str1
    ):
        await log_hub.push(LogLevel.info, "售卖助记词缓存不完整，尝试 Mnemonic_Get01 一次并回写缓存")
        if not await _refresh_sell_mnemonic_cache(state, sm, log_hub, cfg):
            await log_hub.push(
                LogLevel.error,
                "售卖助记词缓存仍不可用（请确认已登录且配置助记词正确），中止 HotWindow",
            )
            state.last_runner_error = "助记词缓存为空"
            return False, True, False

    # 在「等到开售+grace」之前预热，避免 grace 结束后再打一轮 Mnemonic 把首单推迟数百毫秒
    if rk0 and uid0 and sm.outbound_proxy_count() > 0:
        await _warmup_all_outbound_proxies(
            sm, log_hub, rk0, uid0, log_prefix="HotWindow 开售前"
        )

    if sell_start_beijing is not None:
        if not await _wait_until_scheduled_first_sell_batch(
            state, log_hub, sell_start_beijing
        ):
            return False, False, False

    await log_hub.push(
        LogLevel.info,
        (
            f"HotWindow 槽位网格：{proxy_count} 条出口 × {slots_per_proxy} 槽 = {total_slots} 路并发；"
            f"待售 {len(pending)} 个"
        ),
    )

    busy_tracks: set = set()
    busy_lock = asyncio.Lock()
    cfg_lock = asyncio.Lock()
    skip_day_ev = asyncio.Event()
    true_channel_closed_ev = asyncio.Event()

    tasks: List[asyncio.Task] = []
    for proxy_i in range(proxy_count):
        for slot_i in range(slots_per_proxy):
            label = f"代理{proxy_i + 1}-槽{slot_i + 1}"
            tasks.append(
                asyncio.create_task(
                    _hot_slot_sell_loop(
                        user_id,
                        state,
                        log_hub,
                        sm,
                        items,
                        proxy_index=proxy_i,
                        slot_label=label,
                        sell_start_beijing=sell_start_beijing,
                        lease_holder=lease_holder,
                        busy_tracks=busy_tracks,
                        busy_lock=busy_lock,
                        cfg_lock=cfg_lock,
                        skip_day_ev=skip_day_ev,
                        true_channel_closed_ev=true_channel_closed_ev,
                    )
                )
            )

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    skip_day = skip_day_ev.is_set()
    channel_closed = true_channel_closed_ev.is_set()

    if skip_day:
        return False, False, True
    if channel_closed:
        return True, False, False
    return False, False, False


async def run_background(user_id: int, config: AppConfigIn) -> None:
    from app.proxy_binding import get_session_manager_for_user_id
    from app.user_registry import get_or_create_log_hub, get_or_create_state

    state = await get_or_create_state(user_id)
    log_hub: LogHub = await get_or_create_log_hub(user_id)
    lease_holder = get_runner_lease_holder_id()

    state.last_runner_error = None
    state.logged_in = False

    try:
        interval = float(settings.runner_loop_interval_seconds)

        while not state.stop_event.is_set():
            # 每轮从 DB 同步代理绑定：启动时可能尚无绑定，或管理端后续绑定/调用了 invalidate；
            # 若只在 run_background 入口取一次 sm，会永久直连（与 http_requests.log 中 proxy_label=direct 一致）。
            sm = await get_session_manager_for_user_id(user_id)
            cfg: Optional[AppConfigIn] = state.config
            if cfg is None:
                await log_hub.push(LogLevel.error, "配置丢失，停止任务")
                state.last_runner_error = "配置丢失"
                break

            prep_start: Optional[Tuple[datetime, datetime]] = None
            from app.db import AsyncSessionLocal
            from app.platform_settings_repo import get_platform_sell_start_time, platform_sell_time_revision
            from app.runner_lifecycle import apply_timed_sell_late_start_skip_flag

            async with AsyncSessionLocal() as _sell_db:
                sell_hhmm = await get_platform_sell_start_time(_sell_db)
            sell_rev = platform_sell_time_revision()
            apply_timed_sell_late_start_skip_flag(state, sell_hhmm)
            if (sell_hhmm or "").strip():
                prep_start = today_prep_and_start(sell_hhmm)
                if not prep_start:
                    await log_hub.push(LogLevel.warn, "全站开售时间无法解析，跳过定时等待")

            if prep_start:
                prep_dt, start_dt = prep_start
                today_bj = beijing_today_str()
                late_raw = (state.runner_late_start_skip_outbound_today or "").strip()
                if late_raw and late_raw != today_bj:
                    state.runner_late_start_skip_outbound_today = ""
                    late_raw = ""
                if late_raw == today_bj:
                    state.last_runner_error = "已超过开售缓冲时间，本日不执行对外售卖链路"
                    sec = seconds_until_next_beijing_midnight()
                    await log_hub.push(
                        LogLevel.info,
                        f"内部等待：约 {sec / 3600:.1f} 小时至北京时间次日 0 点",
                    )
                    if await _wait_interruptible_until_sell_resync(state, sec, sell_rev):
                        await log_hub.push(LogLevel.info, "全站开售时间已更新，按新时刻重新调度")
                        continue
                    continue

                sell_started = beijing_now() >= start_dt
                if (
                    state.runner_sub_prep_date
                    and state.runner_sub_prep_date != today_bj
                    and state.subaccounts_cache
                ):
                    await log_hub.push(
                        LogLevel.warn,
                        "子账号缓存对应非今日北京日期，已清空以免误卖",
                    )
                    state.subaccounts_cache = []
                    state.runner_sub_prep_date = ""
                    _clear_sell_mnemonic_cache(state)
                    state.runner_must_refresh_trading_cache = True

                s1 = seconds_until_beijing(prep_dt)
                if s1 > 0:
                    if await _wait_interruptible_until_sell_resync(state, s1, sell_rev):
                        await log_hub.push(LogLevel.info, "全站开售时间已更新，按新时刻重新调度")
                        continue
                if state.stop_event.is_set():
                    break

                # 等待结束后重新拉取代理绑定：等待期间自动购机可能完成了分配
                sm = await get_session_manager_for_user_id(user_id)

                skip_prep_fetch = sell_started and bool(state.subaccounts_cache) and (
                    state.runner_sub_prep_date == today_bj
                    or not (state.runner_sub_prep_date or "").strip()
                )
                if skip_prep_fetch and not (state.runner_sub_prep_date or "").strip():
                    state.runner_sub_prep_date = today_bj

                items: List[dict] = []
                cfg = state.config
                if cfg is None:
                    break

                if skip_prep_fetch:
                    items = list(state.subaccounts_cache)
                    await log_hub.push(
                        LogLevel.info,
                        f"定时开售：优先直接售卖（响应未登录再 Login）；当前子账号缓存 {len(items)} 条",
                    )
                    if not items:
                        ok_lm, cfg, _, _ = await _rpc_login_merge_config(
                            user_id, state, log_hub, sm, cfg
                        )
                        if not ok_lm or cfg is None:
                            await _wait_interruptible(state, interval)
                            continue
                        set_sub_fetch_allowed(True)
                        try:
                            items = await _fetch_subaccounts_resume_retries(
                                user_id, state, log_hub, sm, cfg
                            )
                        finally:
                            set_sub_fetch_allowed(True)
                        if not items:
                            state.last_runner_error = "无子账号缓存且补拉失败"
                            await _wait_interruptible(state, interval)
                            continue
                        state.runner_must_refresh_trading_cache = False
                else:
                    if sell_started and not state.subaccounts_cache:
                        await log_hub.push(
                            LogLevel.warn,
                            "已开售无子账号内存缓存：先 Login 再按配置重试拉取 My_Subaccount",
                        )
                        ok_lm, cfg, _, _ = await _rpc_login_merge_config(
                            user_id, state, log_hub, sm, cfg
                        )
                        if not ok_lm or cfg is None:
                            await _wait_interruptible(state, interval)
                            continue
                        items = await _fetch_subaccounts_resume_retries(
                            user_id, state, log_hub, sm, cfg
                        )
                        if not items:
                            state.last_runner_error = "补拉子账号失败"
                            await _wait_interruptible(state, interval)
                            continue
                        state.runner_sub_prep_date = today_bj
                        state.runner_must_refresh_trading_cache = False
                    elif not sell_started:
                        prep_ok, items, cfg, rk, uid = await _timed_prep_phase(
                            user_id, state, log_hub, sm, start_dt
                        )
                        if not prep_ok or cfg is None:
                            await _wait_interruptible(state, interval)
                            continue
                        state.runner_sub_prep_date = today_bj
                        state.runner_must_refresh_trading_cache = False

                        if beijing_now() < start_dt:
                            should_resync = lambda: platform_sell_time_revision() != sell_rev
                            open_ok, _, _ = await asyncio.gather(
                                wait_open_phases_beijing(
                                    state.stop_event,
                                    start_dt,
                                    settings.sell_wait_open_wake_early_ms,
                                    should_resync=should_resync,
                                ),
                                _sell_open_warmup_loop(
                                    state, sm, log_hub, start_dt, should_resync=should_resync
                                ),
                                _sell_start_countdown_logs(
                                    state, start_dt, log_hub, should_resync=should_resync
                                ),
                            )
                            if should_resync() or not open_ok:
                                await log_hub.push(LogLevel.info, "全站开售时间已更新，按新时刻重新调度")
                                continue
                        if state.stop_event.is_set():
                            break
                    else:
                        items = list(state.subaccounts_cache)
                        cfg = state.config
                        if cfg is None:
                            break
                        await log_hub.push(
                            LogLevel.info,
                            "已开售且有子账号缓存（未命中跳过准备条件），直接进入售卖",
                        )

                if not items:
                    cfg_empty = state.config
                    if cfg_empty is None:
                        break
                    main_probe = _runner_main_account_row(cfg_empty)
                    if not runner_listing_amount_for_row(
                        cfg_empty,
                        main_probe,
                        only_main_remaining=True,
                    ):
                        await log_hub.push(LogLevel.warn, "子账号列表为空且主账户无可售数量，跳过售卖")
                        await _wait_interruptible(state, interval)
                        continue
                    await log_hub.push(LogLevel.info, "子账号列表为空，将进入售卖流程（主账户）")

                cfg = state.config
                if cfg is None:
                    break

                # 进入售卖前再次同步代理（prep/warmup 期间可能有新的代理绑定）
                sm = await get_session_manager_for_user_id(user_id)

                if state.runner_must_refresh_trading_cache:
                    await log_hub.push(
                        LogLevel.info,
                        "任务已启动/恢复（订阅有效）：强制执行 登录 + 全量子账号 + Mnemonic_Get01 后再售卖",
                    )
                    ok_sync, items_sync = await _full_login_subaccounts_mnemonic_sync(
                        user_id, state, log_hub, sm, cfg
                    )
                    if not ok_sync or not items_sync:
                        state.last_runner_error = "强制同步（登录/子账号/助记词）失败"
                        await _wait_interruptible(state, interval)
                        continue
                    items = items_sync
                    state.runner_must_refresh_trading_cache = False
                    state.runner_sub_prep_date = today_bj

                if settings.runner_lease_enabled:
                    if not await try_acquire_runner_lease(user_id, lease_holder):
                        await log_hub.push(
                            LogLevel.warn,
                            "未获得 runner 租约（其它实例可能占用该用户），跳过本轮 HotWindow",
                        )
                        await _wait_interruptible(state, interval)
                        continue

                closed, relogin_from_sell, skip_day_sell = await _run_hot_maybe_recover_relogin(
                    user_id,
                    state,
                    log_hub,
                    sm,
                    items,
                    sell_start_beijing=start_dt,
                    lease_holder=lease_holder if settings.runner_lease_enabled else None,
                )

                if skip_day_sell:
                    state.logged_in = False
                    state.runner_late_start_skip_outbound_today = today_bj
                    state.last_runner_error = state.last_runner_error or "售卖未登录，本日跳过对外链路"
                    sec = seconds_until_next_beijing_midnight()
                    await log_hub.push(
                        LogLevel.warn,
                        f"售卖未登录已达上限：本日暂停对外售卖至次日 0 点（约 {sec / 3600:.1f} 小时）",
                    )
                    await _wait_interruptible(state, sec)
                    continue

                if closed:
                    state.logged_in = False
                    sec = seconds_until_next_beijing_midnight()
                    await log_hub.push(
                        LogLevel.warn,
                        f"本日交易通道已關閉：暂停至北京时间次日 0 点（约 {sec / 3600:.1f} 小时）",
                    )
                    await _wait_interruptible(state, sec)
                    continue
                if relogin_from_sell:
                    await _wait_interruptible(state, min(interval, 3.0))
                    continue

                await _wait_interruptible(state, interval)
                continue

            # ---------- 未配置定时开售：优先直接售卖；无缓存则 Login 后补拉 ----------
            cfg = state.config
            if cfg is None:
                break

            items = list(state.subaccounts_cache)
            if not items:
                ok_lm, cfg, _, _ = await _rpc_login_merge_config(user_id, state, log_hub, sm, cfg)
                if not ok_lm or cfg is None:
                    await _wait_interruptible(state, interval)
                    continue
                items = await _fetch_subaccounts_resume_retries(user_id, state, log_hub, sm, cfg)
                if not items:
                    state.last_runner_error = "无子账号列表且补拉失败"
                    await _wait_interruptible(state, interval)
                    continue
                state.runner_must_refresh_trading_cache = False
            else:
                await log_hub.push(
                    LogLevel.info,
                    "无定时开售：使用内存子账号缓存优先尝试售卖（接口提示未登录再 Login）",
                )

            cfg = state.config
            if cfg is None:
                break

            # 进入售卖前同步代理绑定
            sm = await get_session_manager_for_user_id(user_id)

            if state.runner_must_refresh_trading_cache:
                await log_hub.push(
                    LogLevel.info,
                    "任务已启动/恢复（订阅有效）：强制执行 登录 + 全量子账号 + Mnemonic_Get01 后再售卖",
                )
                ok_sync, items_sync = await _full_login_subaccounts_mnemonic_sync(
                    user_id, state, log_hub, sm, cfg
                )
                if not ok_sync or not items_sync:
                    state.last_runner_error = "强制同步（登录/子账号/助记词）失败"
                    await _wait_interruptible(state, interval)
                    continue
                items = items_sync
                state.runner_must_refresh_trading_cache = False

            if settings.runner_lease_enabled:
                if not await try_acquire_runner_lease(user_id, lease_holder):
                    await log_hub.push(LogLevel.warn, "未获得 runner 租约，跳过本轮售卖")
                    await _wait_interruptible(state, interval)
                    continue

            closed, relogin_from_sell, skip_day_sell = await _run_hot_maybe_recover_relogin(
                user_id,
                state,
                log_hub,
                sm,
                items,
                sell_start_beijing=None,
                lease_holder=lease_holder if settings.runner_lease_enabled else None,
            )
            if skip_day_sell:
                state.logged_in = False
                today_u = beijing_today_str()
                state.runner_late_start_skip_outbound_today = today_u
                sec = seconds_until_next_beijing_midnight()
                await log_hub.push(
                    LogLevel.warn,
                    f"售卖未登录已达上限：本日暂停对外售卖至次日 0 点（约 {sec / 3600:.1f} 小时）",
                )
                await _wait_interruptible(state, sec)
                continue
            if closed:
                state.logged_in = False
                sec = seconds_until_next_beijing_midnight()
                await log_hub.push(
                    LogLevel.warn,
                    f"本日交易通道已關閉：暂停至北京时间次日 0 点（约 {sec / 3600:.1f} 小时）",
                )
                await _wait_interruptible(state, sec)
                continue
            if relogin_from_sell:
                await _wait_interruptible(state, min(interval, 3.0))
                continue

            await _wait_interruptible(state, interval)

    except asyncio.CancelledError:
        await log_hub.push(LogLevel.warn, "任务已取消")
        raise
    except Exception as e:
        state.last_runner_error = str(e)
        await log_hub.push(LogLevel.error, f"运行异常: {e}")
    finally:
        set_sub_fetch_allowed(True)
        state.logged_in = False
        await log_hub.push(LogLevel.info, "系统已停止")
