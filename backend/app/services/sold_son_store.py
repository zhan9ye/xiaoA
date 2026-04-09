"""已售子账号 JSON：{\"date\":\"YYYY-MM-DD\",\"ids\":[...]}（北京时间自然日）。"""

from __future__ import annotations

import json
from typing import Set


def sold_son_ids_for_today(sold_json: str, today_bj: str) -> Set[str]:
    try:
        o = json.loads(sold_json or "{}")
    except Exception:
        return set()
    if o.get("date") != today_bj:
        return set()
    ids = o.get("ids") or []
    return {str(x).strip() for x in ids if str(x).strip()}


def add_sold_son_json(sold_json: str, today_bj: str, son_id: str) -> str:
    sid = str(son_id).strip()
    if not sid:
        return sold_json or "{}"
    try:
        o = json.loads(sold_json or "{}")
    except Exception:
        o = {}
    if o.get("date") != today_bj:
        o = {"date": today_bj, "ids": []}
    ids = {str(x).strip() for x in (o.get("ids") or []) if str(x).strip()}
    ids.add(sid)
    o["date"] = today_bj
    o["ids"] = sorted(ids)
    return json.dumps(o, ensure_ascii=False)
