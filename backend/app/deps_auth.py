from typing import Optional

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import User
from app.services.credits_service import subscription_expired
from app.settings import settings

security = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="未登录或缺少令牌")
    try:
        payload = jwt.decode(creds.credentials, settings.jwt_secret, algorithms=["HS256"])
        user_id = int(payload["sub"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="令牌无效或已过期")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    if getattr(user, "is_disabled", False):
        raise HTTPException(status_code=403, detail="账号已禁用")
    return user


async def require_active_subscription(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    """交易/运行等能力需在订阅有效期内；兑换积分接口仅用 get_current_user。"""
    await db.refresh(user, attribute_names=["points_balance", "subscription_end_at"])
    if subscription_expired(user):
        raise HTTPException(
            status_code=403,
            detail="尚未开通使用时长或订阅已过期，请在「积分与时长」中兑换或联系管理员充值积分",
        )
    return user
