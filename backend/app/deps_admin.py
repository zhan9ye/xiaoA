from typing import Optional

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth_jwt import ADMIN_TOKEN_TYP
from app.settings import settings

security = HTTPBearer(auto_error=False)


def is_admin_auth_configured() -> bool:
    if not settings.admin_username.strip():
        return False
    return bool((settings.admin_password_hash or "").strip() or (settings.admin_password or "").strip())


async def require_admin(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> None:
    if not is_admin_auth_configured():
        raise HTTPException(
            status_code=503,
            detail="未配置管理员（.env：admin_username + admin_password_hash 或 admin_password）",
        )
    if creds is None or not creds.credentials:
        raise HTTPException(status_code=401, detail="请先登录管理后台")
    try:
        payload = jwt.decode(creds.credentials, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="管理员令牌无效或已过期")
    if payload.get("typ") != ADMIN_TOKEN_TYP:
        raise HTTPException(status_code=403, detail="非管理员令牌")
    return None
