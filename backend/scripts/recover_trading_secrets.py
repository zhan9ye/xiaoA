#!/usr/bin/env python3
"""
从备份库或 SQLite 文件残留中恢复 trading_configs.key_token_enc / mnemonic_enc。

用法（在 backend 目录、已配置 .env 与 JWT_SECRET 时）：

  # 1. 看谁的数据被清空了
  .venv/bin/python scripts/recover_trading_secrets.py list-empty

  # 2. 从备份库合并（推荐；先 dry-run）
  .venv/bin/python scripts/recover_trading_secrets.py merge-backup --backup /path/to/app.db.bak --dry-run
  .venv/bin/python scripts/recover_trading_secrets.py merge-backup --backup /path/to/app.db.bak

  # 3. 扫描目录里所有 .db，看哪份还有密钥/助记词
  .venv/bin/python scripts/recover_trading_secrets.py scan-dbs /opt/xiaoA/backend/data /var/backups

  # 4. 从当前库文件二进制里挖历史 Fernet 密文（无备份时的最后手段）
  .venv/bin/python scripts/recover_trading_secrets.py carve --near-user --output /tmp/recovered_secrets.json
  .venv/bin/python scripts/recover_trading_secrets.py apply-carved --input /tmp/recovered_secrets.json --dry-run

恢复前请先停服务并备份当前库：
  cp data/app.db data/app.db.before-recover.$(date +%F-%H%M)
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# 允许从 backend/ 运行
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.settings import settings  # noqa: E402
from app.trading_crypto import decrypt_trading_field  # noqa: E402

_FERNET_RE = re.compile(rb"gAAAA[A-Za-z0-9_\-]{40,}")


def _default_db_path() -> Path:
    raw = settings.database_url.replace("sqlite+aiosqlite:///", "", 1)
    p = Path(raw)
    if not p.is_absolute():
        p = (_BACKEND / p).resolve()
    return p


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _classify_plaintext(s: str) -> str:
    t = (s or "").strip()
    if not t:
        return "empty"
    parts = [p.strip() for p in re.split(r"[,，]", t) if p.strip()]
    if len(parts) >= 12:
        return "mnemonic"
    if re.fullmatch(r"[A-Z2-7]{16,32}", t.replace(" ", ""), re.I):
        return "key_token"
    if "," in t and all(re.fullmatch(r"[\dA-Za-z\u4e00-\u9fff]{1,4}", p or "") for p in parts[:12]):
        return "mnemonic"
    if len(t) >= 6 and any(c.isalpha() for c in t) and any(c.isdigit() for c in t):
        return "password"
    return "unknown"


def cmd_list_empty(db_path: Path) -> int:
    conn = _connect(db_path)
    rows = conn.execute(
        """
        SELECT user_id, slot, username,
               length(password_enc) AS pw_len,
               length(key_token_enc) AS key_len,
               length(mnemonic_enc) AS mn_len
        FROM trading_configs
        WHERE trim(coalesce(username, '')) != ''
        ORDER BY user_id, slot
        """
    ).fetchall()
    conn.close()
    empty_key = empty_mn = total = 0
    print(f"数据库: {db_path}\n")
    print(f"{'uid':>6} {'slot':>4} {'username':<20} {'pw':>4} {'key':>4} {'mn':>4}")
    print("-" * 50)
    for r in rows:
        total += 1
        kl = int(r["key_len"] or 0)
        ml = int(r["mn_len"] or 0)
        if kl == 0:
            empty_key += 1
        if ml == 0:
            empty_mn += 1
        print(
            f"{int(r['user_id']):>6} {int(r['slot']):>4} "
            f"{str(r['username'] or '')[:20]:<20} "
            f"{int(r['pw_len'] or 0):>4} {kl:>4} {ml:>4}"
        )
    print("-" * 50)
    print(f"共 {total} 条配置；缺 Google 密钥 {empty_key} 条；缺助记词 {empty_mn} 条")
    return 0


def cmd_scan_dbs(dirs: List[Path]) -> int:
    seen: Set[Path] = set()
    candidates: List[Path] = []
    for d in dirs:
        if not d.exists():
            print(f"跳过不存在的目录: {d}")
            continue
        for p in d.rglob("*"):
            if not p.is_file():
                continue
            low = p.name.lower()
            if not (low.endswith(".db") or low.endswith(".db-wal") or ".db." in low or low.endswith(".sqlite")):
                continue
            rp = p.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            candidates.append(rp)

    if not candidates:
        print("未找到任何 .db 文件")
        return 1

    print(f"{'文件':<60} {'rows':>5} {'has_key':>8} {'has_mn':>8} {'mtime'}")
    print("-" * 100)
    for p in sorted(candidates, key=lambda x: x.stat().st_mtime, reverse=True):
        if p.suffix == "-wal" or "-wal" in p.name:
            continue
        try:
            conn = _connect(p)
            row = conn.execute(
                """
                SELECT COUNT(*) AS n,
                       SUM(CASE WHEN length(key_token_enc) > 0 THEN 1 ELSE 0 END) AS hk,
                       SUM(CASE WHEN length(mnemonic_enc) > 0 THEN 1 ELSE 0 END) AS hm
                FROM trading_configs
                """
            ).fetchone()
            conn.close()
            n = int(row["n"] or 0)
            hk = int(row["hk"] or 0)
            hm = int(row["hm"] or 0)
            if n == 0:
                continue
            mtime = p.stat().st_mtime
            from datetime import datetime

            ts = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            flag = " <-- 可恢复" if hk > 0 or hm > 0 else ""
            print(f"{str(p):<60} {n:>5} {hk:>8} {hm:>8} {ts}{flag}")
        except Exception as ex:
            print(f"{str(p):<60}  (无法打开: {ex})")
    return 0


@dataclass
class MergeStats:
    key_restored: int = 0
    mn_restored: int = 0
    skipped: int = 0


def cmd_merge_backup(db_path: Path, backup_path: Path, *, dry_run: bool) -> int:
    if not backup_path.is_file():
        print(f"备份文件不存在: {backup_path}")
        return 1

    cur = _connect(db_path)
    bak = _connect(backup_path)
    try:
        cur_rows = {
            (int(r["user_id"]), int(r["slot"])): dict(r)
            for r in cur.execute(
                "SELECT user_id, slot, username, key_token_enc, mnemonic_enc FROM trading_configs"
            ).fetchall()
        }
        bak_rows = {
            (int(r["user_id"]), int(r["slot"])): dict(r)
            for r in bak.execute(
                "SELECT user_id, slot, username, key_token_enc, mnemonic_enc FROM trading_configs"
            ).fetchall()
        }
    finally:
        bak.close()

    stats = MergeStats()
    updates: List[Tuple[str, str, int, int, str]] = []

    for key, cur_r in cur_rows.items():
        bak_r = bak_rows.get(key)
        if not bak_r:
            stats.skipped += 1
            continue
        uid, slot = key
        uname = str(cur_r.get("username") or bak_r.get("username") or "")
        new_key = str(cur_r.get("key_token_enc") or "")
        new_mn = str(cur_r.get("mnemonic_enc") or "")
        old_key = str(bak_r.get("key_token_enc") or "")
        old_mn = str(bak_r.get("mnemonic_enc") or "")

        patch_key = (not new_key.strip()) and bool(old_key.strip())
        patch_mn = (not new_mn.strip()) and bool(old_mn.strip())
        if not patch_key and not patch_mn:
            stats.skipped += 1
            continue

        if patch_key:
            try:
                plain = decrypt_trading_field(old_key)
                print(f"  uid={uid} slot={slot} {uname}: 恢复 Google 密钥 ({len(plain)} 字符)")
            except ValueError as ex:
                print(f"  uid={uid} slot={slot} {uname}: 备份密钥解密失败 — {ex}")
                patch_key = False
        if patch_mn:
            try:
                plain = decrypt_trading_field(old_mn)
                segs = len([p for p in plain.split(",") if p.strip()])
                print(f"  uid={uid} slot={slot} {uname}: 恢复助记词 ({segs} 段)")
            except ValueError as ex:
                print(f"  uid={uid} slot={slot} {uname}: 备份助记词解密失败 — {ex}")
                patch_mn = False

        if not patch_key and not patch_mn:
            stats.skipped += 1
            continue

        fk = old_key if patch_key else new_key
        fm = old_mn if patch_mn else new_mn
        updates.append((fk, fm, uid, slot, uname))
        if patch_key:
            stats.key_restored += 1
        if patch_mn:
            stats.mn_restored += 1

    print(f"\n计划恢复: Google 密钥 {stats.key_restored} 条, 助记词 {stats.mn_restored} 条 (跳过 {stats.skipped})")

    if dry_run:
        print("\n[dry-run] 未写入数据库。确认无误后去掉 --dry-run 再执行。")
        cur.close()
        return 0

    if not updates:
        print("没有可恢复的条目。")
        cur.close()
        return 0

    stamp = db_path.with_suffix(db_path.suffix + ".pre-merge.bak")
    shutil.copy2(db_path, stamp)
    print(f"\n已备份当前库到: {stamp}")

    try:
        for fk, fm, uid, slot, _ in updates:
            cur.execute(
                """
                UPDATE trading_configs
                SET key_token_enc = ?, mnemonic_enc = ?
                WHERE user_id = ? AND slot = ?
                """,
                (fk, fm, uid, slot),
            )
        cur.commit()
    finally:
        cur.close()

    print("恢复完成。请 restart 后端并抽查 list-empty。")
    return 0


def _extract_fernet_tokens(blob: bytes) -> List[bytes]:
    found: List[bytes] = []
    seen: Set[bytes] = set()
    for m in _FERNET_RE.finditer(blob):
        tok = m.group(0)
        if tok in seen:
            continue
        seen.add(tok)
        found.append(tok)
    return found


def cmd_carve(db_path: Path, output: Path, *, near_user: bool) -> int:
    blob = db_path.read_bytes()
    tokens = _extract_fernet_tokens(blob)
    print(f"在 {db_path} 中发现 {len(tokens)} 个 Fernet 密文片段")

    conn = _connect(db_path)
    users = conn.execute(
        "SELECT user_id, slot, username FROM trading_configs WHERE trim(username) != ''"
    ).fetchall()
    conn.close()

    recovered: List[Dict[str, Any]] = []
    by_plain: Dict[str, Dict[str, Any]] = {}

    for tok_b in tokens:
        tok = tok_b.decode("ascii", errors="ignore")
        try:
            plain = decrypt_trading_field(tok)
        except ValueError:
            continue
        if not plain.strip():
            continue
        kind = _classify_plaintext(plain)
        entry = {
            "type": kind,
            "plaintext_preview": plain[:8] + "…" if len(plain) > 12 else plain,
            "plaintext_len": len(plain),
            "enc_token": tok,
        }
        if plain not in by_plain:
            by_plain[plain] = entry
            recovered.append(entry)

    if near_user:
        user_hits: List[Dict[str, Any]] = []
        for u in users:
            uname = str(u["username"] or "").encode("utf-8")
            if not uname:
                continue
            window = 8192
            for pos in range(len(blob)):
                idx = blob.find(uname, pos)
                if idx < 0:
                    break
                pos = idx + 1
                lo = max(0, idx - window)
                hi = min(len(blob), idx + window)
                chunk = blob[lo:hi]
                local_tokens = _extract_fernet_tokens(chunk)
                for tok_b in local_tokens:
                    tok = tok_b.decode("ascii", errors="ignore")
                    try:
                        plain = decrypt_trading_field(tok)
                    except ValueError:
                        continue
                    if not plain.strip():
                        continue
                    user_hits.append(
                        {
                            "user_id": int(u["user_id"]),
                            "slot": int(u["slot"]),
                            "username": str(u["username"]),
                            "type": _classify_plaintext(plain),
                            "plaintext_len": len(plain),
                            "enc_token": tok,
                        }
                    )
        output.write_text(
            json.dumps({"global": recovered, "near_username": user_hits}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"全局可解密 {len(recovered)} 条；用户名邻近匹配 {len(user_hits)} 条 → {output}")
    else:
        output.write_text(json.dumps(recovered, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入 {output}")

    mn = sum(1 for x in recovered if x["type"] == "mnemonic")
    key = sum(1 for x in recovered if x["type"] == "key_token")
    print(f"其中助记词候选 {mn} 条，Google 密钥候选 {key} 条")
    print("说明：carve 为最后手段；优先使用 merge-backup 或阿里云云盘快照里的旧 app.db。")
    return 0


def cmd_apply_carved(db_path: Path, carved_json: Path, *, dry_run: bool) -> int:
    data = json.loads(carved_json.read_text(encoding="utf-8"))
    hits: List[Dict[str, Any]] = list(data.get("near_username") or [])
    if not hits:
        print(f"{carved_json} 中没有 near_username 数据；请用 carve --near-user 重新生成")
        return 1

    conn = _connect(db_path)
    stats = {"key": 0, "mn": 0, "skip": 0}
    try:
        for h in hits:
            kind = str(h.get("type") or "")
            if kind not in ("key_token", "mnemonic"):
                stats["skip"] += 1
                continue
            uid = int(h["user_id"])
            slot = int(h["slot"])
            enc = str(h.get("enc_token") or "")
            if not enc:
                stats["skip"] += 1
                continue
            row = conn.execute(
                "SELECT username, key_token_enc, mnemonic_enc FROM trading_configs WHERE user_id=? AND slot=?",
                (uid, slot),
            ).fetchone()
            if row is None:
                stats["skip"] += 1
                continue
            uname = str(row["username"] or "")
            cur_key = str(row["key_token_enc"] or "")
            cur_mn = str(row["mnemonic_enc"] or "")
            if kind == "key_token":
                if cur_key.strip():
                    stats["skip"] += 1
                    continue
                print(f"  uid={uid} slot={slot} {uname}: 写入 Google 密钥")
                if not dry_run:
                    conn.execute(
                        "UPDATE trading_configs SET key_token_enc=? WHERE user_id=? AND slot=?",
                        (enc, uid, slot),
                    )
                stats["key"] += 1
            elif kind == "mnemonic":
                if cur_mn.strip():
                    stats["skip"] += 1
                    continue
                print(f"  uid={uid} slot={slot} {uname}: 写入助记词")
                if not dry_run:
                    conn.execute(
                        "UPDATE trading_configs SET mnemonic_enc=? WHERE user_id=? AND slot=?",
                        (enc, uid, slot),
                    )
                stats["mn"] += 1
        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    print(
        f"\n计划写入: Google 密钥 {stats['key']} 条, 助记词 {stats['mn']} 条 "
        f"(跳过 {stats['skip']})"
    )
    if dry_run:
        print("[dry-run] 未写入。确认后去掉 --dry-run")
    else:
        print("完成。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="恢复 trading_configs 中的 Google 密钥与助记词")
    parser.add_argument("--db", type=Path, default=None, help="当前库路径（默认读 DATABASE_URL）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-empty", help="列出缺 key/mnemonic 的配置")

    p_merge = sub.add_parser("merge-backup", help="从备份库合并空字段")
    p_merge.add_argument("--backup", type=Path, required=True, help="旧 app.db 路径")
    p_merge.add_argument("--dry-run", action="store_true", help="只预览不写入")

    p_scan = sub.add_parser("scan-dbs", help="扫描目录下所有数据库文件")
    p_scan.add_argument("dirs", type=Path, nargs="+", help="要扫描的目录")

    p_carve = sub.add_parser("carve", help="从库文件二进制挖掘历史密文")
    p_carve.add_argument("--output", type=Path, default=Path("/tmp/recovered_secrets.json"))
    p_carve.add_argument("--near-user", action="store_true", help="按用户名邻近窗口关联")

    p_apply = sub.add_parser("apply-carved", help="将 carve --near-user 结果写回数据库")
    p_apply.add_argument("--input", type=Path, required=True, help="recovered_secrets.json")
    p_apply.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    db_path = (args.db or _default_db_path()).resolve()
    if not db_path.is_file() and args.cmd != "scan-dbs":
        print(f"当前库不存在: {db_path}")
        return 1

    if args.cmd == "list-empty":
        return cmd_list_empty(db_path)
    if args.cmd == "merge-backup":
        return cmd_merge_backup(db_path, args.backup.resolve(), dry_run=bool(args.dry_run))
    if args.cmd == "scan-dbs":
        return cmd_scan_dbs([p.resolve() for p in args.dirs])
    if args.cmd == "carve":
        return cmd_carve(db_path, args.output.resolve(), near_user=bool(args.near_user))
    if args.cmd == "apply-carved":
        return cmd_apply_carved(db_path, args.input.resolve(), dry_run=bool(args.dry_run))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
