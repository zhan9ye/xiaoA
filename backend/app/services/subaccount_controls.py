"""子账号列表：开售后的刷新/排序锁定判断（与 runner 状态一致）。"""

from app.services.beijing_time import beijing_now, today_prep_and_start
from app.state import AppState


def subaccount_controls_locked(st: AppState) -> bool:
    """
    True：禁止刷新子账号、禁止改售卖排序。
    - 配置了定时开售且北京时间已过今日开售整点；
    - 或未配置定时开售但本轮已进入过 HotWindow 售卖。
    """
    running = st.runner_task is not None and not st.runner_task.done()
    if not running:
        return False
    cfg = st.config
    if cfg is None:
        return False
    sst = (cfg.sell_start_time or "").strip()
    if sst:
        tup = today_prep_and_start(sst)
        if tup:
            _, start_dt = tup
            if beijing_now() >= start_dt:
                return True
        return False
    return bool(st.hot_sell_session_started)
