"""
全站动态 floor_curr：仅由 ACE_Sell_Son 的 HTTP 429 驱动滑动窗口 SR₄₂₉。
每台进程各自维护窗口与 floor（不共享 Redis）。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Dict, Optional, Tuple

# 滑动窗口样本数
WINDOW_SIZE = 100
FLOOR_MIN_MS = 50.0
FLOOR_MAX_MS = 500.0
FLOOR_STEP_UP_MS = 50.0
FLOOR_STEP_DOWN_MS = 20.0
SR_LOW = 0.80
SR_HIGH = 0.90
MIN_ADJUST_INTERVAL_S = 10.0


class GlobalFloorController:
    """仅统计「已完成」的 ACE_Sell_Son：窗口内 True=429，False=非 429。"""

    def __init__(self) -> None:
        self._window: deque[bool] = deque(maxlen=WINDOW_SIZE)
        self.floor_curr_ms: float = FLOOR_MIN_MS
        self._last_adjust_mono: float = 0.0
        self._prev_eval_ge_high: bool = False

    def record_ace_sell_completion(self, http_is_429: bool) -> None:
        self._window.append(bool(http_is_429))

    def snapshot(self) -> Tuple[float, Optional[float], int]:
        """(floor_curr_ms, sr429 或 None, 窗口内样本数)"""
        n = len(self._window)
        if n == 0:
            return self.floor_curr_ms, None, 0
        c429 = sum(1 for x in self._window if x)
        sr = 1.0 - (c429 / float(n))
        return self.floor_curr_ms, sr, n

    def maybe_adjust_floor(self) -> Optional[str]:
        """
        每次完成一条 ACE_Sell_Son 后调用。未满 100 条不调 floor；改 floor 至少间隔 10s。
        返回非空时写入日志。
        """
        if len(self._window) < WINDOW_SIZE:
            return None

        c429 = sum(1 for x in self._window if x)
        sr = 1.0 - (c429 / float(WINDOW_SIZE))
        now = time.monotonic()
        if now - self._last_adjust_mono < MIN_ADJUST_INTERVAL_S:
            return None

        # 升温：SR < 80%
        if sr < SR_LOW:
            self._prev_eval_ge_high = False
            self.floor_curr_ms = min(FLOOR_MAX_MS, self.floor_curr_ms + FLOOR_STEP_UP_MS)
            self._last_adjust_mono = now
            return f"全站 floor：SR₄₂₉={sr:.1%}（窗口{WINDOW_SIZE}）<80%，升温至 {self.floor_curr_ms:.0f}ms"

        # 降温：连续两次评估均 ≥90%
        if sr >= SR_HIGH:
            if self._prev_eval_ge_high:
                self.floor_curr_ms = max(FLOOR_MIN_MS, self.floor_curr_ms - FLOOR_STEP_DOWN_MS)
                self._last_adjust_mono = now
                self._prev_eval_ge_high = False
                return f"全站 floor：SR₄₂₉={sr:.1%} 连续≥90%，降温至 {self.floor_curr_ms:.0f}ms"
            self._prev_eval_ge_high = True
            return None

        self._prev_eval_ge_high = False
        return None


_controllers: Dict[int, GlobalFloorController] = {}


def get_floor_controller(user_id: int) -> GlobalFloorController:
    if user_id not in _controllers:
        _controllers[user_id] = GlobalFloorController()
    return _controllers[user_id]
