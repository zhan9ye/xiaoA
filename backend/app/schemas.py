from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.credits_service import CREDIT_PACKAGES


def _normalize_hhmm_beijing(s: str) -> str:
    """空串或 HH:MM（北京时间售卖开始时刻）。"""
    raw = (s or "").strip()
    if not raw:
        return ""
    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError("售卖开始时间须为 HH:MM（北京时间）或留空")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError("售卖开始时间须为合法 HH:MM")
    return f"{h:02d}:{m:02d}"


def _normalize_day(s: str) -> str:
    """空串或合法 YYYY-MM-DD（按公历）。"""
    raw = (s or "").strip()
    if not raw:
        return ""
    parts = raw.split("-")
    if len(parts) != 3:
        raise ValueError("日期须为 YYYY-MM-DD")
    y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    date(y, m, d)
    return f"{y:04d}-{m:02d}-{d:02d}"


class AppConfigFormIn(BaseModel):
    """操作台提交的可见字段（密钥/UserID/版本/分页由服务端内部处理）。"""

    username: str = Field(..., min_length=1, description="交易端登录账号")
    password: str = Field(
        default="",
        max_length=512,
        description="交易端密码；留空表示保留服务端已保存的密码",
    )
    key_token: str = Field(
        default="",
        max_length=128,
        description=(
            "Google 验证器「共享密钥」（常为 Base32）；服务端对 ACE_Sell_Son 等接口用其按 TOTP 现算 gCode。"
            "与登录 JSON 顶层 Key 无关"
        ),
    )
    mnemonic: str = Field(
        default="",
        description=(
            "12 组逗号分隔的 4 位数字（如 1148,1015,...），与 ACE 接口 mnemonicstr1 按序号对应；"
            "提交空串时保留服务端已存值"
        ),
    )
    quantity_start_limit: int = Field(
        default=1000,
        ge=0,
        description="数量起始限额：仅当子账号 AceAmount **大于** 该值时才参与 ACE_Sell_Son 售卖",
    )
    request_interval_ms: int = Field(
        default=1000,
        ge=500,
        le=60000,
        description="同一账号两次 ACE_Sell_Son 请求间隔（毫秒）：≥500，固定使用本配置",
    )
    run_period_start: str = Field(
        default="",
        description="售卖时段开始日 YYYY-MM-DD：仅统计「创建日」在此之后的子账号；可空",
    )
    run_period_end: str = Field(
        default="",
        description="售卖时段结束日 YYYY-MM-DD：仅统计「创建日」在此之前的子账号；可空",
    )

    sell_start_time: str = Field(
        default="12:00",
        description="北京时间售卖开始 HH:MM；留空则不等待固定时刻",
    )
    sell_sort_field: str = Field(
        default="create_time",
        description="子账号售卖顺序：create_time=创建日，ace_amount=股数",
    )
    sell_sort_desc: bool = Field(default=False, description="True=降序，False=升序")

    @field_validator("sell_sort_field", mode="before")
    @classmethod
    def _v_sell_sort_field_form(cls, v: Any) -> str:
        s = (str(v) if v is not None else "create_time").strip()
        if s not in ("create_time", "ace_amount"):
            raise ValueError("sell_sort_field 须为 create_time 或 ace_amount")
        return s

    @field_validator("sell_start_time", mode="before")
    @classmethod
    def _v_sell_start(cls, v: Any) -> str:
        if v is None:
            return ""
        return _normalize_hhmm_beijing(str(v))

    @field_validator("run_period_start", "run_period_end", mode="before")
    @classmethod
    def _v_day(cls, v: Any) -> str:
        if v is None:
            return ""
        return _normalize_day(str(v))

    @model_validator(mode="after")
    def _v_period_order(self):
        if self.run_period_start and self.run_period_end:
            if self.run_period_start > self.run_period_end:
                raise ValueError("开始日期不能晚于结束日期")
        return self


class AppConfigIn(BaseModel):
    """内部完整交易配置：验证码 key_token 为用户填写；Login 的 Key/UserID 单独存放。"""

    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    key_token: str = Field(
        default="",
        description="Google 共享密钥；用于 TOTP 生成 gCode，不由登录响应覆盖",
    )
    rpc_login_key: str = Field(default="", description="Login 响应顶层 Key，用于 My_Subaccount 等带 key 参数的接口")
    rpc_user_id: str = Field(default="")
    mnemonic: str = Field(default="")
    quantity_start_limit: int = Field(
        default=1000,
        ge=0,
        description="AceAmount 须 **大于** 此值才允许卖出（ACE_Sell_Son）",
    )
    request_interval_ms: int = Field(
        default=1000,
        ge=500,
        le=60000,
        description="ACE_Sell_Son 请求间隔（毫秒）：≥500，固定使用本配置",
    )
    run_period_start: str = Field(default="", description="子账号创建日筛选：区间起")
    run_period_end: str = Field(default="", description="子账号创建日筛选：区间止")
    runner_enabled: bool = Field(default=False, description="运行开关（落库，进程可恢复）")
    sell_start_time: str = Field(default="12:00", description="北京时间 HH:MM；空则不等待固定时刻")
    sold_son_ids_json: str = Field(default="{}", description="已售子账号 JSON，服务端维护")
    listing_amounts_json: str = Field(
        default="{}",
        description='按子账号覆盖挂售数量 JSON：{"sonId":"数量"}；缺省则挂售全部股数',
    )
    sell_sort_field: str = Field(default="create_time")
    sell_sort_desc: bool = Field(default=False)

    @field_validator("run_period_start", "run_period_end", mode="before")
    @classmethod
    def _v_day_in(cls, v: Any) -> str:
        if v is None:
            return ""
        return _normalize_day(str(v))

    @field_validator("sell_start_time", mode="before")
    @classmethod
    def _v_sell_start_in(cls, v: Any) -> str:
        if v is None:
            return ""
        return _normalize_hhmm_beijing(str(v))

    @field_validator("sell_sort_field", mode="before")
    @classmethod
    def _v_sell_sort_field_in(cls, v: Any) -> str:
        s = (str(v) if v is not None else "create_time").strip()
        if s not in ("create_time", "ace_amount"):
            return "create_time"
        return s

    @model_validator(mode="after")
    def _v_period_order_in(self):
        if self.run_period_start and self.run_period_end:
            if self.run_period_start > self.run_period_end:
                raise ValueError("开始日期不能晚于结束日期")
        return self


class AppConfigOut(BaseModel):
    username: str
    password: str = Field(
        default="",
        description="交易端密码明文（与 key_token 一致由接口回显，便于核对；请勿在不可信环境截屏外传）",
    )
    key_token: str
    mnemonic: str
    quantity_start_limit: int
    request_interval_ms: int
    run_period_start: str
    run_period_end: str
    sell_start_time: str = ""
    sell_sort_field: str = "create_time"
    sell_sort_desc: bool = False
    listing_amounts: Dict[str, str] = Field(
        default_factory=dict,
        description="挂售数量覆盖 sonId→数量；缺省键表示用全部股数",
    )


class ListingAmountPatchIn(BaseModel):
    son_id: str = Field(..., min_length=1, description="子账号 id，与 RPC sonId 一致")
    amount: str = Field(
        default="",
        description="挂售数量；空字符串表示恢复为全部股数（清除该 sonId 的覆盖）",
    )


class RunParamsFormIn(BaseModel):
    """仅更新运行参数（日期区间、数量限额等），不影响账号与密钥。"""

    quantity_start_limit: int = Field(
        default=1000,
        ge=0,
        description="数量起始限额：仅当子账号 AceAmount **大于** 该值时才参与 ACE_Sell_Son 售卖",
    )
    request_interval_ms: int = Field(
        default=1000,
        ge=500,
        le=60000,
        description="ACE_Sell_Son 请求间隔（毫秒）：≥500，固定使用本配置",
    )
    run_period_start: str = Field(default="", description="售卖时段开始日 YYYY-MM-DD；可空")
    run_period_end: str = Field(default="", description="售卖时段结束日 YYYY-MM-DD；可空")
    sell_start_time: str = Field(default="12:00", description="北京时间售卖开始 HH:MM；空则不等待固定时刻")
    sell_sort_field: Optional[str] = Field(
        default=None,
        description="create_time 或 ace_amount；省略则保留原值",
    )
    sell_sort_desc: Optional[bool] = Field(default=None, description="降序；省略则保留原值")

    @field_validator("run_period_start", "run_period_end", mode="before")
    @classmethod
    def _v_day_rp(cls, v: Any) -> str:
        if v is None:
            return ""
        return _normalize_day(str(v))

    @field_validator("sell_start_time", mode="before")
    @classmethod
    def _v_sell_start_rp(cls, v: Any) -> str:
        if v is None:
            return ""
        return _normalize_hhmm_beijing(str(v))

    @field_validator("sell_sort_field", mode="before")
    @classmethod
    def _v_sell_sort_field_rp(cls, v: Any) -> Optional[str]:
        if v is None or v == "":
            return None
        s = str(v).strip()
        if s not in ("create_time", "ace_amount"):
            raise ValueError("sell_sort_field 须为 create_time 或 ace_amount")
        return s

    @model_validator(mode="after")
    def _v_period_order_rp(self):
        if self.run_period_start and self.run_period_end:
            if self.run_period_start > self.run_period_end:
                raise ValueError("开始日期不能晚于结束日期")
        return self


class RunParamsOut(BaseModel):
    quantity_start_limit: int
    request_interval_ms: int
    run_period_start: str
    run_period_end: str
    sell_start_time: str = ""
    sell_sort_field: str = "create_time"
    sell_sort_desc: bool = False


class SubaccountsOut(BaseModel):
    count: int
    items: List[Dict[str, Any]]


class MnemonicGet01Out(BaseModel):
    """Mnemonic_Get01 解析结果（供调试或前端展示）。"""

    ok: bool
    status_code: int
    mnemonicid1: str = ""
    mnemonickey: str = ""
    mnemonictitle: str = ""
    raw_message: str = ""


class AceSellSonIn(BaseModel):
    """
    ACE_Sell_Son 业务参数（与 akapi1 抓包字段对应）。
    不传 mnemonic_key 时会先请求 Mnemonic_Get01 获取 mnemonicid1 / mnemonickey；
    mnemonicstr1 仍由配置中 12 段数字与 mnemonic_id1 推导（mnemonic_id1 未传时以接口返回为准）。
    """

    son_id: str = Field(..., min_length=1, description="子账号 id，如 4218087")
    amount: str = Field(..., min_length=1)
    mnemonic_id1: Optional[str] = Field(
        default=None,
        description="第几段 4 位数字；不传且已调 Mnemonic_Get01 时用接口返回的 mnemonicid1",
    )
    mnemonic_key: Optional[str] = Field(
        default=None,
        description="mnemonickey；不传则先 POST Mnemonic_Get01（key/UserID/v/lang）",
    )
    mnemonic_str1: Optional[str] = Field(
        default=None,
        description="不传则从配置助记词（逗号分隔 12 段）按 mnemonic_id1 取对应一段",
    )
    count: Optional[str] = Field(
        default=None,
        description="对应表单 count；不传则尝试从已缓存子账号行匹配 son_id 的 AceAmount",
    )
    g_code: Optional[str] = Field(
        default=None,
        description="表单 gCode；不传则用配置 key_token（Google 共享密钥）按 TOTP 现算 6 位",
    )


class AceSellSonOut(BaseModel):
    ok: bool
    status_code: int
    message: str
    data: Optional[Any] = None


class LoginResult(BaseModel):
    ok: bool
    status_code: int
    message: str
    cookies: Dict[str, str] = Field(default_factory=dict)
    # 完整响应正文，仅用于服务端写日志，不随 API JSON 返回
    response_body: str = Field(default="", exclude=True)


class RunStatus(BaseModel):
    running: bool
    last_error: Optional[str] = None
    runner_enabled: bool = False
    floor_curr_ms: float = 50.0
    sr429_window: Optional[float] = None
    window_samples: int = 0
    # 定时开售：当前是否处于「晚启动本日仅内部等待、不调对外 RPC」
    timed_sell_internal_only_today: bool = False
    # 未运行时：若此时点「开始」将触发上述内部等待（开售整点已过超过 sell_start_missed_grace_minutes）
    timed_sell_would_skip_outbound_if_started: bool = False
    # 开售进行中：禁止刷新子账号、改售卖排序
    subaccount_controls_locked: bool = False


class UserRegisterIn(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)


class UserLoginIn(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserPublic(BaseModel):
    id: int
    username: str


class CreditPackageOut(BaseModel):
    days: int
    points_cost: int


class CreditsOverviewOut(BaseModel):
    points_balance: int
    subscription_end_at: Optional[datetime] = None
    subscription_active: bool
    packages: List[CreditPackageOut]


class RedeemDaysIn(BaseModel):
    days: int = Field(..., description="套餐天数：7 / 30 / 90 / 180 / 360")

    @field_validator("days")
    @classmethod
    def _v_redeem_days(cls, v: int) -> int:
        if v not in CREDIT_PACKAGES:
            raise ValueError(f"days 须为 {sorted(CREDIT_PACKAGES)} 之一")
        return v


class RedeemDaysOut(BaseModel):
    points_balance: int
    subscription_end_at: Optional[datetime] = None
    redeemed_days: int
    points_spent: int


class RedeemPreviewOut(BaseModel):
    """兑换前预览：与 POST /api/credits/redeem 写入的结束时刻一致（不扣积分）。"""

    subscription_end_at: datetime
    points_cost: int


# —— 后台管理（.env 管理员，不入库）——


class AdminLoginIn(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class AdminTokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AdminUserRow(BaseModel):
    id: int
    username: str
    is_disabled: bool
    points_balance: int
    subscription_end_at: Optional[datetime] = None
    # 出站代理池绑定（proxy_pool_entries）
    proxy_entry_id: Optional[int] = None
    proxy_label: Optional[str] = None
    proxy_host_preview: Optional[str] = None


class AdminUserListOut(BaseModel):
    users: List[AdminUserRow]


class AdminCreateUserIn(BaseModel):
    """管理员创建平台用户（不受 registration_open 限制；试用天数与公开注册一致）。"""

    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)


class AdminSetPasswordIn(BaseModel):
    new_password: str = Field(..., min_length=6, max_length=128)


class AdminSetPointsIn(BaseModel):
    points_balance: int = Field(..., ge=0, le=999_999_999)


class AdminSetDisabledIn(BaseModel):
    disabled: bool


class AdminProxyPoolAddIn(BaseModel):
    """新增一条出站代理（如 http://user:pass@sg-ecs-ip:3128）。"""

    proxy_url: str = Field(..., min_length=8, description="完整代理 URL，含协议与端口")
    label: str = Field(default="", max_length=128)


class AdminProxyPoolRow(BaseModel):
    id: int
    proxy_url: str
    label: str
    is_active: bool
    assigned_user_id: Optional[int] = None
    assigned_username: Optional[str] = None
    proxy_host_preview: str = ""


class AdminProxyPoolListOut(BaseModel):
    entries: List[AdminProxyPoolRow]


class AdminProxyPoolPatchIn(BaseModel):
    is_active: Optional[bool] = None
    release_assigned: bool = False
    label: Optional[str] = None
    proxy_url: Optional[str] = None


class AdminUserProxyIn(BaseModel):
    """pool_entry_id 为 null 表示解除该用户绑定；否则绑定到指定池条目（须空闲或已属于该用户）。"""

    pool_entry_id: Optional[int] = None
