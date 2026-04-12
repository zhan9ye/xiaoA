"""子账号列表：HotWindow 售卖期间的刷新/排序锁定（与 runner 状态一致）。"""

from app.state import AppState


def subaccount_controls_locked(st: AppState) -> bool:
    """
    True：禁止刷新子账号、禁止改售卖排序。
    仅在 runner 正在执行 HotWindow（实际发起 ACE_Sell_Son 批次）时为 True；
    开售前等待、准备、点「开始」但未进入售卖阶段时不锁定。
    """
    running = st.runner_task is not None and not st.runner_task.done()
    if not running:
        return False
    return bool(st.hot_sell_window_active)
