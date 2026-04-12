"""
登录防爆破：按客户端 IP 统计失败次数，超过阈值后须先通过一次性算术验证码再校验密码。
多 worker 时各进程内存独立，防护为尽力而为。
"""

from __future__ import annotations

import asyncio
import random
import secrets
import time
from typing import Dict, Tuple

from fastapi import Request

from app.settings import settings

_lock = asyncio.Lock()
_failures: Dict[str, int] = {}
# challenge_id -> (答案字符串, 过期 monotonic)
_challenges: Dict[str, Tuple[str, float]] = {}


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:128] or "unknown"
    if request.client and request.client.host:
        return str(request.client.host)[:128]
    return "unknown"


def _captcha_threshold() -> int:
    return max(1, int(getattr(settings, "login_captcha_after_failures", 3) or 3))


def _captcha_ttl() -> float:
    return max(30.0, float(getattr(settings, "login_captcha_ttl_seconds", 300) or 300))


def _now_mono() -> float:
    return time.monotonic()


def _purge_expired_unlocked() -> None:
    t = _now_mono()
    dead = [k for k, (_, exp) in _challenges.items() if exp < t]
    for k in dead:
        del _challenges[k]


async def failure_count(ip: str) -> int:
    async with _lock:
        return int(_failures.get(ip, 0))


async def needs_login_captcha(ip: str) -> bool:
    return await failure_count(ip) >= _captcha_threshold()


async def record_login_failure(ip: str) -> int:
    async with _lock:
        _failures[ip] = int(_failures.get(ip, 0)) + 1
        return _failures[ip]


async def clear_login_failures(ip: str) -> None:
    async with _lock:
        _failures.pop(ip, None)


async def create_login_captcha() -> Tuple[str, str]:
    """返回 (captcha_id, captcha_question)，question 如 \"3 + 5\"，答案为两数之和。"""
    async with _lock:
        _purge_expired_unlocked()
        a, b = random.randint(1, 9), random.randint(1, 9)
        ans = str(a + b)
        cid = secrets.token_urlsafe(16)
        _challenges[cid] = (ans, _now_mono() + _captcha_ttl())
        return cid, f"{a} + {b}"


async def verify_login_captcha(captcha_id: str, answer: str) -> bool:
    """校验并消费 challenge（一次性）。"""
    async with _lock:
        _purge_expired_unlocked()
        row = _challenges.pop((captcha_id or "").strip(), None)
        if row is None:
            return False
        stored, exp = row
        if exp < _now_mono():
            return False
        return (answer or "").strip() == stored
