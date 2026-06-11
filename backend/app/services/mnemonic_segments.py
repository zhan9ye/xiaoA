"""助记词配置：12 段逗号分隔，每段 4 个字符（数字、英文或中文），与 RPC 字段 mnemonicid1 / mnemonicstr1 对应。"""

import re
from typing import Optional

MNEMONIC_SEGMENT_LEN = 4
MNEMONIC_SEGMENTS = 12
_MNEMONIC_SEGMENT_RE = re.compile(
    rf"^[\dA-Za-z\u4e00-\u9fff]{{{MNEMONIC_SEGMENT_LEN}}}$"
)


def is_valid_mnemonic_segment(seg: str) -> bool:
    """单段须恰好 4 个字符，且为数字、英文字母或常用汉字。"""
    return bool(_MNEMONIC_SEGMENT_RE.fullmatch(seg))


def _mnemonic_segment_index(mnemonic_id1: str) -> Optional[int]:
    """解析接口 mnemonicid1（1～12）；兼容 JSON 数字或 \"10.0\" 等形式。"""
    raw = str(mnemonic_id1).strip()
    if not raw:
        return None
    try:
        idx = int(float(raw))
    except (ValueError, TypeError):
        return None
    if idx < 1 or idx > 12:
        return None
    return idx


def derive_mnemonic_str1(mnemonic_csv: str, mnemonic_id1: str) -> Optional[str]:
    """
    从「助记词/备注」字符串中取第 mnemonic_id1 段（1～12），作为 mnemonicstr1。
    例如 mnemonic_csv=\"1148,love,春天,...\" 且 mnemonic_id1=\"1\" → \"1148\"。
    """
    raw = (mnemonic_csv or "").strip()
    if not raw:
        return None
    idx = _mnemonic_segment_index(mnemonic_id1)
    if idx is None:
        return None
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) < idx:
        return None
    seg = parts[idx - 1]
    return seg if seg else None
