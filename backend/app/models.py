from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class UtcDateTime(TypeDecorator):
    """SQLite 存 DATETIME 无 tz；读回 naive 一律按 UTC 解释，JSON 可带 +00:00。"""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # 当前使用的交易端配置槽位 0–2（与 trading_configs 复合主键一致）
    active_trading_slot: Mapped[int] = mapped_column(Integer, default=0)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    points_balance: Mapped[int] = mapped_column(Integer, default=0)
    # None = 未开通（须积分兑换）；非空且已过期则须续兑
    subscription_end_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime(), nullable=True)


class AdminEcsInstanceLock(Base):
    """管理端标记：锁定的 ECS 禁止走「释放 ECS」接口（如主程序服务器）。"""

    __tablename__ = "admin_ecs_instance_locks"

    instance_id: Mapped[str] = mapped_column(String(64), primary_key=True)


class ProxyPoolEntry(Base):
    """
    出站 HTTP(S) 代理池（如自建 ECS Squid）。assigned_user_id 为空表示空闲；
    用户首次需要出站 RPC 时从池中领取一条并静态绑定，直至用户删除（FK SET NULL 回收）。
    """

    __tablename__ = "proxy_pool_entries"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    proxy_url: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(String(128), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    assigned_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )


class RunnerLease(Base):
    """
    分布式 runner 互斥：同一 user_id 在 expires_at 前仅 holder_id 匹配实例可卖。
    单机 SQLite 可关 RUNNER_LEASE_ENABLED；多 ECS 共用 PostgreSQL 等时建议开启。
    """

    __tablename__ = "runner_leases"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    holder_id: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class TradingConfig(Base):
    """交易端配置持久化；每用户最多 3 套（slot=0,1,2）。"""

    __tablename__ = "trading_configs"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    slot: Mapped[int] = mapped_column(Integer, primary_key=True, default=0)
    username: Mapped[str] = mapped_column(String(128), default="")
    password_enc: Mapped[str] = mapped_column(Text, default="")
    key_token_enc: Mapped[str] = mapped_column(Text, default="")
    mnemonic_enc: Mapped[str] = mapped_column(Text, default="")
    rpc_login_key_enc: Mapped[str] = mapped_column(Text, default="")
    rpc_user_id: Mapped[str] = mapped_column(String(64), default="")
    rpc_version: Mapped[str] = mapped_column(String(32), default="")
    quantity_start_limit: Mapped[int] = mapped_column(Integer, default=1000)
    request_interval_ms: Mapped[int] = mapped_column(Integer, default=500)
    run_period_start: Mapped[str] = mapped_column(String(32), default="")
    run_period_end: Mapped[str] = mapped_column(String(32), default="")
    # 运行开关落库：关页面/重启后可在 lifespan 中恢复任务
    runner_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 北京时间售卖开始时刻 HH:MM；空串表示不等待固定时刻（与旧行为兼容）
    sell_start_time: Mapped[str] = mapped_column(String(8), default="12:00")
    # {"date":"YYYY-MM-DD","ids":["sonId",...]} 北京时间自然日
    sold_son_ids_json: Mapped[str] = mapped_column(Text, default="{}")
    # {"sonId":"挂售数量"} 字符串；未出现的 sonId 表示挂售数量=全部股数（AceAmount）
    listing_amounts_json: Mapped[str] = mapped_column(Text, default="{}")
    # 售卖时子账号顺序：create_time（创建日）或 ace_amount（股数）；sell_sort_desc 为 True 表示降序
    sell_sort_field: Mapped[str] = mapped_column(String(32), default="create_time")
    sell_sort_desc: Mapped[bool] = mapped_column(Boolean, default=False)
