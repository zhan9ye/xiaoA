"""
从 Google 验证器「共享密钥」生成当前 6 位 TOTP（RFC 6238，SHA1，30s），
即 RPC 表单中的 gCode；密钥来源为配置 key_token。

时间步进使用 UTC（与 Google Authenticator / RFC 6238 一致）；pyotp 的 ``TOTP.now()``
在部分环境下依赖本地 naive 时间，与 UTC 计数可能不一致，故统一用 ``at(UTC)``。
"""

import base64
import binascii
import datetime
import hashlib
import re
from typing import List, Optional, Set, Tuple

import pyotp

# RFC 4648：标准 Base32 字母表 A-Z + 234567；Base32hex 为 0-9 + A-V（同比特布局，符号不同）
_B32_STD = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_B32_HEX = "0123456789ABCDEFGHIJKLMNOPQRSTUV"


def _base32hex_to_std(secret_upper: str) -> Optional[str]:
    """若密钥为 Base32hex（可含 0-9、A-V），转为 pyotp 需要的标准 Base32 字符串。"""
    out: List[str] = []
    for c in secret_upper.replace(" ", ""):
        if c not in _B32_HEX:
            return None
        out.append(_B32_STD[_B32_HEX.index(c)])
    return "".join(out)


def _substitute_ambiguous_base32_digits(s: str) -> str:
    """
    标准 Base32 仅含 A-Z 与 2-7；站点/截图常把 I/L 显示成 1，把 B 显示成 8 等。
    将非法的 0/1/8/9 替换为常见对应字母后再交给 pyotp。
    """
    out: List[str] = []
    for c in s.upper().replace(" ", ""):
        if c == "0":
            out.append("O")
        elif c == "1":
            out.append("I")
        elif c == "8":
            out.append("B")
        elif c == "9":
            out.append("G")
        else:
            out.append(c)
    return "".join(out)


def _pad_base32(s: str) -> str:
    """RFC 4648 Base32 需长度为 8 的倍数，不足则补 =。"""
    s = s.upper().replace(" ", "")
    pad = (-len(s)) % 8
    return s + ("=" * pad if pad else "")


def _totp_at_utc(secret_b32: str) -> str:
    """与 Google Authenticator 一致：SHA1、6 位、30s，计数基于 UTC 时间。"""
    totp = pyotp.TOTP(secret_b32, digest=hashlib.sha1, digits=6, interval=30)
    return totp.at(datetime.datetime.now(datetime.timezone.utc))


def _iter_totp_secret_candidates(raw: str):
    """生成若干候选密钥字符串，依次尝试 pyotp（先成功且无异常的即采用）。"""
    cleaned = raw.strip().replace(" ", "").replace("\n", "").replace("\t", "")
    if not cleaned:
        return
    u = cleaned.upper()

    # 32 位十六进制（常为会话 Key 或原始密钥字节）：若先当 Base32 文本解码会得到错误密钥，
    # 与 Google 验证器不一致。优先「hex 字节 → 再 Base32 编码」再尝试「整串即 Base32」。
    is_32_hex = len(cleaned) == 32 and re.fullmatch(r"[0-9A-Fa-f]{32}", cleaned) is not None
    if is_32_hex:
        try:
            b32_from_raw = base64.b32encode(bytes.fromhex(cleaned)).decode("ascii").rstrip("=")
            yield b32_from_raw
            yield _pad_base32(b32_from_raw)
        except (ValueError, binascii.Error):
            pass
        yield u
        yield _pad_base32(u)
        return

    # 常见：误输入小写、无 padding
    yield u
    yield _pad_base32(u)
    # 站点有时展示 Base32hex（含 0-9、A-V），需映射到标准 Base32
    b32_from_hex = _base32hex_to_std(u)
    if b32_from_hex:
        yield b32_from_hex
        yield _pad_base32(b32_from_hex)
    # 0/1/8/9 在标准 Base32 中非法，站点常误显示为字母
    fixed_digits = _substitute_ambiguous_base32_digits(cleaned)
    if fixed_digits != u:
        yield fixed_digits
        yield _pad_base32(fixed_digits)


def totp_now_from_secret_ex(secret: str) -> Tuple[Optional[str], str]:
    """
    返回 (6 位码或 None, 失败原因说明)。
    成功时第二项为空串。
    """
    raw = (secret or "").strip()
    if not raw:
        return None, "验证器共享密钥未配置"

    last_err = "无法解析为 TOTP 密钥"
    seen: Set[str] = set()
    for candidate in _iter_totp_secret_candidates(raw):
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            return _totp_at_utc(candidate), ""
        except (ValueError, TypeError, binascii.Error) as e:
            last_err = str(e) or last_err
            continue
    # 提示常见误填
    hint = (
        "请使用 Google 验证器里「手动输入密钥」的共享密钥；"
        "若仍失败，请核对是否含 0～9（部分站点为 Base32hex 格式，程序已自动尝试转换）。"
        "勿填 6 位动态码、勿填登录接口返回的 32 位会话 Key。"
    )
    return None, f"{hint}（解析失败：{last_err[:120]}）"


def totp_now_from_secret(secret: str) -> Optional[str]:
    code, _ = totp_now_from_secret_ex(secret)
    return code
