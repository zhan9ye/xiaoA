import asyncio
from typing import Dict, List, Optional

from app.services.log_hub import LogHub
from app.services.session_manager import SessionManager, normalize_proxy_url
from app.state import AppState

_states: Dict[int, AppState] = {}
_managers: Dict[int, SessionManager] = {}
_hubs: Dict[int, LogHub] = {}
_lock = asyncio.Lock()


async def get_or_create_state(user_id: int) -> AppState:
    async with _lock:
        if user_id not in _states:
            _states[user_id] = AppState()
        return _states[user_id]


async def get_or_create_log_hub(user_id: int) -> LogHub:
    async with _lock:
        if user_id not in _hubs:
            _hubs[user_id] = LogHub()
        return _hubs[user_id]


def _normalize_proxy_label(proxy_label: Optional[str]) -> Optional[str]:
    t = (proxy_label or "").strip()
    return t or None


async def get_or_create_session_manager(
    user_id: int,
    proxy_url: Optional[str] = None,
    *,
    proxy_label: Optional[str] = None,
) -> SessionManager:
    """proxy_url / proxy_label 与内存中已存在实例不一致时会关闭旧 client 并重建（例如首次领到池内代理）。"""
    desired = normalize_proxy_url(proxy_url)
    desired_label = _normalize_proxy_label(proxy_label)
    async with _lock:
        cur = _managers.get(user_id)
        if cur is not None:
            prev = getattr(cur, "_proxy_url", None)
            prev_label = getattr(cur, "_proxy_label", None)
            if prev == desired and prev_label == desired_label:
                return cur
            await cur.close()
        _managers[user_id] = SessionManager(
            proxy_url=desired,
            platform_user_id=user_id,
            proxy_label=desired_label,
        )
        return _managers[user_id]


async def invalidate_user_outbound_session(user_id: int) -> None:
    """管理端修改出站代理绑定后调用：关闭该用户 SessionManager，下次请求按新代理建连。"""
    mgr_to_close = None
    async with _lock:
        mgr_to_close = _managers.pop(user_id, None)
        st = _states.get(user_id)
        if st is not None:
            st.logged_in = False
    if mgr_to_close is not None:
        await mgr_to_close.close()


async def remove_user_runtime(user_id: int) -> None:
    """删除平台用户时释放内存中的 runner / 会话 / 日志 hub。"""
    task_to_cancel = None
    mgr_to_close = None
    async with _lock:
        st = _states.pop(user_id, None)
        if st:
            st.stop_event.set()
            if st.runner_task is not None and not st.runner_task.done():
                task_to_cancel = st.runner_task
        mgr_to_close = _managers.pop(user_id, None)
        _hubs.pop(user_id, None)
    if task_to_cancel is not None:
        task_to_cancel.cancel()
        try:
            await task_to_cancel
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    if mgr_to_close is not None:
        await mgr_to_close.close()


async def shutdown_all() -> None:
    async with _lock:
        to_wait: List[asyncio.Task] = []
        for st in _states.values():
            st.stop_event.set()
            if st.runner_task is not None and not st.runner_task.done():
                st.runner_task.cancel()
                to_wait.append(st.runner_task)
        for t in to_wait:
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        for mgr in _managers.values():
            await mgr.close()
        _states.clear()
        _managers.clear()
        _hubs.clear()
