"""响应中含此文案时终止当日售卖循环。"""

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
