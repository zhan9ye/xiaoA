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
    # 拉取子账号成功后 Mnemonic_Get01 一次并缓存；本轮 HotWindow 内所有 ACE_Sell_Son 共用，不再请求 Mnemonic_Get01
    sell_mnemonic_id1: str = ""
    sell_mnemonic_key: str = ""
    sell_mnemonic_str1: str = ""
    # 任务启动/进程恢复后首轮回 Hot 前须完成：Login + 全量子账号 + Mnemonic_Get01（订阅有效由 API 保证）
    runner_must_refresh_trading_cache: bool = False
    # 定时开售：启动日晚于开售+缓冲时置为当日北京 YYYY-MM-DD，runner 本日仅内部等待、不调对外 RPC
    runner_late_start_skip_outbound_today: str = ""
