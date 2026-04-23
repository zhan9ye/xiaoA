"""Runner 启停与定时开售相关的共享逻辑（避免 main 与 admin 循环依赖）。"""

from __future__ import annotations

import asyncio
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas import AppConfigIn
from app.services.beijing_time import beijing_today_str, timed_sell_past_grace_deadline
from app.services.runner import run_background
from app.settings import settings
from app.state import AppState
from app.trading_config_repo import ensure_trading_config_loaded, get_active_trading_slot, persist_trading_config
from app.user_registry import get_or_create_state


def apply_timed_sell_late_start_skip_flag(st: AppState, cfg: Optional[AppConfigIn]) -> None:
    """
    配置了 sell_start_time 且当前已超过「开售整点 + sell_start_missed_grace_minutes」时，
    标记本北京日仅内部等待，runner 不调登录/子账号/助记词/售卖等对外接口。
    """
    if cfg is None or not (cfg.sell_start_time or "").strip():
        st.runner_late_start_skip_outbound_today = ""
        return
    g = max(0, int(settings.sell_start_missed_grace_minutes or 10))
    if timed_sell_past_grace_deadline(cfg.sell_start_time, g):
        st.runner_late_start_skip_outbound_today = beijing_today_str()
    else:
        st.runner_late_start_skip_outbound_today = ""


async def runner_execute_stop(db: AsyncSession, user_id: int) -> AppState:
    """
    与 POST /api/run/stop 相同：runner_enabled 落库为 false，取消并等待 run_background 结束。
    """
    st = await get_or_create_state(user_id)
    await ensure_trading_config_loaded(db, user_id, st)
    if st.config is not None:
        st.config = st.config.model_copy(update={"runner_enabled": False})
        slot_a = await get_active_trading_slot(db, user_id)
        await persist_trading_config(db, user_id, slot_a, st.config)
    st.stop_event.set()
    if st.runner_task is not None and not st.runner_task.done():
        st.runner_task.cancel()
        try:
            await st.runner_task
        except asyncio.CancelledError:
            pass
    st.runner_task = None
    st.hot_sell_window_active = False
    return st


async def runner_execute_start_core(db: AsyncSession, user_id: int, st: AppState) -> None:
    """
    与 POST /api/run/start 中「真正启动」段相同。
    前置：已 ensure_trading_config_loaded；无运行中 runner_task；st.config 非空。
    """
    cfg = st.config
    if cfg is None:
        return
    if st.runner_task is not None and not st.runner_task.done():
        return
    st.config = cfg.model_copy(update={"runner_enabled": True})
    slot_a = await get_active_trading_slot(db, user_id)
    await persist_trading_config(db, user_id, slot_a, st.config)
    st.stop_event = asyncio.Event()
    st.runner_must_refresh_trading_cache = True
    st.hot_sell_window_active = False
    apply_timed_sell_late_start_skip_flag(st, st.config)
    st.runner_task = asyncio.create_task(run_background(user_id, st.config))


def should_restart_runner_like_frontend_after_proxy_rebind(st: AppState) -> bool:
    """runner_enabled 为开且 asyncio 任务正在跑（售卖中），换绑后需先停再起，与前端一致。"""
    if st.config is None or not st.config.runner_enabled:
        return False
    return st.runner_task is not None and not st.runner_task.done()
