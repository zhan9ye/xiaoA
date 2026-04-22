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
from app.trading_config_repo import ensure_trading_config_loaded
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


async def cancel_running_runner_task_keep_enabled(st: AppState) -> bool:
    """
    取消正在运行的 run_background，不把 runner_enabled 写入 false（与用户点「停止」不同）。
    返回是否曾处于运行中。
    """
    if st.runner_task is None or st.runner_task.done():
        return False
    st.stop_event.set()
    st.runner_task.cancel()
    try:
        await st.runner_task
    except asyncio.CancelledError:
        pass
    st.runner_task = None
    st.hot_sell_window_active = False
    return True


async def restart_runner_if_enabled_after_proxy_change(
    db: AsyncSession,
    user_id: int,
    was_running: bool,
) -> None:
    """
    换绑出站代理后：若此前任务在跑，则按当前库内配置再起 run_background（与 run_start 一致，但不重复 persist runner_enabled）。
    """
    if not was_running:
        return
    st = await get_or_create_state(user_id)
    if not await ensure_trading_config_loaded(db, user_id, st):
        return
    if st.config is None or not st.config.runner_enabled:
        return
    st.stop_event = asyncio.Event()
    st.runner_must_refresh_trading_cache = True
    st.hot_sell_window_active = False
    apply_timed_sell_late_start_skip_flag(st, st.config)
    st.runner_task = asyncio.create_task(run_background(user_id, st.config))
