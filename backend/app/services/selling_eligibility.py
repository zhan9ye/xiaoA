"""
运行参数与 ACE_Sell_Son 售卖规则（供后续批量/定时任务复用）。

- 时间段：仅考虑「创建日」落在 [run_period_start, run_period_end] 内的子账号（字段名见下方启发式）。
- 数量起始限额：仅当 AceAmount **大于** quantity_start_limit 时才可参与卖出。
"""

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.schemas import AppConfigIn


def resolve_son_id(row: Dict[str, Any]) -> Optional[str]:
    for k in ("SonId", "sonId", "Id", "ID", "SubAccountId", "SubId"):
        v = row.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return None


def resolve_subaccount_display_name(row: Dict[str, Any]) -> str:
    """子账号展示名（与前端「子账户名」MemberNo 等一致）；无则返回空串。"""
    for k in (
        "MemberNo",
        "memberNo",
        "MemberNO",
        "SonName",
        "sonName",
        "UserName",
        "username",
        "NickName",
        "Name",
        "AccountName",
    ):
        v = row.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def ace_amount_string_for_rpc(row: Dict[str, Any]) -> str:
    """子账号 AceAmount 格式化为 RPC 字符串（amount/count）。"""
    v = _parse_ace_amount(row)
    if v is None:
        return ""
    if v == int(v):
        return str(int(v))
    return str(v)


def _parse_ace_amount(row: Dict[str, Any]) -> Optional[float]:
    for k in ("AceAmount", "ACEAmount", "aceAmount", "Ace_Count"):
        v = row.get(k)
        if v is None or v == "":
            continue
        try:
            return float(str(v).replace(",", "").strip())
        except ValueError:
            continue
    return None


def _coerce_date_string_to_yyyy_mm_dd(s: str) -> Optional[str]:
    """
    将常见日期字符串规范为 YYYY-MM-DD。
    支持：YYYY-MM-DD、YYYY/MM/D、YYYY/MM/DD（可带时间）；接口常返回 2023/9/11 13:59:16 这类斜杠格式。
    """
    s = str(s).strip()
    if not s:
        return None
    if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
        return s[:10]
    m = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            datetime(y, mo, d)
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            return None
    return None


def _parse_created_day_yyyy_mm_dd(row: Dict[str, Any]) -> Optional[str]:
    """从子账号行解析创建日 YYYY-MM-DD；无法解析则返回 None。"""
    for k in (
        "CreateTime",
        "CreateDate",
        "CreatedAt",
        "AddTime",
        "RegisterTime",
        "RegisterDate",
        "CreateTimeStr",
        "Time",
        "time",
    ):
        v = row.get(k)
        if v is None or v == "":
            continue
        if isinstance(v, (int, float)):
            try:
                return datetime.fromtimestamp(v).strftime("%Y-%m-%d")
            except (OSError, ValueError, OverflowError):
                continue
        day = _coerce_date_string_to_yyyy_mm_dd(str(v))
        if day:
            return day
    return None


def subaccount_eligible_for_ace_sell(row: Dict[str, Any], cfg: AppConfigIn) -> Tuple[bool, str]:
    """
    是否满足当前配置下的售卖条件（不发起 HTTP，仅规则判断）。
    若配置了时段但无法从行内解析创建日，返回 (False, …) 以避免误卖。
    """
    ace = _parse_ace_amount(row)
    if ace is None:
        return False, "缺少或可解析的 AceAmount"

    limit = float(cfg.quantity_start_limit)
    if ace <= limit:
        return False, f"AceAmount={ace} 未大于起始限额 {limit}"

    rs = (cfg.run_period_start or "").strip()
    re = (cfg.run_period_end or "").strip()
    if not rs and not re:
        return True, "ok"

    day = _parse_created_day_yyyy_mm_dd(row)
    if day is None:
        return False, "已配置售卖时段但子账号数据中无可用创建日期字段"

    if rs and day < rs:
        return False, f"创建日 {day} 早于时段开始 {rs}"
    if re and day > re:
        return False, f"创建日 {day} 晚于时段结束 {re}"
    return True, "ok"


def _normalize_amount_token(s: str) -> str:
    s = str(s).replace(",", "").strip()
    if not s:
        return ""
    try:
        f = float(s)
        if f == int(f):
            return str(int(f))
        return str(f)
    except ValueError:
        return s


def parse_listing_amounts_map(json_str: str) -> Dict[str, str]:
    """解析 listing_amounts_json → sonId → 数量字符串（不含逗号）。"""
    raw = (json_str or "").strip() or "{}"
    try:
        m = json.loads(raw)
        if not isinstance(m, dict):
            return {}
        out: Dict[str, str] = {}
        for k, v in m.items():
            if v is None:
                continue
            sk = str(k).strip()
            sv = str(v).replace(",", "").strip()
            if sk and sv:
                out[sk] = sv
        return out
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def listing_amounts_for_api(cfg: AppConfigIn) -> Dict[str, str]:
    """供 GET /api/config 返回的挂售覆盖表。"""
    return dict(parse_listing_amounts_map(cfg.listing_amounts_json))


def effective_listing_amount_str(cfg: AppConfigIn, son_id: str, full_amount_str: str) -> str:
    """
    挂售数量：若配置中有该 sonId 的覆盖则用之，否则为全部股数（full_amount_str）。
    full_amount_str 须为 RPC 侧数量字符串（与 ace_amount_string_for_rpc 一致）。
    """
    if not full_amount_str:
        return ""
    m = parse_listing_amounts_map(cfg.listing_amounts_json)
    sid = str(son_id).strip()
    v = m.get(sid)
    if v is None or v == "":
        return _normalize_amount_token(full_amount_str)
    return _normalize_amount_token(v)


def enrich_subaccounts_with_listing_qty(items: List[Dict[str, Any]], cfg: AppConfigIn) -> List[Dict[str, Any]]:
    """
    为每条子账号浅拷贝并写入 ListingQty：listing_amounts_json 有该 sonId 用库中值，否则为全部股数。
    仅用于 API 响应；内存中的 subaccounts_cache 仍保持 RPC 原始行。
    """
    out: List[Dict[str, Any]] = []
    for raw in items:
        row = dict(raw)
        son_id = resolve_son_id(row)
        full = ace_amount_string_for_rpc(row)
        if son_id and full:
            row["ListingQty"] = effective_listing_amount_str(cfg, son_id, full)
        out.append(row)
    return out
