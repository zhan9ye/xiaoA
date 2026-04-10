"""响应中含此文案时终止当日售卖循环。

配置了定时开售时，runner 会在「北京时间 ≥ 开售时刻 + sell_channel_closed_trust_after_seconds」
之后才采纳该信号，避免整点前后上游尚未开门时误判收工。见 app.services.runner._sell_session。
"""

from __future__ import annotations

import json
from typing import Any


CHANNEL_CLOSED_PHRASE = "本日交易通道已關閉"


def response_indicates_channel_closed(parsed: Any, raw_text: str) -> bool:
    blob = ""
    if raw_text:
        blob += raw_text
    if parsed is not None:
        try:
            blob += json.dumps(parsed, ensure_ascii=False)
        except Exception:
            blob += str(parsed)
    return CHANNEL_CLOSED_PHRASE in blob
