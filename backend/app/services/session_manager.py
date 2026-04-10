import asyncio
import os
from typing import Optional

import certifi
import httpx

from app.middleware_request_log import httpx_outbound_response_log_hook
from app.settings import settings


def _outbound_verify_ca_bundle() -> str:
    """出站 HTTPS 校验使用的 CA 包路径。优先 SSL_CERT_FILE / REQUESTS_CA_BUNDLE，否则用 certifi 内置包，避免仅依赖系统 /usr/lib/ssl。"""
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        p = (os.environ.get(key) or "").strip()
        if p and os.path.isfile(p):
            return p
    return certifi.where()


def normalize_proxy_url(proxy_url: Optional[str]) -> Optional[str]:
    """无 scheme 的 `ip:port` / `user:pass@host:port` 补全为 http://，供 httpx 与 urlparse 一致识别。"""
    t = (proxy_url or "").strip() or None
    if not t:
        return None
    if "://" not in t:
        return "http://" + t.lstrip("/")
    return t


class SessionManager:
    """复用 httpx.AsyncClient，在登录后持有 Cookie，供后续 RPC 使用。可选固定出站 HTTP(S) 代理。"""

    def __init__(self, proxy_url: Optional[str] = None, platform_user_id: Optional[int] = None) -> None:
        self._proxy_url = normalize_proxy_url(proxy_url)
        self._platform_user_id = platform_user_id
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    async def client(self) -> httpx.AsyncClient:
        async with self._lock:
            if self._client is None:
                kw: dict = {
                    "timeout": 30.0,
                    "follow_redirects": True,
                    "verify": _outbound_verify_ca_bundle() if settings.outbound_tls_verify else False,
                }
                if self._proxy_url:
                    kw["proxy"] = self._proxy_url
                if settings.request_log_enabled and (settings.request_log_outbound_hosts or "").strip():
                    uid = self._platform_user_id

                    async def _response_log_hook(response: httpx.Response) -> None:
                        await httpx_outbound_response_log_hook(response, platform_user_id=uid)

                    kw["event_hooks"] = {"response": [_response_log_hook]}
                self._client = httpx.AsyncClient(**kw)
            return self._client

    async def reset(self) -> None:
        async with self._lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    async def close(self) -> None:
        await self.reset()
