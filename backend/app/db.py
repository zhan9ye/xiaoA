from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.settings import settings


class Base(DeclarativeBase):
    pass


def _ensure_data_dir() -> None:
    if settings.database_url.startswith("sqlite"):
        raw = settings.database_url.replace("sqlite+aiosqlite:///", "", 1)
        path = Path(raw).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)


_ensure_data_dir()

engine = create_async_engine(settings.database_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if settings.database_url.startswith("sqlite"):
            rows = (await conn.exec_driver_sql("PRAGMA table_info(trading_configs)")).fetchall()
            cols = {str(r[1]) for r in rows}
            if "request_interval_ms" not in cols:
                await conn.exec_driver_sql(
                    "ALTER TABLE trading_configs ADD COLUMN request_interval_ms INTEGER DEFAULT 500"
                )
            if "runner_enabled" not in cols:
                await conn.exec_driver_sql(
                    "ALTER TABLE trading_configs ADD COLUMN runner_enabled INTEGER DEFAULT 0"
                )
            if "sell_start_time" not in cols:
                await conn.exec_driver_sql(
                    "ALTER TABLE trading_configs ADD COLUMN sell_start_time VARCHAR(8) DEFAULT ''"
                )
            if "sold_son_ids_json" not in cols:
                await conn.exec_driver_sql(
                    "ALTER TABLE trading_configs ADD COLUMN sold_son_ids_json TEXT DEFAULT '{}'"
                )
            if "listing_amounts_json" not in cols:
                await conn.exec_driver_sql(
                    "ALTER TABLE trading_configs ADD COLUMN listing_amounts_json TEXT DEFAULT '{}'"
                )
            if "main_account_info_json" not in cols:
                await conn.exec_driver_sql(
                    "ALTER TABLE trading_configs ADD COLUMN main_account_info_json TEXT DEFAULT '{}'"
                )
            if "sell_sort_field" not in cols:
                await conn.exec_driver_sql(
                    "ALTER TABLE trading_configs ADD COLUMN sell_sort_field VARCHAR(32) DEFAULT 'create_time'"
                )
            if "sell_sort_desc" not in cols:
                await conn.exec_driver_sql(
                    "ALTER TABLE trading_configs ADD COLUMN sell_sort_desc INTEGER DEFAULT 0"
                )

            rows = (await conn.exec_driver_sql("PRAGMA table_info(trading_configs)")).fetchall()
            cols = {str(r[1]) for r in rows}
            if "slot" not in cols:
                await conn.exec_driver_sql("ALTER TABLE trading_configs RENAME TO trading_configs_old")

                def _create_trading_configs(sync_conn):
                    from app.models import TradingConfig  # noqa: PLC0415

                    TradingConfig.__table__.create(sync_conn, checkfirst=True)

                await conn.run_sync(_create_trading_configs)
                await conn.exec_driver_sql(
                    """
                    INSERT INTO trading_configs (
                        user_id, slot, username, password_enc, key_token_enc, mnemonic_enc,
                        rpc_login_key_enc, rpc_user_id, rpc_version, quantity_start_limit,
                        request_interval_ms, run_period_start, run_period_end, runner_enabled,
                        sell_start_time, sold_son_ids_json, listing_amounts_json, main_account_info_json,
                        sell_sort_field, sell_sort_desc
                    )
                    SELECT
                        user_id,
                        0 AS slot,
                        username,
                        password_enc,
                        key_token_enc,
                        mnemonic_enc,
                        rpc_login_key_enc,
                        rpc_user_id,
                        rpc_version,
                        quantity_start_limit,
                        COALESCE(request_interval_ms, 500),
                        run_period_start,
                        run_period_end,
                        COALESCE(runner_enabled, 0),
                        COALESCE(sell_start_time, ''),
                        COALESCE(sold_son_ids_json, '{}'),
                        COALESCE(listing_amounts_json, '{}'),
                        '{}',
                        COALESCE(sell_sort_field, 'create_time'),
                        COALESCE(sell_sort_desc, 0)
                    FROM trading_configs_old
                    """
                )
                await conn.exec_driver_sql("DROP TABLE trading_configs_old")

            urows = (await conn.exec_driver_sql("PRAGMA table_info(users)")).fetchall()
            ucols = {str(r[1]) for r in urows}
            if "points_balance" not in ucols:
                await conn.exec_driver_sql(
                    "ALTER TABLE users ADD COLUMN points_balance INTEGER NOT NULL DEFAULT 0"
                )
            if "is_disabled" not in ucols:
                await conn.exec_driver_sql(
                    "ALTER TABLE users ADD COLUMN is_disabled INTEGER NOT NULL DEFAULT 0"
                )
            if "subscription_end_at" not in ucols:
                await conn.exec_driver_sql("ALTER TABLE users ADD COLUMN subscription_end_at DATETIME")
                from datetime import datetime, timedelta, timezone

                far = datetime.now(timezone.utc) + timedelta(days=settings.existing_user_subscription_grace_days)
                await conn.execute(
                    text("UPDATE users SET subscription_end_at = :far WHERE subscription_end_at IS NULL"),
                    {"far": far},
                )
            if "active_trading_slot" not in ucols:
                await conn.exec_driver_sql(
                    "ALTER TABLE users ADD COLUMN active_trading_slot INTEGER NOT NULL DEFAULT 0"
                )
            if "admin_remark" not in ucols:
                await conn.exec_driver_sql(
                    "ALTER TABLE users ADD COLUMN admin_remark TEXT NOT NULL DEFAULT ''"
                )

            try:
                op_rows = (await conn.exec_driver_sql("PRAGMA table_info(user_operation_logs)")).fetchall()
            except Exception:
                op_rows = []
            op_cols = {str(r[1]) for r in op_rows}
            if op_rows and "business_summary" not in op_cols:
                await conn.exec_driver_sql(
                    "ALTER TABLE user_operation_logs ADD COLUMN business_summary VARCHAR(256) NOT NULL DEFAULT ''"
                )
