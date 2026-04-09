import json
from typing import Tuple

from app.schemas import AppConfigIn


def merge_from_rpc_login(cfg: AppConfigIn, response_body: str) -> Tuple[AppConfigIn, bool]:
    """
    解析 Login JSON：UserData.Id → rpc_user_id；顶层 Key → rpc_login_key（会话 key，如子账号接口的 key）。
    key_token 为用户填写的后续接口验证码，不从登录响应写入。
    当 Error 为 true 或缺少 UserData.Id 时返回原配置且 changed=False。
    """
    if not (response_body or "").strip():
        return cfg, False
    try:
        data = json.loads(response_body)
    except json.JSONDecodeError:
        return cfg, False

    if data.get("Error") is True:
        return cfg, False

    ud = data.get("UserData")
    if not isinstance(ud, dict):
        return cfg, False

    uid = ud.get("Id")
    key = data.get("Key")
    if uid is None:
        return cfg, False
    if isinstance(uid, bool):
        return cfg, False

    if isinstance(uid, int):
        new_uid = str(uid)
    elif isinstance(uid, float):
        new_uid = str(int(uid))
    else:
        new_uid = str(uid).strip()

    if not new_uid:
        return cfg, False

    new_key = str(key).strip() if key is not None else ""

    updates: dict = {"rpc_user_id": new_uid}
    if new_key:
        updates["rpc_login_key"] = new_key

    merged = cfg.model_copy(update=updates)
    prev_lk = (cfg.rpc_login_key or "").strip()
    new_lk = (merged.rpc_login_key or "").strip()
    prev_u = (cfg.rpc_user_id or "").strip()
    new_u = (merged.rpc_user_id or "").strip()
    changed = (prev_u != new_u) or (prev_lk != new_lk)
    return merged, changed
