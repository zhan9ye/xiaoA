import json
from typing import Any, Dict, List, Optional, Tuple


def _maybe_parse_json_string(value: Any) -> Any:
    if isinstance(value, str) and value.strip().startswith(("{", "[")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value


def extract_subaccount_rows(payload: Any) -> List[Any]:
    """从 My_Subaccount 响应中取出列表数据（兼容多种常见字段名）。"""
    if payload is None:
        return []
    payload = _maybe_parse_json_string(payload)
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []

    if payload.get("Error") is True:
        return []

    data = payload.get("Data")
    data = _maybe_parse_json_string(data)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in (
            "List",
            "Items",
            "Rows",
            "Records",
            "SubAccounts",
            "SubAccountList",
            "Data",
            "Children",
        ):
            v = data.get(k)
            v = _maybe_parse_json_string(v)
            if isinstance(v, list):
                return v

    for k in ("List", "Items", "Rows", "Records", "SubAccounts", "Result"):
        v = payload.get(k)
        v = _maybe_parse_json_string(v)
        if isinstance(v, list):
            return v

    return []


def find_total_count(
    payload: Any,
    rows_len: Optional[int] = None,
    page_size: Optional[int] = None,
) -> Optional[int]:
    """
    解析「全库总条数」用于提前结束翻页。
    注意：Count / ItemCount / 本页 Total 常与当页条数相同，误当总数会导致只拉第一页。
    """

    def _maybe_page_batch_count(key: str, n: int) -> bool:
        """若数值等于「满页条数」，很可能是本批条数而非全库总数。"""
        if rows_len is None or page_size is None:
            return False
        return rows_len == page_size and n == rows_len

    if not isinstance(payload, dict):
        return None

    def pick_int(key: str, raw_val: Any) -> Optional[int]:
        try:
            n = int(raw_val)
        except (TypeError, ValueError):
            return None
        if n < 0:
            return None
        if key in ("Total", "RecordCount") and _maybe_page_batch_count(key, n):
            return None
        return n

    for k in ("TotalCount", "TotalRecords", "RecordsTotal"):
        if k in payload:
            n = pick_int(k, payload[k])
            if n is not None:
                return n

    if "RecordCount" in payload:
        n = pick_int("RecordCount", payload["RecordCount"])
        if n is not None:
            return n

    if "Total" in payload:
        n = pick_int("Total", payload["Total"])
        if n is not None:
            return n

    data = payload.get("Data")
    data = _maybe_parse_json_string(data)
    if isinstance(data, dict):
        inner = find_total_count(data)
        if inner is not None:
            return inner
    return None


def normalize_subaccount_row(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        return item
    return {"value": item}


def should_request_next_page(
    rows: List[Any],
    page_size: int,
    payload: Any,
    collected_count: int,
) -> Tuple[bool, str]:
    """
    是否继续请求 p+1。
    """
    if len(rows) == 0:
        return False, "本页无数据"

    total = (
        find_total_count(payload, len(rows), page_size)
        if isinstance(payload, dict)
        else None
    )
    if total is not None and collected_count >= total:
        return False, f"已达声明总数（Total*={total}）"

    if len(rows) < page_size:
        return False, f"本页 {len(rows)} 条 < 每页 {page_size}，视为末页"

    if total is not None:
        return True, f"已收 {collected_count}/{total}，继续下一页"

    return True, f"本页满 {page_size} 条，继续下一页（未声明可靠总数）"
