"""定时开售 HotWindow 期间禁止全量 My_Subaccount（由 contextvar 控制）。"""

from contextvars import ContextVar

_sub_fetch_allowed: ContextVar[bool] = ContextVar("_sub_fetch_allowed", default=True)


def set_sub_fetch_allowed(allowed: bool) -> None:
    _sub_fetch_allowed.set(allowed)


def sub_fetch_allowed() -> bool:
    return _sub_fetch_allowed.get()


def assert_sub_fetch_allowed() -> None:
    if not _sub_fetch_allowed.get():
        raise RuntimeError("HotWindow 禁止调用 fetch_all_subaccounts / 全量 My_Subaccount")
