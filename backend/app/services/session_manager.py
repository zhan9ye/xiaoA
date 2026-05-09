from __future__ import annotations

import asyncio
import contextvars
import os
from typing import List, Optional, Union
from urllib.parse import urlencode, urlparse

import certifi
import httpx
from httpx import AsyncBaseTransport

from app.middleware_request_log import (
    httpx_outbound_response_log_hook,
    log_httpx_outbound_request_error_sync,
)
from app.settings import settings

# 多代理时：当前请求应走哪条出站 URL（httpx 0.28 不支持 post(..., proxy=)，用 transport 分发）
_multi_proxy_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_multi_proxy_ctx", default=None
)
_multi_proxy_label_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_multi_proxy_label_ctx", default=None
)
_multi_proxy_ace_dbg_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_multi_proxy_ace_dbg_ctx", default=None
)


def _outbound_verify_ca_bundle() -> str:
    """出站 HTTPS 校验使用的 CA 包路径。优先 SSL_CERT_FILE / REQUESTS_CA_BUNDLE，否则用 certifi 内置包，避免仅依赖系统 /usr/lib/ssl。"""
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        p = (os.environ.get(key) or "").strip()
        if p and os.path.isfile(p):
            return p
    return certifi.where()


def _normalize_proxy_label(proxy_label: Optional[str]) -> Optional[str]:
    t = (proxy_label or "").strip()
    return t or None


def normalize_proxy_url(proxy_url: Optional[str]) -> Optional[str]:
    """无 scheme 的 `ip:port` / `user:pass@host:port` 补全为 http://，供 httpx 与 urlparse 一致识别。"""
    t = (proxy_url or "").strip() or None
    if not t:
        return None
    if "://" not in t:
        return "http://" + t.lstrip("/")
    return t


class _MultiProxyDispatchTransport(AsyncBaseTransport):
    """按 ContextVar 将请求交给对应 proxy 的 AsyncHTTPTransport（httpx 0.28+）。"""

    def __init__(self, *, verify: Union[bool, str]) -> None:
        self._verify = verify
        self._delegates: dict[str, httpx.AsyncHTTPTransport] = {}

    def _sub(self, proxy_url: str) -> httpx.AsyncHTTPTransport:
        if proxy_url not in self._delegates:
            self._delegates[proxy_url] = httpx.AsyncHTTPTransport(
                proxy=proxy_url,
                verify=self._verify,
                trust_env=False,
            )
        return self._delegates[proxy_url]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        pu = _multi_proxy_ctx.get()
        if not pu:
            raise RuntimeError("internal: multi-proxy dispatch without context url")
        return await self._sub(pu).handle_async_request(request)

    async def aclose(self) -> None:
        for d in self._delegates.values():
            await d.aclose()
        self._delegates.clear()


class _PerRequestProxyClient:
    """
    多出口轮询：在每次 post 前设置 ContextVar，由自定义 Transport 选择实际代理。
    仅实现当前 RPC 用到的 post / cookies。
    """

    def __init__(
        self,
        inner: httpx.AsyncClient,
        proxies: List[str],
        labels: List[Optional[str]],
        *,
        platform_user_id: Optional[int] = None,
    ) -> None:
        self._inner = inner
        self._proxies = proxies
        self._labels = labels if labels else [None] * len(proxies)
        self._platform_user_id = platform_user_id
        self._i = 0
        self._ace_proxy_idx = 0
        self._ace_count_in_group = 0
        self._ace_group_idx = 0
        self._ace_seq = 0
        self._lock = asyncio.Lock()

    @property
    def cookies(self):
        return self._inner.cookies

    @staticmethod
    def _extract_url(args, kwargs) -> str:
        if args and isinstance(args[0], str):
            return args[0]
        u = kwargs.get("url")
        return u if isinstance(u, str) else ""

    @staticmethod
    def _path_only(url: str) -> str:
        if not url:
            return ""
        try:
            return urlparse(url).path or ""
        except Exception:
            return ""

    def _is_ace_sell_request(self, args, kwargs) -> bool:
        # 仅把 ACE_Sell / ACE_Sell_Son 纳入「每 HOT_WINDOW_CONCURRENCY 次切一次代理」规则。
        url = self._extract_url(args, kwargs)
        p = self._path_only(url).lower()
        if p.endswith("/rpc/ace_sell") or p.endswith("/rpc/ace_sell_son"):
            return True
        return False

    @staticmethod
    def _serialize_post_data(data) -> str:
        if data is None:
            return ""
        if isinstance(data, dict):
            try:
                return urlencode(data, doseq=True)
            except Exception:
                return str(data)
        if isinstance(data, (bytes, bytearray)):
            try:
                return data.decode("utf-8", errors="replace")
            except Exception:
                return str(data)
        return str(data)

    async def post(self, *args, **kwargs):
        if not self._proxies:
            return await self._inner.post(*args, **kwargs)
        async with self._lock:
            ace_dbg: Optional[str] = None
            if self._is_ace_sell_request(args, kwargs):
                idx = self._ace_proxy_idx % len(self._proxies)
                group_size = max(1, int(settings.hot_window_concurrency or 1))
                group_pos = self._ace_count_in_group + 1
                self._ace_seq += 1
                ace_dbg = (
                    f"ace_seq={self._ace_seq}"
                    f",group_idx={self._ace_group_idx}"
                    f",group_pos={group_pos}/{group_size}"
                )
                self._ace_count_in_group += 1
                if self._ace_count_in_group >= group_size:
                    self._ace_count_in_group = 0
                    self._ace_proxy_idx = (self._ace_proxy_idx + 1) % len(self._proxies)
                    self._ace_group_idx += 1
            else:
                idx = self._i % len(self._proxies)
                self._i += 1
            prox = self._proxies[idx]
            lab = self._labels[idx] if idx < len(self._labels) else None
        token = _multi_proxy_ctx.set(prox)
        label_token = _multi_proxy_label_ctx.set(lab)
        ace_dbg_token = _multi_proxy_ace_dbg_ctx.set(ace_dbg)
        url_str = self._extract_url(args, kwargs)
        req_body_preview = self._serialize_post_data(kwargs.get("data"))
        try:
            return await self._inner.post(*args, **kwargs)
        except httpx.RequestError as e:
            log_httpx_outbound_request_error_sync(
                method="POST",
                url=url_str,
                req_body=req_body_preview,
                err=(str(e) or repr(e)),
                platform_user_id=self._platform_user_id,
                proxy_label=lab,
                uses_outbound_proxy=True,
                proxy_debug=ace_dbg,
            )
            raise
        finally:
            _multi_proxy_ace_dbg_ctx.reset(ace_dbg_token)
            _multi_proxy_label_ctx.reset(label_token)
            _multi_proxy_ctx.reset(token)


class SessionManager:
    """复用 httpx.AsyncClient，在登录后持有 Cookie，供后续 RPC 使用。支持单代理或多代理（每请求轮询出口）。"""

    def __init__(
        self,
        platform_user_id: Optional[int] = None,
        *,
        proxy_urls: Optional[List[str]] = None,
        proxy_labels: Optional[List[Optional[str]]] = None,
    ) -> None:
        self._platform_user_id = platform_user_id
        urls_in: List[str] = []
        labels_in: List[Optional[str]] = []
        for i, u in enumerate(proxy_urls or []):
            nu = normalize_proxy_url(u)
            if nu:
                urls_in.append(nu)
                lab = proxy_labels[i] if proxy_labels and i < len(proxy_labels) else None
                labels_in.append(_normalize_proxy_label(lab))

        self._proxy_urls = urls_in
        self._proxy_labels = labels_in if labels_in else [None] * len(urls_in)
        self._client: Optional[httpx.AsyncClient] = None
        self._facade: Optional[_PerRequestProxyClient] = None
        self._lock = asyncio.Lock()
        self._use_multi_dispatch = len(self._proxy_urls) >= 2

    def uses_multi_proxy_dispatch(self) -> bool:
        return self._use_multi_dispatch

    @property
    def platform_user_id(self) -> Optional[int]:
        return self._platform_user_id

    def uses_outbound_proxy(self) -> bool:
        return bool(self._proxy_urls)

    def outbound_proxy_log_label(self) -> Optional[str]:
        """直连或单条固定代理时用于文件日志；多代理轮询由 _PerRequestProxyClient 侧记录。"""
        if len(self._proxy_urls) == 1:
            lab = self._proxy_labels[0] if self._proxy_labels else None
            return (lab or "").strip() or None
        return None

    async def client(self) -> Union[httpx.AsyncClient, _PerRequestProxyClient]:
        async with self._lock:
            if self._client is None:
                verify_arg: Union[bool, str] = (
                    _outbound_verify_ca_bundle() if settings.outbound_tls_verify else False
                )
                kw: dict = {
                    "timeout": settings.rpc_timeout_seconds or 30.0,
                    "follow_redirects": True,
                    "verify": verify_arg,
                    # 忽略 HTTP(S)_PROXY 等环境变量，出站仅走本应用配置的代理（避免「关了代理仍走系统代理」）
                    "trust_env": False,
                }
                if self._use_multi_dispatch:
                    kw["transport"] = _MultiProxyDispatchTransport(verify=verify_arg)
                elif len(self._proxy_urls) == 1:
                    kw["proxy"] = self._proxy_urls[0]

                from app.middleware_request_log import http_request_log_file_ok

                if (
                    settings.request_log_enabled
                    and (settings.request_log_outbound_hosts or "").strip()
                    and http_request_log_file_ok()
                ):
                    uid = self._platform_user_id

                    async def _response_log_hook(response: httpx.Response) -> None:
                        if self._use_multi_dispatch:
                            proxy_label = _multi_proxy_label_ctx.get()
                            ace_debug = _multi_proxy_ace_dbg_ctx.get()
                        elif len(self._proxy_labels) == 1:
                            proxy_label = self._proxy_labels[0]
                            ace_debug = None
                        else:
                            proxy_label = None
                            ace_debug = None
                        await httpx_outbound_response_log_hook(
                            response,
                            platform_user_id=uid,
                            proxy_label=proxy_label,
                            uses_outbound_proxy=bool(self._proxy_urls),
                            proxy_debug=ace_debug,
                        )

                    kw["event_hooks"] = {"response": [_response_log_hook]}
                self._client = httpx.AsyncClient(**kw)
                if self._use_multi_dispatch:
                    self._facade = _PerRequestProxyClient(
                        self._client,
                        self._proxy_urls,
                        self._proxy_labels,
                        platform_user_id=self._platform_user_id,
                    )
                else:
                    self._facade = None
            if self._facade is not None:
                return self._facade
            assert self._client is not None
            return self._client

    async def reset(self) -> None:
        async with self._lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None
                self._facade = None

    async def close(self) -> None:
        await self.reset()
