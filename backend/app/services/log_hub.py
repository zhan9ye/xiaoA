import asyncio
import json
from datetime import datetime, timezone, timedelta
from enum import Enum

from fastapi import WebSocket


class LogLevel(str, Enum):
    info = "info"
    success = "success"
    warn = "warn"
    error = "error"


def _cn_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


class LogHub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._history: list[dict] = []
        self._max_history = 500

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
        for item in self._history[-200:]:
            await ws.send_text(json.dumps(item, ensure_ascii=False))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def clear_history(self) -> None:
        """清空服务端环形缓冲，避免重连 WebSocket 再次推送旧日志。"""
        async with self._lock:
            self._history.clear()

    async def push(self, level: LogLevel, message: str) -> None:
        now = _cn_now()
        payload = {
            "ts": now.strftime("%H:%M:%S"),
            "level": level.value,
            "message": message,
        }
        async with self._lock:
            self._history.append(payload)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history :]
            clients = list(self._clients)
        text = json.dumps(payload, ensure_ascii=False)
        dead: list[WebSocket] = []
        for client in clients:
            try:
                await client.send_text(text)
            except Exception:
                dead.append(client)
        for c in dead:
            async with self._lock:
                self._clients.discard(c)
