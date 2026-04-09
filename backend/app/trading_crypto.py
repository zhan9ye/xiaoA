"""交易端敏感字段入库加密（依赖 jwt_secret，更换密钥后旧数据将无法解密）。"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.settings import settings


def _fernet() -> Fernet:
    key = hashlib.sha256(settings.jwt_secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_trading_field(plain: str) -> str:
    if plain is None or plain == "":
        return ""
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_trading_field(enc: str) -> str:
    if enc is None or enc == "":
        return ""
    try:
        return _fernet().decrypt(enc.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        raise ValueError("交易配置解密失败，请检查 JWT_SECRET 是否与写入时一致")
