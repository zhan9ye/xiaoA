"""积分兑换系统使用时长（按天）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User

# 天数 -> 所需积分
CREDIT_PACKAGES: Dict[int, int] = {
    7: 1000,
    30: 3000,
    90: 8500,
    180: 16000,
    360: 28000,
}

# prev 与 ext=now+days 几乎重合（剩余≈套餐天数）时仍从 prev 叠 days；须小于「短剩余买 1 天」时 prev 与 ext 的常见间隔（约数小时）
_REDEEM_NEAR_EXT_THRESHOLD = timedelta(hours=6)


def packages_public() -> List[Dict[str, int]]:
    return [{"days": d, "points_cost": p} for d, p in sorted(CREDIT_PACKAGES.items())]


def _aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def subscription_expired(user: User) -> bool:
    """None 表示尚未通过积分开通时长；非空且早于等于当前 UTC 视为过期。"""
    end = _aware_utc(user.subscription_end_at)
    if end is None:
        return True
    return end <= datetime.now(timezone.utc)


def subscription_active(user: User) -> bool:
    return not subscription_expired(user)


def compute_redeem_end_at(user: User, days: int) -> datetime:
    """
    不修改 user。与 redeem_days 写入的 subscription_end_at 使用同一套时刻计算（UTC + timedelta）。

    - 未开通或已过期：new = now + days。
    - 仍在期内且原到期晚于 now+days：在「原到期」上叠 days（长周期续费）。
    - 仍在期内但原到期略早于或接近 now+days（差在数小时内）：视为「剩余天数≈所购档位」，在「原到期」上叠 days，
      避免出现只延长几分钟的 bug。
    - 仍在期内且原到期明显早于 now+days（短剩余）：new = now + days（与「当日凌晨买 1 天」等产品预期一致）。
    """
    now = datetime.now(timezone.utc)
    prev = _aware_utc(user.subscription_end_at)
    ext = now + timedelta(days=days)
    if prev is None or prev <= now:
        return ext
    if prev > ext:
        return prev + timedelta(days=days)
    if (ext - prev) <= _REDEEM_NEAR_EXT_THRESHOLD:
        return prev + timedelta(days=days)
    return ext


async def redeem_days(session: AsyncSession, user_id: int, days: int) -> Tuple[User, int]:
    """
    扣积分并延长 subscription_end_at。具体时刻与 compute_redeem_end_at 一致。
    """
    cost = CREDIT_PACKAGES.get(days)
    if cost is None:
        raise ValueError(f"不支持的天数套餐：{days}，可选：{sorted(CREDIT_PACKAGES)}")

    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise ValueError("用户不存在")

    if user.points_balance < cost:
        raise ValueError(f"积分不足：需要 {cost}，当前 {user.points_balance}")

    new_end = compute_redeem_end_at(user, days)

    user.points_balance -= cost
    user.subscription_end_at = new_end
    await session.commit()
    await session.refresh(user)
    return user, cost
