import asyncio
from datetime import datetime
from typing import List, Optional, Tuple

from app.schemas import AppConfigIn
from app.services.ace_sell_son_service import describe_ace_sell_response, post_ace_sell_son
from app.services.beijing_time import (
    beijing_today_str,
    seconds_until_beijing,
    seconds_until_next_beijing_midnight,
    today_prep_and_start,
)
from app.services.channel_closed import response_indicates_channel_closed
from app.services.global_floor import get_floor_controller
from app.services.login_response_parse import merge_from_rpc_login
from app.services.login_service import rpc_login
from app.services.log_hub import LogHub, LogLevel
from app.services.mnemonic_rpc_service import fetch_mnemonic_meta
from app.services.mnemonic_segments import derive_mnemonic_str1
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


async def _sell_session(
    user_id: int,
    state: AppState,
    cfg: AppConfigIn,
    log_hub: LogHub,
    sm,
    items: List[dict],
    *,
    mid1: str,
    mkey: str,
    mstr: str,
    rk: str,
    uid: str,
) -> bool:
    """
    多轮售卖：每子账号每轮最多 3 次 ACE_Sell_Son；仅 429 影响 SR₄₂₉ 与 floor。
    返回 True 表示响应含「本日交易通道已關閉」，应结束当日循环。
    """
    floor = get_floor_controller(user_id)
    today = beijing_today_str()
    sold_ids = sold_son_ids_for_today(cfg.sold_son_ids_json, today)

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
                g, g_err = totp_now_from_secret_ex(cfg.key_token)
                if not g:
                    await log_hub.push(
                        LogLevel.error,
                        f"无法生成 TOTP 验证码：{g_err or '请检查控制台中的验证器密钥配置'}",
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

                is_429 = code_s == 429
                floor.record_ace_sell_completion(is_429)
                adj_msg = floor.maybe_adjust_floor()
                if adj_msg:
                    await log_hub.push(LogLevel.info, adj_msg)

                if response_indicates_channel_closed(parsed, raw_out):
                    channel_closed = True
                    await log_hub.push(
                        LogLevel.warn,
                        "响应含「本日交易通道已關閉」，停止当日售卖循环",
                    )
                    return True

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

            eff = _effective_interval_ms(cfg, floor.floor_curr_ms)
            await _sleep_between_sell_requests(state, eff)

    return channel_closed


async def run_background(user_id: int, config: AppConfigIn) -> None:
    from app.proxy_binding import get_session_manager_for_user_id
    from app.user_registry import get_or_create_log_hub, get_or_create_state

    state = await get_or_create_state(user_id)
    log_hub: LogHub = await get_or_create_log_hub(user_id)
    sm = await get_session_manager_for_user_id(user_id)

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
                s1 = seconds_until_beijing(prep_dt)
                if s1 > 0:
                    await log_hub.push(
                        LogLevel.info,
                        f"北京时间开售：约 {s1:.0f} 秒后进行准备（登录 + 子账号，不打印 My_Subaccount 原文）",
                    )
                    await _wait_interruptible(state, s1)
                if state.stop_event.is_set():
                    break

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
                    await log_hub.push(LogLevel.warn, f"交易配置写入数据库失败（重启可能丢失最新 UserID/Key）: {ex}")

                rk = cfg.rpc_login_key.strip()
                uid = cfg.rpc_user_id.strip()
                if not rk or not uid:
                    await log_hub.push(LogLevel.error, "Login 未解析出 Key/UserID，跳过本轮售卖")
                    state.last_runner_error = "缺少 Key/UserID"
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
            # 仅 401 视为会话失效；403 常为限流/WAF，避免误判导致频繁重新 Login
            if skip_login and (not sub_out.first_page_ok) and sub_out.first_page_status_code == 401:
                state.logged_in = False
                await log_hub.push(
                    LogLevel.warn,
                    "子账号接口 HTTP 401，会话可能失效，下轮将重新 Login",
                )
                state.last_runner_error = "My_Subaccount HTTP 401"
                await _wait_interruptible(state, interval)
                continue

            state.subaccounts_cache = list(items)
            if prep_start:
                await log_hub.push(
                    LogLevel.success,
                    f"开售前准备：子账号列表已全量拉取并更新缓存，共 {len(items)} 条",
                )
                _, start_dt = prep_start
                s2 = seconds_until_beijing(start_dt)
                if s2 > 0:
                    await log_hub.push(
                        LogLevel.info,
                        f"准备完成：约 {s2:.0f} 秒后开售（Mnemonic_Get01 → ACE_Sell_Son）",
                    )
                    await _wait_interruptible(state, s2)
                if state.stop_event.is_set():
                    break

            v_mn = compute_js_timespan_v()
            meta = await fetch_mnemonic_meta(sm, rpc_key=rk, user_id=uid, v=v_mn)
            if not meta:
                await log_hub.push(
                    LogLevel.warn,
                    "Mnemonic_Get01 失败，本轮不执行售卖（子账号列表已在内存中刷新）",
                )
            else:
                mid1 = str(meta["mnemonicid1"])
                mkey = meta["mnemonickey"]
                mstr = derive_mnemonic_str1(cfg.mnemonic, mid1) or ""
                if not mstr:
                    await log_hub.push(LogLevel.error, "无法从配置助记词推导 mnemonicstr1，本轮不售卖")
                else:
                    g, g_err = totp_now_from_secret_ex(cfg.key_token)
                    if not g:
                        await log_hub.push(
                            LogLevel.error,
                            f"无法生成 TOTP 验证码：{g_err or '请检查控制台中的验证器密钥配置'}",
                        )
                    else:
                        fl = get_floor_controller(user_id)
                        await log_hub.push(
                            LogLevel.info,
                            f"运行参数：用户间隔≥1000ms 与全站 floor（当前 {fl.floor_curr_ms:.0f}ms）取较大值作为请求间隔",
                        )
                        closed = await _sell_session(
                            user_id,
                            state,
                            cfg,
                            log_hub,
                            sm,
                            items,
                            mid1=mid1,
                            mkey=mkey,
                            mstr=mstr,
                            rk=rk,
                            uid=uid,
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

            await _wait_interruptible(state, interval)

    except asyncio.CancelledError:
        await log_hub.push(LogLevel.warn, "任务已取消")
        raise
    except Exception as e:
        state.last_runner_error = str(e)
        await log_hub.push(LogLevel.error, f"运行异常: {e}")
    finally:
        state.logged_in = False
        await log_hub.push(LogLevel.info, "系统已停止")
