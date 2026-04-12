from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import TradingConfig
from app.schemas import AppConfigIn
from app.state import AppState
from app.trading_crypto import decrypt_trading_field, encrypt_trading_field


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
        sell_sort_field=ssf,
        sell_sort_desc=ssd,
    )


async def load_trading_config(session: AsyncSession, user_id: int) -> Optional[AppConfigIn]:
    row = await session.get(TradingConfig, user_id)
    if row is None:
        return None
    try:
        return _row_to_app_config(row)
    except ValueError:
        return None


async def persist_trading_config(session: AsyncSession, user_id: int, cfg: AppConfigIn) -> None:
    row = await session.get(TradingConfig, user_id)
    pw_enc = encrypt_trading_field(cfg.password)
    key_enc = encrypt_trading_field(cfg.key_token)
    mn_enc = encrypt_trading_field(cfg.mnemonic)
    rk_enc = encrypt_trading_field(cfg.rpc_login_key)
    ssf = (cfg.sell_sort_field or "create_time").strip()
    if ssf not in ("create_time", "ace_amount"):
        ssf = "create_time"
    if row is None:
        row = TradingConfig(
            user_id=user_id,
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
        row.sell_sort_field = ssf
        row.sell_sort_desc = bool(cfg.sell_sort_desc)
    await session.commit()


async def persist_trading_config_standalone(user_id: int, cfg: AppConfigIn) -> None:
    async with AsyncSessionLocal() as session:
        await persist_trading_config(session, user_id, cfg)


async def ensure_trading_config_loaded(db: AsyncSession, user_id: int, st: AppState) -> bool:
    if st.config is not None:
        return True
    cfg = await load_trading_config(db, user_id)
    if cfg is None:
        return False
    st.config = cfg
    return True
