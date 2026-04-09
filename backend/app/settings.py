from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    jwt_secret: str = "dev-change-me"
    jwt_expire_hours: int = 72
    # 后台管理员（仅存 .env，不入库）；留空则禁用 /api/admin/*
    admin_username: str = ""
    # 二选一：推荐 ADMIN_PASSWORD_HASH（bcrypt）；未设哈希时可用明文 ADMIN_PASSWORD（仅建议本地）
    admin_password: str = ""
    admin_password_hash: str = ""
    admin_jwt_expire_hours: int = 8
    registration_open: bool = True
    # 新注册用户试用天数（从注册时刻起算 subscription_end_at）；0 表示注册后无时长须积分兑换
    new_user_trial_days: int = 0
    # 为已有账号迁移时：subscription_end_at 为空则补齐到「当前 UTC + 该天数」（仅 SQLite 迁移执行一次）
    existing_user_subscription_grace_days: int = 3650

    login_url: str = "https://www.akapi1.com/RPC/Login"
    subaccount_url: str = "https://www.akapi1.com/RPC/My_Subaccount"
    # 子账号卖出 RPC（运行参数中的限额/时段在业务层与 AceAmount、创建日结合使用）
    ace_sell_son_url: str = "https://www.akapi1.com/RPC/ACE_Sell_Son"
    mnemonic_get01_url: str = "https://www.akapi1.com/RPC/Mnemonic_Get01"

    subaccount_page_size: int = 15
    subaccount_max_pages: int = 200
    # 运行任务每轮之间的等待秒数（登录 → 子账号 → Mnemonic_Get01 → ACE_Sell_Son 之后）
    runner_loop_interval_seconds: int = 10
    # 配置了 sell_start_time 时：开售整点（北京时间）之前多少秒开始登录并拉取子账号（全量更新内存缓存一次）
    sell_prep_seconds_before: int = 30

    database_url: str = "sqlite+aiosqlite:///./data/app.db"

    # 出站 httpx 请求文件日志（backend/logs/http_requests.log，敏感字段 JSON 内脱敏）
    request_log_enabled: bool = True
    request_log_dir: str = ""
    request_log_max_bytes: int = 10_485_760
    request_log_backup_count: int = 5
    request_log_max_body: int = 262_144
    # 逗号分隔主机名；仅当 URL 主机等于或为其子域时记录（默认 akapi1.com → 含 www.akapi1.com）
    request_log_outbound_hosts: str = "akapi1.com"

    # 代理池（表 proxy_pool_entries）非空时：用户首次出站 RPC 领取一条并固定绑定。
    # True：池已满无法分配时拒绝（503）；False：回退为不使用代理直连。
    proxy_pool_require_available: bool = True  # PROXY_POOL_REQUIRE_AVAILABLE


settings = Settings()
