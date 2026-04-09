from datetime import datetime, timedelta, timezone

import jwt

from app.settings import settings

ADMIN_TOKEN_TYP = "admin"


def create_access_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=settings.jwt_expire_hours)
    payload = {"sub": str(user_id), "exp": exp, "iat": now}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> int:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    return int(payload["sub"])


def create_admin_access_token() -> str:
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=settings.admin_jwt_expire_hours)
    payload = {"sub": "admin", "typ": ADMIN_TOKEN_TYP, "exp": exp, "iat": now}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
