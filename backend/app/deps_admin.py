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
    """
    仅接受管理员 JWT（payload.typ == admin）。平台用户登录令牌无 typ、sub 为数字用户 id，
    会在 typ 校验处被拒绝，因此所有 Depends(require_admin) 的接口不可能被普通用户 Bearer 调用。
    """
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
