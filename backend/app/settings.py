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
    # 同一 IP 登录失败达到此次数后，须先通过算术验证码再验密码（内存计数，多 worker 各算各的）
    login_captcha_after_failures: int = 3
    login_captcha_ttl_seconds: int = 300
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
    # RPC 请求超时秒数（抢购建议设为 5-10，平时可设为 30）
    rpc_timeout_seconds: float = 10.0
    # HotWindow 并发数：同时处理多少个子账号的售卖（默认 1，抢购建议 3-5，过高易 429）
    hot_window_concurrency: int = 1
    # 配置了 sell_start_time 时：开售整点（北京时间）之前多少秒开始登录并拉取子账号（全量更新内存缓存一次）
    sell_prep_seconds_before: int = 30
    # 若启动任务时北京时间已超过「开售整点 + 本分钟数」，本日不调用对外售卖链路 RPC，仅内部等待至次日
    sell_start_missed_grace_minutes: int = 10
    # 配置了 sell_start_time 时：仅当「北京时间 ≥ 开售时刻 + 本秒数」后，ACE_Sell_Son 返回「本日交易通道已關閉」才视为真收工。
    # 避免上游晚 1～2 秒开门时，整点请求误把「尚未开放」当成通道已关。
    sell_channel_closed_trust_after_seconds: int = 60
    # 信任窗口内收到「通道关闭」后，两次 ACE 重试之间的间隔毫秒（过小易 503，默认 100）
    sell_channel_closed_grace_retry_ms: int = 100
    # 停止后再开始、未登录后补拉子账号：最多尝试次数与间隔（毫秒）
    sell_resume_sub_fetch_max_attempts: int = 6
    sell_resume_sub_fetch_delay_ms: int = 500
    # 定时开售：准备阶段 Login+My_Subaccount 最大尝试次数（每次尝试均须在 T_open 前）
    sell_prep_max_attempts: int = 8
    sell_prep_retry_delay_seconds: float = 2.0
    # WaitOpen：先睡到 T_open - 本毫秒数再睡到整点（最后组装）
    sell_wait_open_wake_early_ms: int = 50
    # 定时开售：距开售整点前多少秒起，周期性请求 Mnemonic_Get01 预热 TLS/TCP 连接（0 关闭）
    sell_warmup_seconds_before_open: int = 15
    # 预热期间两次 ping 的间隔秒数（下限 2）
    sell_warmup_ping_interval_seconds: float = 6.0
    # 多实例互斥（需共用同一数据库）；单机可 false
    runner_lease_enabled: bool = False
    runner_lease_ttl_seconds: int = 45
    # 留空则进程内随机 UUID；多机部署建议每机固定不同值（如 hostname）
    runner_lease_holder_id: str = ""

    database_url: str = "sqlite+aiosqlite:///./data/app.db"

    # 出站 httpx 请求文件日志（backend/logs/http_requests.log；不设脱敏，注意文件权限）
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

    # 出站 httpx 是否校验 HTTPS 证书链（连接 akapi1 等 RPC）。默认 True。
    # 若对端证书链不完整导致 [SSL: CERTIFICATE_VERIFY_FAILED]，可先设 false 权宜（存在中间人风险）。
    outbound_tls_verify: bool = True  # OUTBOUND_TLS_VERIFY


settings = Settings()
