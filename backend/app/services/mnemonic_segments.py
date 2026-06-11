"""助记词配置：12 段逗号分隔，每段最多 4 个字符（数字、英文或中文），与 RPC 字段 mnemonicid1 / mnemonicstr1 对应。"""

import re
import unicodedata
from typing import List, Optional

MNEMONIC_SEGMENT_LEN = 4
MNEMONIC_SEGMENTS = 12
_MNEMONIC_SEGMENT_RE = re.compile(
    rf"^[\dA-Za-z\u4e00-\u9fff]{{{MNEMONIC_SEGMENT_LEN}}}$"
)
_MNEMONIC_LEGACY_SEGMENT_RE = re.compile(r"^[^\s,，]+$")


def normalize_mnemonic_segment(seg: str) -> str:
    """全角数字/字母等归一为半角，便于读写与校验一致。"""
    return unicodedata.normalize("NFKC", (seg or "").strip())


def split_mnemonic_csv(raw: str) -> List[str]:
    """
    解析助记词 CSV。支持英文/中文逗号分隔，以及历史无逗号的 48 位纯数字串。
    不做字符过滤，原样保留各段内容供回显与 RPC 推导。
    """
    s = (raw or "").strip()
    if not s:
        return []
    if "," in s or "，" in s:
        return [normalize_mnemonic_segment(p) for p in re.split(r"[,，]", s)]
    compact = re.sub(r"\s+", "", s)
    if compact.isdigit() and len(compact) == MNEMONIC_SEGMENT_LEN * MNEMONIC_SEGMENTS:
        return [
            compact[i : i + MNEMONIC_SEGMENT_LEN]
            for i in range(0, len(compact), MNEMONIC_SEGMENT_LEN)
        ]
    return [normalize_mnemonic_segment(s)]


def is_valid_mnemonic_segment(seg: str) -> bool:
    """
    校验单段助记词。
    - 新版：恰好 4 个数字/英文/常用汉字；
    - 兼容：纯数字（含全角归一化后 1～4 位）、以及已存库的不超过 4 字符的其它内容。
    """
    s = normalize_mnemonic_segment(seg)
    if not s or len(s) > MNEMONIC_SEGMENT_LEN:
        return False
    if re.search(r"[,，\s]", s):
        return False
    if _MNEMONIC_SEGMENT_RE.fullmatch(s):
        return True
    if s.isdigit():
        return True
    return bool(_MNEMONIC_LEGACY_SEGMENT_RE.fullmatch(s))


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
    parts = split_mnemonic_csv(raw)
    if len(parts) < idx:
        return None
    seg = parts[idx - 1]
    return seg if seg else None
