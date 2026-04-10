import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.schemas import AppConfigIn


@dataclass
class AppState:
    config: Optional[AppConfigIn] = None
    runner_task: Optional[asyncio.Task] = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    last_runner_error: Optional[str] = None
    logged_in: bool = False
    subaccounts_cache: List[Dict[str, Any]] = field(default_factory=list)
    # 北京时间日期 YYYY-MM-DD；配置了 sell_start_time 时，表示该日已在开售前完成一次全量 My_Subaccount，循环内复用缓存不再重复拉取
    runner_sub_prep_date: str = ""
    # 同账户两次 ACE_Sell_Son 间隔（time.monotonic）；并 per-sonId 记录
    last_ace_sell_monotonic: float = 0.0
    last_ace_sell_monotonic_by_son: Dict[str, float] = field(default_factory=dict)
