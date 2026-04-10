import asyncio
import time
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from app.schemas import AppConfigIn
from app.services.ace_sell_son_service import describe_ace_sell_response, post_ace_sell_son
from app.services.beijing_time import (
    beijing_now,
    beijing_today_str,
    seconds_until_beijing,
    seconds_until_next_beijing_midnight,
    today_prep_and_start,
    wait_open_phases_beijing,
)
from app.services.channel_closed import response_indicates_channel_closed
from app.services.global_floor import get_floor_controller
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


def _effective_interval_ms(cfg: AppConfigIn, floor_ms: float) -> int:
    u = max(1000, int(cfg.request_interval_ms or 1000))
    return int(max(float(u), float(floor_ms)))


async def _await_ace_interval_before_sell(
    state: AppState,
    cfg: AppConfigIn,
    floor_ms: float,
    son_id: str,
) -> None:
    """同账户 + 同 sonId 两次 ACE 均需满足间隔（取较大等待）。"""
    eff_ms = _effective_interval_ms(cfg, floor_ms)
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
        await log_hub.push(
            LogLevel.success,
            f"开售前准备：子账号列表已全量拉取，共 {len(items)} 条（尝试 {attempt}/{max_attempts}）",
        )
        return True, items, cfg, rk, uid

    await log_hub.push(LogLevel.error, f"开售前准备失败：已达最大尝试次数 {max_attempts} 仍未成功")
    return False, [], state.config, rk, uid


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
    每笔 ACE_Sell_Son 前：间隔等待 → Mnemonic_Get01 → ACE_Sell_Son。
    返回 (channel_closed, relogin_recommended)。
    """
    floor = get_floor_controller(user_id)
    today = beijing_today_str()
    channel_closed = False

    while not state.stop_event.is_set() and not channel_closed:
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

        await log_hub.push(LogLevel.info, f"售卖轮次：本轮待处理 {len(remaining_rows)} 个子账号")

        for row in remaining_rows:
            if state.stop_event.is_set():
                break
            cfg = state.config
            if cfg is None:
                break
            rk = cfg.rpc_login_key.strip()
            uid = cfg.rpc_user_id.strip()
            if not rk or not uid:
                await log_hub.push(LogLevel.error, "配置缺少 Key/UserID，中止 HotWindow")
                break

            son_id = resolve_son_id(row)
            if not son_id:
                continue
            full_cnt = ace_amount_string_for_rpc(row)
            cnt = effective_listing_amount_str(cfg, son_id, full_cnt) if full_cnt else ""
            if not cnt:
                continue
            amt = cnt

            biz_success = False
            for attempt in range(1, 4):
                if state.stop_event.is_set():
                    break

                await _await_ace_interval_before_sell(state, cfg, floor.floor_curr_ms, son_id)

                v_mn = compute_js_timespan_v()
                ok_m, code_m, parsed_m, _raw_m = await post_mnemonic_get01(
                    sm, rpc_key=rk, user_id=uid, v=v_mn, lang="cn"
                )
                if ok_m and json_indicates_rpc_not_logged_in(parsed_m):
                    state.logged_in = False
                    await log_hub.push(
                        LogLevel.warn,
                        "Mnemonic_Get01 返回用戶未登錄，中止 HotWindow，下轮将重新 Login",
                    )
                    state.last_runner_error = "Mnemonic_Get01 未登錄"
                    return False, True

                meta = parse_mnemonic_get01_response(parsed_m) if ok_m else None
                if not meta:
                    await log_hub.push(
                        LogLevel.warn,
                        f"Mnemonic_Get01 失败 sonId={son_id}（尝试 {attempt}/3），重试",
                    )
                    eff = _effective_interval_ms(cfg, floor.floor_curr_ms)
                    if attempt < 3:
                        await _sleep_between_sell_requests(state, eff)
                    continue

                mid1 = str(meta["mnemonicid1"])
                mkey = meta["mnemonickey"]
                mstr = derive_mnemonic_str1(cfg.mnemonic, mid1) or ""
                if not mstr:
                    await log_hub.push(
                        LogLevel.error,
                        f"无法推导 mnemonicstr1 sonId={son_id}，跳过本子账号本轮",
                    )
                    break

                g, g_err = totp_now_from_secret_ex(cfg.key_token)
                if not g:
                    await log_hub.push(
                        LogLevel.error,
                        f"无法生成 TOTP：{g_err or '请检查 key_token'}",
                    )
                    break

                v_ace = compute_js_timespan_v()
                ok_s, code_s, parsed, raw_out = await post_ace_sell_son(
                    sm,
                    amount=amt,
                    password=cfg.password,
                    son_id=son_id,
                    mnemonic_id1=mid1,
                    mnemonic_key=mkey,
                    mnemonic_str1=mstr,
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
                floor.record_ace_sell_completion(is_429)
                adj_msg = floor.maybe_adjust_floor()
                if adj_msg:
                    await log_hub.push(LogLevel.info, adj_msg)

                if response_indicates_channel_closed(parsed, raw_out):
                    trust_after = max(0, int(settings.sell_channel_closed_trust_after_seconds or 0))
                    grace_deadline = (
                        sell_start_beijing + timedelta(seconds=trust_after)
                        if sell_start_beijing is not None
                        else None
                    )
                    if grace_deadline is not None and beijing_now() < grace_deadline:
                        detail_grace = describe_ace_sell_response(code_s, parsed, raw_out)
                        await log_hub.push(
                            LogLevel.warn,
                            (
                                f"收到「本日交易通道已關閉」文案，但距配置开售未满 {trust_after}s，"
                                f"视为通道可能尚未开放，继续重试：{detail_grace}"
                            ).strip()[:2600],
                        )
                        eff = _effective_interval_ms(cfg, floor.floor_curr_ms)
                        if attempt < 3:
                            await _sleep_between_sell_requests(state, eff)
                        continue

                    channel_closed = True
                    await log_hub.push(
                        LogLevel.warn,
                        "响应含「本日交易通道已關閉」，停止当日售卖循环",
                    )
                    return True, False

                if json_indicates_rpc_not_logged_in(parsed):
                    state.logged_in = False
                    await log_hub.push(
                        LogLevel.warn,
                        "ACE_Sell_Son 返回用戶未登錄，中止 HotWindow，下轮将重新 Login",
                    )
                    state.last_runner_error = "ACE_Sell_Son 未登錄"
                    return False, True

                detail = describe_ace_sell_response(code_s, parsed, raw_out)
                json_err = isinstance(parsed, dict) and parsed.get("Error") is True
                rate_limited = is_429

                if ok_s and not json_err and not rate_limited:
                    await log_hub.push(
                        LogLevel.success,
                        f"ACE_Sell_Son sonId={son_id} HTTP {code_s} {detail}".strip()[:2600],
                    )
                    biz_success = True
                    break
                if rate_limited:
                    await log_hub.push(
                        LogLevel.warn,
                        f"ACE_Sell_Son sonId={son_id} HTTP 429 限流：{detail}".strip()[:2600],
                    )
                else:
                    tag = "业务失败(Error=true)" if json_err else "请求失败"
                    await log_hub.push(
                        LogLevel.error,
                        f"ACE_Sell_Son sonId={son_id} HTTP {code_s} {tag}：{detail}".strip()[:2600],
                    )

                eff = _effective_interval_ms(cfg, floor.floor_curr_ms)
                if attempt < 3:
                    await _sleep_between_sell_requests(state, eff)

            if biz_success:
                new_json = add_sold_son_json(cfg.sold_son_ids_json, today, son_id)
                new_cfg = cfg.model_copy(update={"sold_son_ids_json": new_json})
                state.config = new_cfg
                try:
                    await persist_trading_config_standalone(user_id, new_cfg)
                except Exception as ex:
                    await log_hub.push(LogLevel.warn, f"已售子账号写入数据库失败: {ex}")

    return channel_closed, False


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

                if skip_prep_fetch:
                    items = list(state.subaccounts_cache)
                    cfg = state.config
                    if cfg is None:
                        break
                    rk = cfg.rpc_login_key.strip()
                    uid = cfg.rpc_user_id.strip()
                    skip_login = bool(state.logged_in and rk and uid)
                    if not skip_login:
                        v_login = compute_js_timespan_v()
                        login_res = await rpc_login(sm, cfg.username, cfg.password, v=v_login)
                        if not login_res.ok:
                            state.logged_in = False
                            await log_hub.push(LogLevel.error, f"Login 失败 HTTP {login_res.status_code}")
                            state.last_runner_error = (login_res.message or "")[:500]
                            await _wait_interruptible(state, interval)
                            continue
                        merged, _ = merge_from_rpc_login(cfg, login_res.response_body)
                        state.config = merged
                        cfg = merged
                        state.logged_in = True
                        rk = cfg.rpc_login_key.strip()
                        uid = cfg.rpc_user_id.strip()
                        try:
                            await persist_trading_config_standalone(user_id, merged)
                        except Exception as ex:
                            await log_hub.push(LogLevel.warn, f"交易配置写入数据库失败: {ex}")
                        if not rk or not uid:
                            await log_hub.push(LogLevel.error, "Login 未解析出 Key/UserID")
                            state.logged_in = False
                            await _wait_interruptible(state, interval)
                            continue
                    else:
                        await log_hub.push(LogLevel.info, "复用会话 Cookie，跳过 Login（HotWindow 前）")

                    await log_hub.push(
                        LogLevel.info,
                        f"HotWindow：复用子账号缓存 {len(items)} 条，禁止全量 My_Subaccount",
                    )
                else:
                    if sell_started and not state.subaccounts_cache:
                        await log_hub.push(
                            LogLevel.error,
                            "已开售但无子账号缓存：HotWindow 禁止拉取 My_Subaccount，本轮不参与售卖",
                        )
                        state.last_runner_error = "已开售无子账号缓存"
                        await _wait_interruptible(state, interval)
                        continue

                    prep_ok, items, cfg, rk, uid = await _timed_prep_phase(
                        user_id, state, log_hub, sm, start_dt
                    )
                    if not prep_ok or cfg is None:
                        await _wait_interruptible(state, interval)
                        continue
                    state.runner_sub_prep_date = today_bj

                    if beijing_now() < start_dt:
                        await log_hub.push(LogLevel.info, "WaitOpen：等待开售整点（北京时间分段对齐）")
                        await wait_open_phases_beijing(
                            state.stop_event,
                            start_dt,
                            settings.sell_wait_open_wake_early_ms,
                        )
                    if state.stop_event.is_set():
                        break

                if not items:
                    await log_hub.push(LogLevel.warn, "子账号列表为空，跳过售卖")
                    await _wait_interruptible(state, interval)
                    continue

                if settings.runner_lease_enabled:
                    if not await try_acquire_runner_lease(user_id, lease_holder):
                        await log_hub.push(
                            LogLevel.warn,
                            "未获得 runner 租约（其它实例可能占用该用户），跳过本轮 HotWindow",
                        )
                        await _wait_interruptible(state, interval)
                        continue

                set_sub_fetch_allowed(False)
                try:
                    closed, relogin_from_sell = await _hot_window_sell_session(
                        user_id,
                        state,
                        cfg,
                        log_hub,
                        sm,
                        items,
                        sell_start_beijing=start_dt,
                        lease_holder=lease_holder if settings.runner_lease_enabled else None,
                    )
                finally:
                    set_sub_fetch_allowed(True)

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

            # ---------- 未配置定时开售：每轮可拉子账号 ----------
            rk = cfg.rpc_login_key.strip()
            uid = cfg.rpc_user_id.strip()
            skip_login = bool(state.logged_in and rk and uid)

            if not skip_login:
                v_login = compute_js_timespan_v()
                login_res = await rpc_login(sm, cfg.username, cfg.password, v=v_login)
                if not login_res.ok:
                    state.logged_in = False
                    await log_hub.push(LogLevel.error, f"登录失败 HTTP {login_res.status_code}")
                    state.last_runner_error = (login_res.message or "")[:500]
                    await _wait_interruptible(state, interval)
                    continue

                merged, _ = merge_from_rpc_login(cfg, login_res.response_body)
                state.config = merged
                cfg = merged
                state.logged_in = True
                await log_hub.push(
                    LogLevel.success,
                    f"登录成功 HTTP {login_res.status_code} UserID={cfg.rpc_user_id.strip() or '?'}",
                )
                try:
                    await persist_trading_config_standalone(user_id, merged)
                except Exception as ex:
                    await log_hub.push(LogLevel.warn, f"交易配置写入数据库失败: {ex}")

                rk = cfg.rpc_login_key.strip()
                uid = cfg.rpc_user_id.strip()
                if not rk or not uid:
                    await log_hub.push(LogLevel.error, "Login 未解析出 Key/UserID，跳过本轮售卖")
                    state.logged_in = False
                    await _wait_interruptible(state, interval)
                    continue
            else:
                await log_hub.push(LogLevel.info, "复用会话 Cookie，跳过 Login")

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
            items = sub_out.items
            if skip_login and (
                (not sub_out.first_page_ok and sub_out.first_page_status_code == 401)
                or sub_out.not_logged_in
            ):
                state.logged_in = False
                await log_hub.push(
                    LogLevel.warn,
                    "子账号接口会话失效，下轮将重新 Login",
                )
                state.last_runner_error = "My_Subaccount 未登錄或 401"
                await _wait_interruptible(state, interval)
                continue

            state.subaccounts_cache = list(items)

            if settings.runner_lease_enabled:
                if not await try_acquire_runner_lease(user_id, lease_holder):
                    await log_hub.push(LogLevel.warn, "未获得 runner 租约，跳过本轮售卖")
                    await _wait_interruptible(state, interval)
                    continue

            await log_hub.push(
                LogLevel.info,
                f"运行参数：用户间隔≥1000ms 与全站 floor（当前 {get_floor_controller(user_id).floor_curr_ms:.0f}ms）",
            )
            closed, relogin_from_sell = await _hot_window_sell_session(
                user_id,
                state,
                cfg,
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
