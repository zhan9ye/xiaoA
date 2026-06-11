"""交易端配置完整性校验（启动 Runner 前）。"""

from __future__ import annotations

from typing import Optional

from app.schemas import AppConfigIn
from app.services.mnemonic_segments import (
    MNEMONIC_SEGMENTS,
    is_valid_mnemonic_segment,
    split_mnemonic_csv,
)


def trading_config_field_prompt(field_label: str) -> str:
    return f"{field_label}填写不正确，请先填写{field_label}，再重试！"


def trading_config_start_block_reason(cfg: Optional[AppConfigIn]) -> Optional[str]:
    """返回阻止启动的提示文案；配置完整时返回 None。"""
    if cfg is None:
        return trading_config_field_prompt("交易端配置")

    if not (cfg.username or "").strip():
        return trading_config_field_prompt("登录账户")

    pw = cfg.password or ""
    if not pw.strip() or pw == " ":
        return trading_config_field_prompt("登录密码")

    if not (cfg.key_token or "").strip():
        return trading_config_field_prompt("Google 共享密钥")

    raw = (cfg.mnemonic or "").strip()
    if not raw:
        return trading_config_field_prompt("助记词")

    parts = split_mnemonic_csv(raw)
    if len(parts) < MNEMONIC_SEGMENTS:
        return trading_config_field_prompt("助记词")
    for seg in parts[:MNEMONIC_SEGMENTS]:
        if not is_valid_mnemonic_segment(seg):
            return trading_config_field_prompt("助记词")
    return None
