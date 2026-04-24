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


def son_id_form_fields_empty(row: Dict[str, Any]) -> bool:
    """与 RPC 卖主账户时「sonId 无值」一致：仅看 SonId/sonId 是否都未填。"""
    for k in ("SonId", "sonId"):
        v = row.get(k)
        if v is not None and str(v).strip() != "":
            return False
    return True


def listing_amount_key_for_row(row: Dict[str, Any]) -> str:
    """挂售覆盖 JSON 的键：仅主账户行用空字符串，其它行用本行账号 id。"""
    if bool(row.get("__is_main_account")):
        return ""
    return resolve_son_id(row) or ""


def ace_sell_rpc_son_id(row: Dict[str, Any]) -> str:
    """
    POST ACE_Sell_Son 表单里的 sonId：
    - 主账户行（__is_main_account=true）强制空字符串；
    - 其余优先 SonId/sonId，缺失时回退 resolve_son_id（兼容上游仅给 Id 的历史数据）。
    """
    if bool(row.get("__is_main_account")):
        return ""
    for k in ("SonId", "sonId"):
        v = row.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return resolve_son_id(row) or ""


def ace_sell_track_id(row: Dict[str, Any]) -> str:
    """并发去重、已售记录、日志键：主账户固定 __main__，子账户为表单 sonId（与 ace_sell_rpc_son_id 一致）。"""
    s = ace_sell_rpc_son_id(row)
    return "__main__" if not s else s


def is_main_account_row(row: Dict[str, Any]) -> bool:
    """主账户判定：仅看显式标记，避免把缺字段子账号误判为主账户。"""
    return bool(row.get("__is_main_account"))


def ensure_main_account_row(items: List[Dict[str, Any]], main_account_id: str = "") -> List[Dict[str, Any]]:
    """
    确保列表中始终有主账户行，且位于最前。
    约定：主账户默认股数 AceAmount=0（当上游未提供时），用于前端稳定展示。
    """
    rest: List[Dict[str, Any]] = []
    for raw in items:
        row = dict(raw)
        if bool(row.get("__is_main_account")):
            continue
        rest.append(row)
    main_id = str(main_account_id or "").strip()
    main_row: Dict[str, Any] = {
        "SonId": "",
        "FlowNumber": main_id,
        "MemberNo": "主账户",
        "AceAmount": "0",
        "__is_main_account": True,
    }
    return [main_row, *rest]


def parse_main_account_info_json(raw: str) -> Dict[str, Any]:
    try:
        o = json.loads((raw or "").strip() or "{}")
        return o if isinstance(o, dict) else {}
    except Exception:
        return {}


def apply_main_account_info(row: Dict[str, Any], info: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    ace = info.get("ACECount")
    if ace is not None and str(ace).strip() != "":
        out["AceAmount"] = str(ace).strip()
    if info.get("HonorName") is not None:
        out["HonorName"] = info.get("HonorName")
    if info.get("LevelNumber") is not None:
        out["LevelNumber"] = info.get("LevelNumber")
    if info.get("CurrentStockPrice") is not None:
        out["CurrentStockPrice"] = info.get("CurrentStockPrice")
    for k in ("EP", "RP", "SP", "ULP"):
        if info.get(k) is not None:
            out[k] = info.get(k)
    ctime = info.get("CreateTime")
    if ctime is not None and str(ctime).strip():
        out["CreateTime"] = str(ctime).strip()
    return out


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
            if not sv:
                continue
            out[sk] = sv
        return out
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def ensure_main_listing_default_json(json_str: str) -> Tuple[str, bool]:
    """
    确保挂售覆盖中存在主账户默认值："" -> "0"（不卖）。
    返回 (new_json, changed)。
    """
    raw = (json_str or "").strip() or "{}"
    try:
        m = json.loads(raw)
        if not isinstance(m, dict):
            m = {}
    except (json.JSONDecodeError, TypeError, ValueError):
        m = {}
    has_main = ("" in m) and str(m.get("", "")).strip() != ""
    if has_main:
        return json.dumps(m, ensure_ascii=False, separators=(",", ":")), False
    m[""] = "0"
    return json.dumps(m, ensure_ascii=False, separators=(",", ":")), True


def listing_amounts_for_api(cfg: AppConfigIn) -> Dict[str, str]:
    """供 GET /api/config 返回的挂售覆盖表。"""
    return dict(parse_listing_amounts_map(cfg.listing_amounts_json))


def effective_listing_amount_str(cfg: AppConfigIn, son_id: str, full_amount_str: str) -> str:
    """
    挂售数量：若配置中有该 sonId 的覆盖则用之，否则为全部股数（full_amount_str）。
    full_amount_str 须为 RPC 侧数量字符串（与 ace_amount_string_for_rpc 一致）。
    覆盖为「0」表示不参与挂售（返回空，上层跳过 ACE_Sell_Son）。
    """
    if not full_amount_str:
        return ""
    m = parse_listing_amounts_map(cfg.listing_amounts_json)
    sid = str(son_id).strip()
    v = m.get(sid)
    if v is None or v == "":
        # 主账户（sonId 为空）默认不卖；仅当显式配置了 "" 键时才按配置值执行。
        if sid == "":
            return ""
        return _normalize_amount_token(full_amount_str)
    token = _normalize_amount_token(v)
    try:
        if float(token) <= 0:
            return ""
    except ValueError:
        pass
    return token


def sort_subaccounts_for_sell(rows: List[Dict[str, Any]], cfg: AppConfigIn) -> List[Dict[str, Any]]:
    """
    按配置 sell_sort_field / sell_sort_desc 排序（同键时保持原相对顺序）。
    """
    if not rows:
        return []
    field = (getattr(cfg, "sell_sort_field", None) or "create_time").strip()
    if field not in ("create_time", "ace_amount"):
        field = "create_time"
    desc = bool(getattr(cfg, "sell_sort_desc", False))

    def pk_val(row: Dict[str, Any]):
        if field == "ace_amount":
            v = _parse_ace_amount(row)
            return float("-inf") if v is None else float(v)
        return _parse_created_day_yyyy_mm_dd(row) or ""

    decorated = [(pk_val(r), i, r) for i, r in enumerate(rows)]
    decorated.sort(key=lambda t: (t[0], t[1]), reverse=desc)
    return [t[2] for t in decorated]


def enrich_subaccounts_with_listing_qty(items: List[Dict[str, Any]], cfg: AppConfigIn) -> List[Dict[str, Any]]:
    """
    为每条子账号浅拷贝并写入 ListingQty：listing_amounts_json 有该 sonId 用库中值，否则为全部股数。
    仅用于 API 响应；内存中的 subaccounts_cache 仍保持 RPC 原始行。
    """
    out: List[Dict[str, Any]] = []
    for raw in items:
        row = dict(raw)
        full = ace_amount_string_for_rpc(row)
        if full:
            row["ListingQty"] = effective_listing_amount_str(cfg, listing_amount_key_for_row(row), full)
        elif is_main_account_row(row):
            row["ListingQty"] = "0"
        out.append(row)
    return out
