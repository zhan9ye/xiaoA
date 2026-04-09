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
