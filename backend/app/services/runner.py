import asyncio
import time
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
from app.services.mnemonic_segments import derive_mnemonic_str1
from app.services.runner_fetch_guard import set_sub_fetch_allowed
from app.services.runner_lease import get_runner_lease_holder_id, renew_runner_lease_if_holder, try_acquire_runner_lease
from app.services.rpc_auth_signals import json_indicates_rpc_not_logged_in
from app.services.selling_eligibility import (
    ace_amount_string_for_rpc,
    effective_listing_amount_str,
    resolve_son_id,
    resolve_subaccount_display_name,
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


async def _sleep_between_sell_requests(state: AppState, ms: int) -> None:
    await _wait_interruptible(state, max(0.0, float(ms) / 1000.0))


def _user_sell_interval_ms(cfg: AppConfigIn) -> int:
    """仅使用交易配置 request_interval_ms，下限 500ms（与保存接口一致）。"""
    return max(500, int(cfg.request_interval_ms or 500))


async def _await_ace_interval_before_sell(
    state: AppState,
    cfg: AppConfigIn,
    son_id: str,
) -> None:
    """同账户 + 同 sonId 两次 ACE 均需满足间隔（取较大等待）。"""
    eff_ms = _user_sell_interval_ms(cfg)
    eff_s = eff_ms / 1000.0
    now_m = time.monotonic()
    last_acc = state.last_ace_sell_monotonic
    last_son = state.last_ace_sell_monotonic_by_son.get(son_id, 0.0)
    wait_acc = max(0.0, eff_s - (now_m - last_acc)) if last_acc > 0.0 else 0.0
    wait_son = max(0.0, eff_s - (now_m - last_son)) if last_son > 0.0 else 0.0
    wait_s = max(wait_acc, wait_son)
    if wait_s > 0.0:
        await _sleep_between_sell_requests(state, int(wait_s * 1000.0))


def _mark_ace_sell_sent(state: AppState, son_id: str) -> None:
    m = time.monotonic()
    state.last_ace_sell_monotonic = m
    state.last_ace_sell_monotonic_by_son[son_id] = m


def _clear_sell_mnemonic_cache(state: AppState) -> None:
    state.sell_mnemonic_id1 = ""
    state.sell_mnemonic_key = ""
    state.sell_mnemonic_str1 = ""


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
        await log_hub.push(LogLevel.error, "无法从配置助记词推导 mnemonicstr1，无法缓存")
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


async def _run_hot_maybe_recover_relogin(
    user_id: int,
    state: AppState,
    log_hub: LogHub,
    sm,
    items: List[dict],
    *,
    sell_start_beijing: Optional[datetime],
    lease_holder: Optional[str],
) -> Tuple[bool, bool]:
    """
    先直接 HotWindow；若返回需重新登录，则 Login → 读缓存 → 空则补拉（重试）
    → 再跑一轮 HotWindow。返回 (channel_closed, relogin_still_needed)。
    """
    cfg = state.config
    if cfg is None:
        return False, True

    if not await _ensure_sell_mnemonic_cached(state, sm, log_hub, cfg):
        await log_hub.push(LogLevel.error, "助记词缓存未就绪，无法进入 HotWindow")
        state.last_runner_error = "助记词缓存失败"
        return False, True

    set_sub_fetch_allowed(False)
    try:
        closed, relogin = await _hot_window_sell_session(
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
        set_sub_fetch_allowed(True)

    if closed or not relogin:
        return closed, relogin

    ok, cfg2, _, _ = await _rpc_login_merge_config(user_id, state, log_hub, sm, state.config)
    if not ok or cfg2 is None:
        return False, True

    set_sub_fetch_allowed(True)
    try:
        items2 = await _resolve_items_cache_or_resume_fetch(user_id, state, log_hub, sm, cfg2)
    finally:
        set_sub_fetch_allowed(True)

    if not items2:
        state.last_runner_error = "Login 后子账号仍为空且补拉失败"
        return False, True

    cfg = state.config
    if cfg is None:
        return False, True

    if not await _ensure_sell_mnemonic_cached(state, sm, log_hub, cfg):
        state.last_runner_error = "Login 后助记词缓存失败"
        return False, True

    set_sub_fetch_allowed(False)
    try:
        closed2, relogin2 = await _hot_window_sell_session(
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
        set_sub_fetch_allowed(True)
    return closed2, relogin2


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

        cfg = state.config
        if cfg is None:
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
            await log_hub.push(LogLevel.error, "准备阶段：子账号已拉取但 Mnemonic_Get01 失败")
            return False, [], cfg, rk, uid
        return True, items, cfg, rk, uid

    await log_hub.push(LogLevel.error, f"开售前准备失败：已达最大尝试次数 {max_attempts} 仍未成功")
    return False, [], state.config, rk, uid


async def _sell_open_warmup_loop(
    state: AppState,
    sm,
    log_hub: LogHub,
    start_dt: datetime,
) -> None:
    """
    与 WaitOpen 并行：在开售整点前若干秒内周期性请求 Mnemonic_Get01 预热 TLS/TCP 连接。
    选用 Mnemonic_Get01 而非 My_Subaccount：同域名同端口，返回极小 JSON，无副作用。
    """
    before = max(0, int(settings.sell_warmup_seconds_before_open or 0))
    if before <= 0:
        return
    ping_sec = max(2.0, float(settings.sell_warmup_ping_interval_seconds or 6.0))
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=BJ)
    first_at = start_dt - timedelta(seconds=before)
    while not state.stop_event.is_set():
        now = beijing_now()
        if now >= start_dt:
            return
        if now < first_at:
            await wait_interruptible_until_beijing(state.stop_event, first_at)
            continue
        cfg = state.config
        if cfg is None:
            return
        rk = cfg.rpc_login_key.strip()
        uid = cfg.rpc_user_id.strip()
        if not rk or not uid:
            return
        v = compute_js_timespan_v()
        ok, code, _parsed, _raw = await post_mnemonic_get01(
            sm, rpc_key=rk, user_id=uid, v=v, lang="cn"
        )
        if not ok:
            await log_hub.push(
                LogLevel.warn,
                f"开售预热：Mnemonic_Get01 HTTP {code}（将继续重试至整点）",
            )
        next_deadline = beijing_now() + timedelta(seconds=ping_sec)
        if next_deadline > start_dt:
            next_deadline = start_dt
        await wait_interruptible_until_beijing(state.stop_event, next_deadline)


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
) -> Tuple[bool, bool]:
    """
    HotWindow：禁止在本函数内调用 fetch_all_subaccounts（由外层 fetch 守卫保证）。
    助记词使用 state 缓存；ACE_Sell_Son 在 Semaphore 控制下并发（hot_window_concurrency），
    每笔仍遵守 request_interval_ms（同账户/同 sonId 节流）。
    定时开售时：本函数内首轮并发 ACE 前先等待 sell_channel_closed_grace_retry_ms 再发第一批。
    返回 (channel_closed, relogin_recommended)。
    """
    today = beijing_today_str()
    concurrency = max(1, int(settings.hot_window_concurrency or 1))
    semaphore = asyncio.Semaphore(concurrency)
    channel_ev = asyncio.Event()
    relogin_ev = asyncio.Event()
    cfg_lock = asyncio.Lock()
    initial_batch_delay_applied = False

    while not state.stop_event.is_set():
        cfg = state.config
        if cfg is None:
            break
        sold_ids = sold_son_ids_for_today(cfg.sold_son_ids_json, today)

        remaining_rows: List[dict] = []
        for row in items:
            ok_el, _ = subaccount_eligible_for_ace_sell(row, cfg)
            if not ok_el:
                continue
            son_id = resolve_son_id(row)
            if not son_id or son_id in sold_ids:
                continue
            full_cnt = ace_amount_string_for_rpc(row)
            cnt = effective_listing_amount_str(cfg, son_id, full_cnt) if full_cnt else ""
            if not cnt:
                continue
            remaining_rows.append(row)

        if not remaining_rows:
            await log_hub.push(LogLevel.info, "当日待售子账号已处理完毕（无未成功且符合条件者）")
            break

        await log_hub.push(
            LogLevel.info,
            f"售卖轮次：本轮待处理 {len(remaining_rows)} 个子账号",
        )

        rk0 = cfg.rpc_login_key.strip()
        uid0 = cfg.rpc_user_id.strip()
        if not rk0 or not uid0:
            await log_hub.push(LogLevel.error, "配置缺少 Key/UserID，中止 HotWindow")
            break

        mid1 = state.sell_mnemonic_id1
        mkey = state.sell_mnemonic_key
        mstr = state.sell_mnemonic_str1
        if not mid1 or not mkey or not mstr:
            await log_hub.push(
                LogLevel.info,
                "售卖助记词缓存不完整，尝试 Mnemonic_Get01 一次并回写缓存",
            )
            if await _refresh_sell_mnemonic_cache(state, sm, log_hub, cfg):
                mid1 = state.sell_mnemonic_id1
                mkey = state.sell_mnemonic_key
                mstr = state.sell_mnemonic_str1
        if not mid1 or not mkey or not mstr:
            await log_hub.push(
                LogLevel.error,
                "售卖助记词缓存仍不可用（请确认已登录且配置助记词正确），中止 HotWindow",
            )
            state.last_runner_error = "助记词缓存为空"
            return False, True

        async def process_row(row: dict) -> None:
            if channel_ev.is_set() or relogin_ev.is_set() or state.stop_event.is_set():
                return
            cfg0 = state.config
            if cfg0 is None:
                return
            son_id = resolve_son_id(row)
            if not son_id:
                return
            full_cnt = ace_amount_string_for_rpc(row)
            cnt = effective_listing_amount_str(cfg0, son_id, full_cnt) if full_cnt else ""
            if not cnt:
                return
            amt = cnt
            m_a, m_b, m_c = state.sell_mnemonic_id1, state.sell_mnemonic_key, state.sell_mnemonic_str1
            if not m_a or not m_b or not m_c:
                return

            async with semaphore:
                biz_success = False
                trust_after = max(0, int(settings.sell_channel_closed_trust_after_seconds or 0))
                grace_deadline = (
                    sell_start_beijing + timedelta(seconds=trust_after)
                    if sell_start_beijing is not None
                    else None
                )
                in_grace = grace_deadline is not None and beijing_now() < grace_deadline
                # 信任窗口内仅对「通道尚未开放」文案密集重试；其余失败（429/超时/业务错等）单本子账号只打一发即进入下一子账户
                max_attempts = 100 if in_grace else 1
                logged_grace_for_son = False
                for attempt in range(1, max_attempts + 1):
                    if channel_ev.is_set() or relogin_ev.is_set() or state.stop_event.is_set():
                        break
                    cfg_row = state.config
                    if cfg_row is None:
                        break

                    in_grace = grace_deadline is not None and beijing_now() < grace_deadline
                    if not in_grace:
                        await _await_ace_interval_before_sell(state, cfg_row, son_id)

                    g, g_err = totp_now_from_secret_ex(cfg_row.key_token)
                    if not g:
                        await log_hub.push(
                            LogLevel.error,
                            f"无法生成 TOTP sonId={son_id}：{g_err or '请检查 key_token'}",
                        )
                        break

                    rk = cfg_row.rpc_login_key.strip()
                    uid = cfg_row.rpc_user_id.strip()
                    if not rk or not uid:
                        break

                    v_ace = compute_js_timespan_v()
                    ok_s, code_s, parsed, raw_out = await post_ace_sell_son(
                        sm,
                        amount=amt,
                        password=cfg_row.password,
                        son_id=son_id,
                        mnemonic_id1=m_a,
                        mnemonic_key=m_b,
                        mnemonic_str1=m_c,
                        g_code=g,
                        count=cnt,
                        rpc_key=rk,
                        user_id=uid,
                        v=v_ace,
                    )
                    _mark_ace_sell_sent(state, son_id)
                    if lease_holder:
                        await renew_runner_lease_if_holder(user_id, lease_holder)

                    is_429 = code_s == 429

                    if response_indicates_channel_closed(parsed, raw_out):
                        in_grace = grace_deadline is not None and beijing_now() < grace_deadline
                        if in_grace:
                            grace_ms = max(0, int(settings.sell_channel_closed_grace_retry_ms or 0))
                            if not logged_grace_for_son:
                                logged_grace_for_son = True
                                await log_hub.push(
                                    LogLevel.warn,
                                    f"sonId={son_id} 通道尚未开放，信任窗口内短间隔重试（约 {grace_ms}ms）",
                                )
                            if grace_ms > 0 and attempt < max_attempts:
                                await _sleep_between_sell_requests(state, grace_ms)
                            continue

                        if not channel_ev.is_set():
                            channel_ev.set()
                            await log_hub.push(
                                LogLevel.warn,
                                "响应含「本日交易通道已關閉」，停止当日售卖循环",
                            )
                        return

                    if json_indicates_rpc_not_logged_in(parsed):
                        state.logged_in = False
                        if not relogin_ev.is_set():
                            relogin_ev.set()
                            await log_hub.push(
                                LogLevel.warn,
                                "ACE_Sell_Son 返回用戶未登錄，中止 HotWindow，下轮将重新 Login",
                            )
                            state.last_runner_error = "ACE_Sell_Son 未登錄"
                        return

                    json_err = isinstance(parsed, dict) and parsed.get("Error") is True
                    rate_limited = is_429

                    if ok_s and not json_err and not rate_limited:
                        sub_ok = resolve_subaccount_display_name(row) or son_id
                        await log_hub.push(
                            LogLevel.success,
                            f"恭喜子账户：{sub_ok}，售卖成功！售卖数量：{cnt}",
                        )
                        biz_success = True
                        break
                    if rate_limited:
                        sub_name = resolve_subaccount_display_name(row) or son_id
                        await log_hub.push(
                            LogLevel.warn,
                            f"子账号：{sub_name}，售卖失败，限流！",
                        )
                    elif json_err:
                        sub_name = resolve_subaccount_display_name(row) or son_id
                        await log_hub.push(
                            LogLevel.error,
                            f"子账号：{sub_name}，售卖失败，参数不正确！",
                        )
                    elif code_s == 0:
                        sub_name = resolve_subaccount_display_name(row) or son_id
                        await log_hub.push(
                            LogLevel.error,
                            f"子账号：{sub_name}，售卖失败，服务器没有响应！",
                        )
                    else:
                        sub_name = resolve_subaccount_display_name(row) or son_id
                        await log_hub.push(
                            LogLevel.error,
                            f"子账户：{sub_name}，售卖失败，服务器忙！",
                        )

                    break

                if biz_success:
                    async with cfg_lock:
                        cfg_cur = state.config
                        if cfg_cur is None:
                            return
                        new_json = add_sold_son_json(cfg_cur.sold_son_ids_json, today, son_id)
                        new_cfg = cfg_cur.model_copy(update={"sold_son_ids_json": new_json})
                        state.config = new_cfg
                        try:
                            await persist_trading_config_standalone(user_id, new_cfg)
                        except Exception as ex:
                            await log_hub.push(
                                LogLevel.warn,
                                f"已售子账号写入数据库失败: {ex}",
                            )

        if not initial_batch_delay_applied:
            initial_batch_delay_applied = True
            if sell_start_beijing is not None:
                pre_ms = max(0, int(settings.sell_channel_closed_grace_retry_ms or 0))
                if pre_ms > 0:
                    await _sleep_between_sell_requests(state, pre_ms)

        results = await asyncio.gather(
            *[process_row(r) for r in remaining_rows],
            return_exceptions=True,
        )
        for res in results:
            if isinstance(res, BaseException):
                await log_hub.push(LogLevel.error, f"售卖并发任务异常：{res!r}")

        if channel_ev.is_set():
            return True, False
        if relogin_ev.is_set():
            return False, True

    return False, False


async def run_background(user_id: int, config: AppConfigIn) -> None:
    from app.proxy_binding import get_session_manager_for_user_id
    from app.user_registry import get_or_create_log_hub, get_or_create_state

    state = await get_or_create_state(user_id)
    log_hub: LogHub = await get_or_create_log_hub(user_id)
    sm = await get_session_manager_for_user_id(user_id)
    lease_holder = get_runner_lease_holder_id()

    state.last_runner_error = None
    state.logged_in = False

    try:
        interval = float(settings.runner_loop_interval_seconds)

        while not state.stop_event.is_set():
            cfg: Optional[AppConfigIn] = state.config
            if cfg is None:
                await log_hub.push(LogLevel.error, "配置丢失，停止任务")
                state.last_runner_error = "配置丢失"
                break

            prep_start: Optional[Tuple[datetime, datetime]] = None
            if (cfg.sell_start_time or "").strip():
                prep_start = today_prep_and_start(cfg.sell_start_time)
                if not prep_start:
                    await log_hub.push(LogLevel.warn, "sell_start_time 无法解析，跳过定时等待")

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
                    await _wait_interruptible(state, sec)
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
                    await log_hub.push(
                        LogLevel.info,
                        f"PreWait：约 {s1:.0f} 秒后进行准备（登录 + 子账号），此阶段不发 RPC",
                    )
                    await _wait_interruptible(state, s1)
                if state.stop_event.is_set():
                    break

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
                            await asyncio.gather(
                                wait_open_phases_beijing(
                                    state.stop_event,
                                    start_dt,
                                    settings.sell_wait_open_wake_early_ms,
                                ),
                                _sell_open_warmup_loop(state, sm, log_hub, start_dt),
                            )
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
                    await log_hub.push(LogLevel.warn, "子账号列表为空，跳过售卖")
                    await _wait_interruptible(state, interval)
                    continue

                cfg = state.config
                if cfg is None:
                    break

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

                closed, relogin_from_sell = await _run_hot_maybe_recover_relogin(
                    user_id,
                    state,
                    log_hub,
                    sm,
                    items,
                    sell_start_beijing=start_dt,
                    lease_holder=lease_holder if settings.runner_lease_enabled else None,
                )

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

            await log_hub.push(
                LogLevel.info,
                f"售卖请求间隔固定为交易配置 request_interval_ms={_user_sell_interval_ms(cfg)}ms",
            )
            closed, relogin_from_sell = await _run_hot_maybe_recover_relogin(
                user_id,
                state,
                log_hub,
                sm,
                items,
                sell_start_beijing=None,
                lease_holder=lease_holder if settings.runner_lease_enabled else None,
            )
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
