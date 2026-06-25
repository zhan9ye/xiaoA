from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import TradingConfig, User
from app.schemas import AppConfigIn
from app.state import AppState
from app.trading_crypto import decrypt_trading_field, encrypt_trading_field

MAX_TRADING_CONFIG_SLOTS = 3


def _encrypted_field_for_persist(plain: str, existing_enc: Optional[str]) -> str:
    """
    写入库时的加密字段：新值为空则保留已有密文，禁止空串覆盖（批量 Login 等误 persist 曾导致全站密钥丢失）。
    """
    if (plain or "").strip():
        return encrypt_trading_field(plain)
    prev = (existing_enc or "").strip()
    return prev


def _cfg_preserving_row_secrets(cfg: AppConfigIn, row: Optional[TradingConfig]) -> AppConfigIn:
    """合并已有行中的业务字段，避免 probe/Login 产生的空配置抹掉运行参数与 JSON 状态。"""
    if row is None:
        return cfg
    updates: Dict[str, Any] = {}
    if not (cfg.key_token or "").strip() and (row.key_token_enc or "").strip():
        try:
            updates["key_token"] = decrypt_trading_field(row.key_token_enc)
        except ValueError:
            pass
    if not (cfg.mnemonic or "").strip() and (row.mnemonic_enc or "").strip():
        try:
            updates["mnemonic"] = decrypt_trading_field(row.mnemonic_enc)
        except ValueError:
            pass
    old_listing = (row.listing_amounts_json or "").strip() or "{}"
    new_listing = (cfg.listing_amounts_json or "").strip() or "{}"
    if new_listing in ("", "{}") and old_listing not in ("", "{}"):
        updates["listing_amounts_json"] = old_listing
    old_sold = (row.sold_son_ids_json or "").strip() or "{}"
    new_sold = (cfg.sold_son_ids_json or "").strip() or "{}"
    if new_sold in ("", "{}") and old_sold not in ("", "{}"):
        updates["sold_son_ids_json"] = old_sold
    if int(cfg.quantity_start_limit or 0) == 0 and int(row.quantity_start_limit or 0) > 0:
        updates["quantity_start_limit"] = int(row.quantity_start_limit)
    if not (cfg.run_period_start or "").strip() and (row.run_period_start or "").strip():
        updates["run_period_start"] = row.run_period_start
    if not (cfg.run_period_end or "").strip() and (row.run_period_end or "").strip():
        updates["run_period_end"] = row.run_period_end
    if not updates:
        return cfg
    return cfg.model_copy(update=updates)


def _row_to_app_config(row: TradingConfig) -> AppConfigIn:
    try:
        pw = decrypt_trading_field(row.password_enc)
        key_tok = decrypt_trading_field(row.key_token_enc)
        mn = decrypt_trading_field(row.mnemonic_enc)
        rk = decrypt_trading_field(row.rpc_login_key_enc)
    except ValueError:
        raise
    if not pw:
        pw = " "
    ri = int(row.request_interval_ms or 1000)
    if ri < 500:
        ri = 500
    ssf = (getattr(row, "sell_sort_field", None) or "create_time").strip()
    if ssf not in ("create_time", "ace_amount"):
        ssf = "create_time"
    ssd = bool(getattr(row, "sell_sort_desc", False))
    return AppConfigIn.model_construct(
        username=(row.username or "").strip() or "user",
        password=pw,
        key_token=key_tok,
        mnemonic=mn,
        rpc_login_key=rk,
        rpc_user_id=row.rpc_user_id or "",
        quantity_start_limit=int(row.quantity_start_limit or 0),
        request_interval_ms=ri,
        run_period_start=row.run_period_start or "",
        run_period_end=row.run_period_end or "",
        runner_enabled=bool(getattr(row, "runner_enabled", False)),
        sell_start_time=(getattr(row, "sell_start_time", None) or "") or "",
        sold_son_ids_json=(getattr(row, "sold_son_ids_json", None) or "") or "{}",
        listing_amounts_json=(getattr(row, "listing_amounts_json", None) or "") or "{}",
        main_account_info_json=(getattr(row, "main_account_info_json", None) or "") or "{}",
        sell_sort_field=ssf,
        sell_sort_desc=ssd,
    )


async def get_active_trading_slot(session: AsyncSession, user_id: int) -> int:
    u = await session.get(User, user_id)
    if u is None:
        return 0
    s = getattr(u, "active_trading_slot", None)
    if s is None:
        return 0
    return max(0, min(MAX_TRADING_CONFIG_SLOTS - 1, int(s)))


async def set_active_trading_slot(session: AsyncSession, user_id: int, slot: int) -> None:
    slot = max(0, min(MAX_TRADING_CONFIG_SLOTS - 1, int(slot)))
    u = await session.get(User, user_id)
    if u is None:
        return
    u.active_trading_slot = slot
    await session.commit()


async def load_trading_config_slot(
    session: AsyncSession, user_id: int, slot: int
) -> Optional[AppConfigIn]:
    slot = max(0, min(MAX_TRADING_CONFIG_SLOTS - 1, int(slot)))
    row = await session.get(TradingConfig, (user_id, slot))
    if row is None:
        return None
    try:
        return _row_to_app_config(row)
    except ValueError:
        return None


async def load_trading_config(session: AsyncSession, user_id: int) -> Optional[AppConfigIn]:
    """加载当前用户「活动槽位」的交易配置。"""
    slot = await get_active_trading_slot(session, user_id)
    return await load_trading_config_slot(session, user_id, slot)


async def list_trading_slot_briefs(session: AsyncSession, user_id: int) -> List[Dict[str, Any]]:
    active = await get_active_trading_slot(session, user_id)
    out: List[Dict[str, Any]] = []
    for slot in range(MAX_TRADING_CONFIG_SLOTS):
        row = await session.get(TradingConfig, (user_id, slot))
        uname = (row.username or "").strip() if row else ""
        has_saved = row is not None and (
            bool(uname) or bool((row.password_enc or "").strip()) or bool((row.key_token_enc or "").strip())
        )
        out.append(
            {
                "slot": slot,
                "username": uname,
                "has_saved": has_saved,
                "is_active": slot == active,
            }
        )
    return out


async def persist_trading_config(
    session: AsyncSession, user_id: int, slot: int, cfg: AppConfigIn
) -> None:
    slot = max(0, min(MAX_TRADING_CONFIG_SLOTS - 1, int(slot)))
    row = await session.get(TradingConfig, (user_id, slot))
    cfg = _cfg_preserving_row_secrets(cfg, row)
    prev_key_enc = (row.key_token_enc if row else None) or ""
    prev_mn_enc = (row.mnemonic_enc if row else None) or ""
    pw_enc = encrypt_trading_field(cfg.password)
    key_enc = _encrypted_field_for_persist(cfg.key_token, prev_key_enc or None)
    mn_enc = _encrypted_field_for_persist(cfg.mnemonic, prev_mn_enc or None)
    rk_enc = encrypt_trading_field(cfg.rpc_login_key)
    ssf = (cfg.sell_sort_field or "create_time").strip()
    if ssf not in ("create_time", "ace_amount"):
        ssf = "create_time"
    if row is None:
        row = TradingConfig(
            user_id=user_id,
            slot=slot,
            username=cfg.username,
            password_enc=pw_enc,
            key_token_enc=key_enc,
            mnemonic_enc=mn_enc,
            rpc_login_key_enc=rk_enc,
            rpc_user_id=cfg.rpc_user_id or "",
            rpc_version="",
            quantity_start_limit=cfg.quantity_start_limit,
            request_interval_ms=max(500, int(cfg.request_interval_ms or 1000)),
            run_period_start=cfg.run_period_start or "",
            run_period_end=cfg.run_period_end or "",
            runner_enabled=bool(cfg.runner_enabled),
            sell_start_time=cfg.sell_start_time or "",
            sold_son_ids_json=cfg.sold_son_ids_json or "{}",
            listing_amounts_json=cfg.listing_amounts_json or "{}",
            main_account_info_json=cfg.main_account_info_json or "{}",
            sell_sort_field=ssf,
            sell_sort_desc=bool(cfg.sell_sort_desc),
        )
        session.add(row)
    else:
        row.username = cfg.username
        row.password_enc = pw_enc
        row.key_token_enc = key_enc
        row.mnemonic_enc = mn_enc
        row.rpc_login_key_enc = rk_enc
        row.rpc_user_id = cfg.rpc_user_id or ""
        row.rpc_version = ""
        row.quantity_start_limit = cfg.quantity_start_limit
        row.request_interval_ms = max(500, int(cfg.request_interval_ms or 1000))
        row.run_period_start = cfg.run_period_start or ""
        row.run_period_end = cfg.run_period_end or ""
        row.runner_enabled = bool(cfg.runner_enabled)
        row.sell_start_time = cfg.sell_start_time or ""
        row.sold_son_ids_json = cfg.sold_son_ids_json or "{}"
        row.listing_amounts_json = cfg.listing_amounts_json or "{}"
        row.main_account_info_json = cfg.main_account_info_json or "{}"
        row.sell_sort_field = ssf
        row.sell_sort_desc = bool(cfg.sell_sort_desc)
    await session.commit()


async def persist_trading_config_standalone(user_id: int, cfg: AppConfigIn) -> None:
    async with AsyncSessionLocal() as session:
        slot = await get_active_trading_slot(session, user_id)
        await persist_trading_config(session, user_id, slot, cfg)


async def ensure_trading_config_loaded(db: AsyncSession, user_id: int, st: AppState) -> bool:
    slot = await get_active_trading_slot(db, user_id)
    if st.config is not None and st.loaded_config_slot == slot:
        return True
    cfg = await load_trading_config_slot(db, user_id, slot)
    st.config = cfg
    st.loaded_config_slot = slot
    return cfg is not None
