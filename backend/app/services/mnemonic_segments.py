"""助记词配置：12 段逗号分隔的 4 位数字，与 RPC 字段 mnemonicid1 / mnemonicstr1 对应。"""

from typing import Optional


def derive_mnemonic_str1(mnemonic_csv: str, mnemonic_id1: str) -> Optional[str]:
    """
    从「助记词/备注」字符串中取第 mnemonic_id1 段（1～12），作为 mnemonicstr1。
    例如 mnemonic_csv=\"1148,1015,...\" 且 mnemonic_id1=\"1\" → \"1148\"。
    """
    raw = (mnemonic_csv or "").strip()
    if not raw:
        return None
    try:
        idx = int(str(mnemonic_id1).strip())
    except ValueError:
        return None
    if idx < 1 or idx > 12:
        return None
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) < idx:
        return None
    seg = parts[idx - 1]
    return seg if seg else None
