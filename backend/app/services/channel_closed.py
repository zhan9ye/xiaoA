"""响应中含下列文案时终止当日售卖循环。

配置了定时开售时，runner 会在「北京时间 ≥ 开售时刻 + sell_channel_closed_trust_after_seconds」
之后才采纳该信号，避免整点前后上游尚未开门时误判收工。见 app.services.runner._sell_session。
"""

from __future__ import annotations

import json
from typing import Any, Optional

# 简繁体均匹配，避免上游返回体编码不一致
DAY_SELL_END_PHRASES = (
    "本日交易通道已關閉",
    "本日交易通道已关闭",
    "当前可售额度已达上限",
    "當前可售額度已達上限",
)

# 兼容旧引用
CHANNEL_CLOSED_PHRASE = DAY_SELL_END_PHRASES[0]


def _response_text_blob(parsed: Any, raw_text: str) -> str:
    blob = ""
    if raw_text:
        blob += raw_text
    if parsed is not None:
        try:
            blob += json.dumps(parsed, ensure_ascii=False)
        except Exception:
            blob += str(parsed)
    return blob


def response_day_sell_end_reason(parsed: Any, raw_text: str) -> Optional[str]:
    blob = _response_text_blob(parsed, raw_text)
    for phrase in DAY_SELL_END_PHRASES:
        if phrase in blob:
            return phrase
    return None


def response_indicates_channel_closed(parsed: Any, raw_text: str) -> bool:
    return response_day_sell_end_reason(parsed, raw_text) is not None
