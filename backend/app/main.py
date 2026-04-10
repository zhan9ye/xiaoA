import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import jwt
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_crypto import hash_password, verify_password
from app.admin_routes import router as admin_router
from app.auth_jwt import create_access_token
from app.db import AsyncSessionLocal, get_db, init_db
from app.deps_auth import get_current_user, require_active_subscription
from app.models import TradingConfig, User
from app.schemas import (
    AceSellSonIn,
    AceSellSonOut,
    AppConfigFormIn,
    AppConfigIn,
    AppConfigOut,
    ListingAmountPatchIn,
    LoginResult,
    MnemonicGet01Out,
    RunParamsFormIn,
    RunParamsOut,
    RunStatus,
    SubaccountsOut,
    CreditPackageOut,
    CreditsOverviewOut,
    RedeemDaysIn,
    RedeemDaysOut,
    RedeemPreviewOut,
    TokenOut,
    UserLoginIn,
    UserPublic,
    UserRegisterIn,
)
from app.services.ace_sell_son_service import (
    describe_ace_sell_response,
    post_ace_sell_son,
    resolve_count_from_subaccounts,
)
from app.services.mnemonic_rpc_service import (
    fetch_mnemonic_meta,
    parse_mnemonic_get01_response,
    post_mnemonic_get01,
)
from app.services.mnemonic_segments import derive_mnemonic_str1
from app.services.log_hub import LogLevel
from app.services.login_response_parse import merge_from_rpc_login
from app.services.login_service import rpc_login
from app.services.beijing_time import beijing_today_str, timed_sell_past_grace_deadline
from app.services.runner import run_background
from app.services.subaccount_service import FetchSubaccountsOutcome, fetch_all_subaccounts
from app.services.totp_util import totp_now_from_secret_ex
from app.settings import settings
from app.services.credits_service import (
    CREDIT_PACKAGES,
    compute_redeem_end_at,
    packages_public,
    redeem_days,
    subscription_active,
    subscription_expired,
)
from app.services.global_floor import get_floor_controller
from app.services.selling_eligibility import (
    effective_listing_amount_str,
    enrich_subaccounts_with_listing_qty,
    listing_amounts_for_api,
)
from app.middleware_request_log import http_request_log_file_ok, setup_request_file_logger
from app.proxy_binding import get_session_manager_for_user_id
from app.rpc_v import compute_js_timespan_v
from app.trading_config_repo import ensure_trading_config_loaded, load_trading_config, persist_trading_config
from app.user_registry import get_or_create_log_hub, get_or_create_state, shutdown_all


def _trading_password_for_api(pw: str) -> str:
    """与 key_token 一样明文回显；库内空密码在内存中用单空格占位，不把占位符返回前端。"""
    if not pw:
        return ""
    if pw == " ":
        return ""
    return pw


def _run_status_timed_sell_flags(st) -> Tuple[bool, bool]:
    """(timed_sell_internal_only_today, timed_sell_would_skip_outbound_if_started)。"""
    today = beijing_today_str()
    running = st.runner_task is not None and not st.runner_task.done()
    internal_only = running and (st.runner_late_start_skip_outbound_today or "").strip() == today
    would_skip = False
    if not running and st.config and (st.config.sell_start_time or "").strip():
        g = max(0, int(settings.sell_start_missed_grace_minutes or 10))
        would_skip = timed_sell_past_grace_deadline(st.config.sell_start_time, g)
    return internal_only, would_skip


def _apply_timed_sell_late_start_skip_flag(st, cfg: Optional[AppConfigIn]) -> None:
    """
    配置了 sell_start_time 且当前已超过「开售整点 + sell_start_missed_grace_minutes」时，
    标记本北京日仅内部等待，runner 不调登录/子账号/助记词/售卖等对外接口。
    """
    if cfg is None or not (cfg.sell_start_time or "").strip():
        st.runner_late_start_skip_outbound_today = ""
        return
    g = max(0, int(settings.sell_start_missed_grace_minutes or 10))
    if timed_sell_past_grace_deadline(cfg.sell_start_time, g):
        st.runner_late_start_skip_outbound_today = beijing_today_str()
    else:
        st.runner_late_start_skip_outbound_today = ""


async def _resume_runner_tasks() -> None:
    """进程启动后恢复 runner_enabled=true 的任务。"""
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        r = await session.execute(select(TradingConfig.user_id).where(TradingConfig.runner_enabled.is_(True)))
        uids = [row[0] for row in r.all()]
    for uid in uids:
        async with AsyncSessionLocal() as session:
            urow = await session.get(User, uid)
            if urow is not None and subscription_expired(urow):
                continue
        st = await get_or_create_state(uid)
        if st.runner_task is not None and not st.runner_task.done():
            continue
        async with AsyncSessionLocal() as session:
            cfg = await load_trading_config(session, uid)
        if cfg is None:
            continue
        st.config = cfg
        st.stop_event = asyncio.Event()
        st.runner_must_refresh_trading_cache = True
        _apply_timed_sell_late_start_skip_flag(st, cfg)
        st.runner_task = asyncio.create_task(run_background(uid, cfg))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    if settings.jwt_secret == "dev-change-me":
        print("WARNING: 使用默认 JWT_SECRET，公网部署请在 .env 设置强随机 jwt_secret")
    if settings.request_log_enabled and (settings.request_log_outbound_hosts or "").strip():
        setup_request_file_logger()
        if http_request_log_file_ok():
            print(
                "出站 HTTP 日志：已启用（仅匹配 request_log_outbound_hosts），见 request_log_dir / http_requests.log"
            )
    await _resume_runner_tasks()
    yield
    await shutdown_all()
    from app.db import engine

    await engine.dispose()


app = FastAPI(title="控制台", lifespan=lifespan)
app.include_router(admin_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
@app.get("/api/health")
async def health():
    return {"ok": True}


@app.post("/api/auth/register", response_model=UserPublic)
async def register(body: UserRegisterIn, db: AsyncSession = Depends(get_db)):
    if not settings.registration_open:
        raise HTTPException(status_code=403, detail="注册已关闭，请联系管理员")
    name = body.username.strip()
    result = await db.execute(select(User).where(User.username == name))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=400, detail="用户名已存在")
    trial = settings.new_user_trial_days
    sub_end = None
    if trial > 0:
        sub_end = datetime.now(timezone.utc) + timedelta(days=trial)
    user = User(
        username=name,
        password_hash=hash_password(body.password),
        is_disabled=False,
        points_balance=0,
        subscription_end_at=sub_end,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return UserPublic(id=user.id, username=user.username)


@app.post("/api/auth/token", response_model=TokenOut)
async def login_token(body: UserLoginIn, db: AsyncSession = Depends(get_db)):
    name = body.username.strip()
    result = await db.execute(select(User).where(User.username == name))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    token = create_access_token(user.id)
    return TokenOut(access_token=token)


@app.get("/api/auth/me", response_model=UserPublic)
async def me(user: User = Depends(get_current_user)):
    return UserPublic(id=user.id, username=user.username)


@app.get("/api/credits/overview", response_model=CreditsOverviewOut)
async def credits_overview(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """积分、订阅状态与可兑换套餐（未开通或过期时仍可调用，用于兑换）。"""
    await db.refresh(user, attribute_names=["points_balance", "subscription_end_at"])
    pkgs = [CreditPackageOut(**row) for row in packages_public()]
    return CreditsOverviewOut(
        points_balance=int(user.points_balance or 0),
        subscription_end_at=user.subscription_end_at,
        subscription_active=subscription_active(user),
        packages=pkgs,
    )


@app.post("/api/credits/preview-redeem", response_model=RedeemPreviewOut)
async def credits_preview_redeem(
    body: RedeemDaysIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """确认弹窗用：预览兑换后的订阅结束时间（与真实 redeem 计算一致，不扣积分）。"""
    await db.refresh(user, attribute_names=["points_balance", "subscription_end_at"])
    cost = CREDIT_PACKAGES.get(body.days)
    if cost is None:
        raise HTTPException(status_code=400, detail=f"不支持的天数套餐：{body.days}")
    end_at = compute_redeem_end_at(user, body.days)
    return RedeemPreviewOut(subscription_end_at=end_at, points_cost=cost)


@app.post("/api/credits/redeem", response_model=RedeemDaysOut)
async def credits_redeem(
    body: RedeemDaysIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        u, spent = await redeem_days(db, user.id, body.days)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedeemDaysOut(
        points_balance=u.points_balance,
        subscription_end_at=u.subscription_end_at,
        redeemed_days=body.days,
        points_spent=spent,
    )


@app.post("/api/config")
async def save_config(
    body: AppConfigFormIn,
    user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
) -> AppConfigOut:
    st = await get_or_create_state(user.id)
    await ensure_trading_config_loaded(db, user.id, st)
    prev = st.config
    new_pw = (body.password or "").strip()
    if not new_pw:
        if prev and prev.password:
            new_pw = prev.password
        else:
            raise HTTPException(status_code=400, detail="首次保存请填写交易密码；之后可留空保留原密码")
    new_key = (body.key_token or "").strip()
    if not new_key and prev and prev.key_token:
        new_key = prev.key_token
    new_mnemonic = (body.mnemonic or "").strip()
    if not new_mnemonic and prev and prev.mnemonic:
        new_mnemonic = prev.mnemonic
    new_rps = body.run_period_start
    new_rpe = body.run_period_end
    if prev:
        if new_rps == "" and prev.run_period_start:
            new_rps = prev.run_period_start
        if new_rpe == "" and prev.run_period_end:
            new_rpe = prev.run_period_end
    new_sell = body.sell_start_time
    if prev and not (new_sell or "").strip() and (prev.sell_start_time or "").strip():
        new_sell = prev.sell_start_time
    ri = max(500, int(body.request_interval_ms or 1000))
    st.config = AppConfigIn(
        username=body.username.strip(),
        password=new_pw,
        mnemonic=new_mnemonic,
        quantity_start_limit=body.quantity_start_limit,
        request_interval_ms=ri,
        run_period_start=new_rps,
        run_period_end=new_rpe,
        key_token=new_key,
        rpc_login_key=prev.rpc_login_key if prev else "",
        rpc_user_id=prev.rpc_user_id if prev else "",
        runner_enabled=bool(prev.runner_enabled) if prev else False,
        sell_start_time=new_sell or "",
        sold_son_ids_json=(prev.sold_son_ids_json if prev else None) or "{}",
        listing_amounts_json=(prev.listing_amounts_json if prev else None) or "{}",
    )
    await persist_trading_config(db, user.id, st.config)
    c = st.config
    return AppConfigOut(
        username=c.username,
        password=_trading_password_for_api(c.password),
        key_token=c.key_token,
        mnemonic=c.mnemonic,
        quantity_start_limit=c.quantity_start_limit,
        request_interval_ms=c.request_interval_ms,
        run_period_start=c.run_period_start,
        run_period_end=c.run_period_end,
        sell_start_time=c.sell_start_time or "",
        listing_amounts=listing_amounts_for_api(c),
    )


@app.get("/api/config")
async def get_config(user: User = Depends(require_active_subscription), db: AsyncSession = Depends(get_db)):
    st = await get_or_create_state(user.id)
    await ensure_trading_config_loaded(db, user.id, st)
    if st.config is None:
        return {}
    c = st.config
    return AppConfigOut(
        username=c.username,
        password=_trading_password_for_api(c.password),
        key_token=c.key_token,
        mnemonic=c.mnemonic,
        quantity_start_limit=c.quantity_start_limit,
        request_interval_ms=c.request_interval_ms,
        run_period_start=c.run_period_start,
        run_period_end=c.run_period_end,
        sell_start_time=c.sell_start_time or "",
        listing_amounts=listing_amounts_for_api(c),
    )


@app.patch("/api/config/listing-amount")
async def patch_listing_amount(
    body: ListingAmountPatchIn,
    user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
):
    """设置单个子账号挂售数量；amount 为空则恢复为全部股数。写入数据库并更新内存中的 trading config。"""
    st = await get_or_create_state(user.id)
    if not await ensure_trading_config_loaded(db, user.id, st) or st.config is None:
        raise HTTPException(status_code=400, detail="请先保存交易配置")
    cfg = st.config
    sid = body.son_id.strip()
    amt = (body.amount or "").strip().replace(",", "")
    try:
        m = json.loads((cfg.listing_amounts_json or "").strip() or "{}")
        if not isinstance(m, dict):
            m = {}
    except json.JSONDecodeError:
        m = {}
    if not amt:
        m.pop(sid, None)
        for k in list(m.keys()):
            if str(k).strip() == sid:
                del m[k]
    else:
        if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", amt):
            raise HTTPException(status_code=400, detail="挂售数量须为数字")
        try:
            if float(amt) <= 0:
                raise HTTPException(status_code=400, detail="挂售数量须大于 0")
        except HTTPException:
            raise
        except ValueError:
            raise HTTPException(status_code=400, detail="挂售数量须为有效数字")
        full = resolve_count_from_subaccounts(list(st.subaccounts_cache or []), sid)
        if full:
            try:
                full_n = float(str(full).replace(",", "").strip())
                if float(amt) > full_n + 1e-9:
                    raise HTTPException(status_code=400, detail="挂售数量不能大于当前股数")
            except HTTPException:
                raise
            except ValueError:
                pass
        m[sid] = amt
    new_json = json.dumps(m, ensure_ascii=False, separators=(",", ":"))
    new_cfg = cfg.model_copy(update={"listing_amounts_json": new_json})
    st.config = new_cfg
    await persist_trading_config(db, user.id, new_cfg)
    return {"ok": True, "listing_amounts": listing_amounts_for_api(new_cfg)}


@app.get("/api/config/run-params", response_model=RunParamsOut)
async def get_run_params(user: User = Depends(require_active_subscription), db: AsyncSession = Depends(get_db)):
    """从数据库读取运行参数（明文列，无需解密）；无记录时返回与前端一致的默认。"""
    row = await db.get(TradingConfig, user.id)
    if row is None:
        return RunParamsOut(
            quantity_start_limit=1000,
            request_interval_ms=1000,
            run_period_start="",
            run_period_end="",
            sell_start_time="12:00",
        )
    ri = int(row.request_interval_ms or 1000)
    if ri < 500:
        ri = 500
    return RunParamsOut(
        quantity_start_limit=int(row.quantity_start_limit or 0),
        request_interval_ms=ri,
        run_period_start=row.run_period_start or "",
        run_period_end=row.run_period_end or "",
        sell_start_time=(getattr(row, "sell_start_time", None) or "") or "",
    )


@app.patch("/api/config/run-params", response_model=RunParamsOut)
async def patch_run_params(
    body: RunParamsFormIn,
    user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
):
    """仅更新运行参数；须已至少完整保存过一次交易端配置。"""
    st = await get_or_create_state(user.id)
    if not await ensure_trading_config_loaded(db, user.id, st) or st.config is None:
        raise HTTPException(
            status_code=400,
            detail="请先使用「保存配置」保存一次交易端配置，再保存运行参数",
        )
    prev = st.config
    st.config = prev.model_copy(
        update={
            "quantity_start_limit": body.quantity_start_limit,
            "request_interval_ms": max(500, int(body.request_interval_ms or 1000)),
            "run_period_start": body.run_period_start,
            "run_period_end": body.run_period_end,
            "sell_start_time": body.sell_start_time or "",
        }
    )
    await persist_trading_config(db, user.id, st.config)
    c = st.config
    return RunParamsOut(
        quantity_start_limit=c.quantity_start_limit,
        request_interval_ms=c.request_interval_ms,
        run_period_start=c.run_period_start,
        run_period_end=c.run_period_end,
        sell_start_time=c.sell_start_time or "",
    )


@app.post("/api/trade/ace-sell-son", response_model=AceSellSonOut)
async def trade_ace_sell_son(
    body: AceSellSonIn,
    user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
):
    """
    调用 RPC ACE_Sell_Son。须已保存配置且会话内完成过 Login（与拉子账号同源 Cookie）。
    g_code 可省略：用配置 key_token（Google 共享密钥）按 pyotp TOTP 现算 6 位 gCode。
    """
    st = await get_or_create_state(user.id)
    if not await ensure_trading_config_loaded(db, user.id, st):
        raise HTTPException(status_code=400, detail="请先保存交易配置")
    cfg = st.config
    g = (body.g_code or "").strip()
    if not g:
        g, g_err = totp_now_from_secret_ex(cfg.key_token)
        if not g:
            raise HTTPException(
                status_code=400,
                detail=g_err
                or "缺少 gCode：请传入 g_code，或在配置中填写 Google 共享密钥（key_token）以 TOTP 现算",
            )
    son = body.son_id.strip()
    count = (body.count or "").strip()
    count_auto = False
    if not count:
        resolved = resolve_count_from_subaccounts(list(st.subaccounts_cache or []), son)
        if resolved:
            count = effective_listing_amount_str(cfg, son, resolved)
            count_auto = True
    if not count:
        raise HTTPException(
            status_code=400,
            detail="缺少 count：请在请求中传入，或先保存配置以拉取子账号并从 AceAmount 解析",
        )
    amount = (body.amount or "").strip()
    if count_auto:
        amount = count
    uid = cfg.rpc_user_id.strip()
    rk = cfg.rpc_login_key.strip()
    ver_rpc = compute_js_timespan_v()
    if not uid or not rk:
        raise HTTPException(status_code=400, detail="缺少 UserID 或会话 key，请先保存配置并完成 RPC Login")
    sm = await get_session_manager_for_user_id(user.id)
    mkey = (body.mnemonic_key or "").strip()
    mid1_input = (body.mnemonic_id1 or "").strip()
    if not mkey:
        meta = await fetch_mnemonic_meta(sm, rpc_key=rk, user_id=uid, v=ver_rpc)
        if not meta:
            raise HTTPException(
                status_code=400,
                detail="Mnemonic_Get01 未返回 mnemonickey：请先保存配置完成 Login（会话 Cookie），或手动传入 mnemonic_key",
            )
        mkey = meta["mnemonickey"]
        mid1 = mid1_input or meta["mnemonicid1"]
    else:
        mid1 = mid1_input or "1"
    mstr = (body.mnemonic_str1 or "").strip()
    if not mstr:
        mstr = derive_mnemonic_str1(cfg.mnemonic, mid1) or ""
        if not mstr:
            raise HTTPException(
                status_code=400,
                detail="mnemonicstr1：请在请求中传入，或确保「助记词/备注」为至少 12 段逗号分隔数字且 mnemonic_id1 为 1～12",
            )
    ok, code, parsed, raw_out = await post_ace_sell_son(
        sm,
        amount=amount,
        password=cfg.password,
        son_id=son,
        mnemonic_id1=mid1,
        mnemonic_key=mkey,
        mnemonic_str1=mstr,
        g_code=g,
        count=count,
        rpc_key=rk,
        user_id=uid,
        v=ver_rpc,
    )
    detail = describe_ace_sell_response(code, parsed, raw_out)
    msg = f"HTTP {code} {detail}"
    data = parsed if isinstance(parsed, dict) else None
    return AceSellSonOut(ok=ok, status_code=code, message=msg[:800], data=data)


@app.get("/api/trade/mnemonic-get01", response_model=MnemonicGet01Out)
async def trade_mnemonic_get01(
    user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
):
    """单独调用 RPC Mnemonic_Get01，返回 mnemonicid1 / mnemonickey（须已完成 Login 的会话）。"""
    st = await get_or_create_state(user.id)
    if not await ensure_trading_config_loaded(db, user.id, st):
        raise HTTPException(status_code=400, detail="请先保存交易配置")
    cfg = st.config
    uid = cfg.rpc_user_id.strip()
    rk = cfg.rpc_login_key.strip()
    ver = compute_js_timespan_v()
    if not uid or not rk:
        raise HTTPException(status_code=400, detail="缺少 UserID 或会话 key，请先保存配置并完成 RPC Login")
    sm = await get_session_manager_for_user_id(user.id)
    ok, code, parsed, raw = await post_mnemonic_get01(sm, rpc_key=rk, user_id=uid, v=ver)
    meta = parse_mnemonic_get01_response(parsed)
    if meta:
        return MnemonicGet01Out(
            ok=ok,
            status_code=code,
            mnemonicid1=meta["mnemonicid1"],
            mnemonickey=meta["mnemonickey"],
            mnemonictitle=meta["mnemonictitle"],
        )
    tail = raw if isinstance(raw, str) else str(parsed)
    return MnemonicGet01Out(
        ok=ok,
        status_code=code,
        raw_message=(tail or "")[:4000],
    )


@app.get("/api/subaccounts", response_model=SubaccountsOut)
async def list_subaccounts(
    user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
):
    st = await get_or_create_state(user.id)
    await ensure_trading_config_loaded(db, user.id, st)
    items = list(st.subaccounts_cache or [])
    if st.config is not None:
        items = enrich_subaccounts_with_listing_qty(items, st.config)
    return SubaccountsOut(count=len(items), items=items)


@app.post("/api/subaccounts/refresh", response_model=SubaccountsOut)
async def refresh_subaccounts(
    user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
):
    """已登录则复用 Cookie 拉取子账号；未登录或会话失效(401)时先 RPC Login 再拉取。"""
    st = await get_or_create_state(user.id)
    if not await ensure_trading_config_loaded(db, user.id, st):
        raise HTTPException(status_code=400, detail="请先保存交易配置")
    cfg = st.config
    if cfg is None:
        raise HTTPException(status_code=400, detail="请先保存交易配置")
    sm = await get_session_manager_for_user_id(user.id)

    rk = cfg.rpc_login_key.strip()
    uid = cfg.rpc_user_id.strip()
    reuse_session = bool(st.logged_in and rk and uid)

    async def fetch_subs(key: str, user_id: str) -> FetchSubaccountsOutcome:
        v_sub = compute_js_timespan_v()
        return await fetch_all_subaccounts(
            sm,
            key=key,
            user_id=user_id,
            v=v_sub,
            page_size=settings.subaccount_page_size,
            max_pages=settings.subaccount_max_pages,
            log_push=None,
            silent=True,
        )

    sub_out = None
    if reuse_session:
        sub_out = await fetch_subs(rk, uid)
        if not sub_out.first_page_ok and (
            sub_out.first_page_status_code == 401 or sub_out.not_logged_in
        ):
            st.logged_in = False
            sub_out = None
        # 其它错误码仍返回已拿到的列表（可能为空），不强制重登

    if sub_out is None:
        v_login = compute_js_timespan_v()
        login_res = await rpc_login(sm, cfg.username, cfg.password, v=v_login)
        if not login_res.ok:
            raise HTTPException(
                status_code=400,
                detail=(login_res.message or f"登录失败 HTTP {login_res.status_code}")[:500],
            )
        merged, _ = merge_from_rpc_login(cfg, login_res.response_body)
        st.config = merged
        st.logged_in = True
        await persist_trading_config(db, user.id, merged)
        cfg = merged
        rk = merged.rpc_login_key.strip()
        uid = merged.rpc_user_id.strip()
        if not rk or not uid:
            raise HTTPException(status_code=400, detail="Login 未解析出 Key/UserID，无法拉取子账号")
        sub_out = await fetch_subs(rk, uid)

    items = sub_out.items
    st.subaccounts_cache = list(items)
    cfg_out = st.config
    enriched = enrich_subaccounts_with_listing_qty(list(items), cfg_out) if cfg_out is not None else list(items)
    return SubaccountsOut(count=len(enriched), items=enriched)


@app.post("/api/external/rpc-login")
async def external_rpc_login(
    user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
) -> LoginResult:
    st = await get_or_create_state(user.id)
    if not await ensure_trading_config_loaded(db, user.id, st):
        raise HTTPException(status_code=400, detail="请先保存配置")
    sm = await get_session_manager_for_user_id(user.id)
    ver = compute_js_timespan_v()
    res = await rpc_login(sm, st.config.username, st.config.password, v=ver)
    st.logged_in = res.ok
    return res


@app.get("/api/run/status")
async def run_status(user: User = Depends(require_active_subscription), db: AsyncSession = Depends(get_db)) -> RunStatus:
    st = await get_or_create_state(user.id)
    await ensure_trading_config_loaded(db, user.id, st)
    running = st.runner_task is not None and not st.runner_task.done()
    fl = get_floor_controller(user.id)
    floor_ms, sr429, nwin = fl.snapshot()
    enabled = bool(st.config.runner_enabled) if st.config else False
    tio, tws = _run_status_timed_sell_flags(st)
    return RunStatus(
        running=running,
        last_error=st.last_runner_error,
        runner_enabled=enabled,
        floor_curr_ms=floor_ms,
        sr429_window=sr429,
        window_samples=nwin,
        timed_sell_internal_only_today=tio,
        timed_sell_would_skip_outbound_if_started=tws,
    )


@app.post("/api/run/start")
async def run_start(
    user: User = Depends(require_active_subscription),
    db: AsyncSession = Depends(get_db),
) -> RunStatus:
    st = await get_or_create_state(user.id)
    if not await ensure_trading_config_loaded(db, user.id, st):
        raise HTTPException(status_code=400, detail="请先保存配置")
    if st.runner_task is not None and not st.runner_task.done():
        fl = get_floor_controller(user.id)
        fm, sr429, nwin = fl.snapshot()
        tio, tws = _run_status_timed_sell_flags(st)
        return RunStatus(
            running=True,
            last_error=st.last_runner_error,
            runner_enabled=bool(st.config.runner_enabled) if st.config else False,
            floor_curr_ms=fm,
            sr429_window=sr429,
            window_samples=nwin,
            timed_sell_internal_only_today=tio,
            timed_sell_would_skip_outbound_if_started=tws,
        )

    cfg = st.config
    if cfg is None:
        raise HTTPException(status_code=400, detail="请先保存配置")
    st.config = cfg.model_copy(update={"runner_enabled": True})
    await persist_trading_config(db, user.id, st.config)
    st.stop_event = asyncio.Event()
    st.runner_must_refresh_trading_cache = True
    _apply_timed_sell_late_start_skip_flag(st, st.config)
    st.runner_task = asyncio.create_task(run_background(user.id, st.config))
    fl = get_floor_controller(user.id)
    fm, sr429, nwin = fl.snapshot()
    tio, tws = _run_status_timed_sell_flags(st)
    return RunStatus(
        running=True,
        last_error=None,
        runner_enabled=True,
        floor_curr_ms=fm,
        sr429_window=sr429,
        window_samples=nwin,
        timed_sell_internal_only_today=tio,
        timed_sell_would_skip_outbound_if_started=tws,
    )


@app.post("/api/run/stop")
async def run_stop(user: User = Depends(require_active_subscription), db: AsyncSession = Depends(get_db)) -> RunStatus:
    st = await get_or_create_state(user.id)
    await ensure_trading_config_loaded(db, user.id, st)
    if st.config is not None:
        st.config = st.config.model_copy(update={"runner_enabled": False})
        await persist_trading_config(db, user.id, st.config)
    st.stop_event.set()
    if st.runner_task is not None and not st.runner_task.done():
        st.runner_task.cancel()
        try:
            await st.runner_task
        except asyncio.CancelledError:
            pass
    st.runner_task = None
    fl = get_floor_controller(user.id)
    fm, sr429, nwin = fl.snapshot()
    tio, tws = _run_status_timed_sell_flags(st)
    return RunStatus(
        running=False,
        last_error=st.last_runner_error,
        runner_enabled=False,
        floor_curr_ms=fm,
        sr429_window=sr429,
        window_samples=nwin,
        timed_sell_internal_only_today=tio,
        timed_sell_would_skip_outbound_if_started=tws,
    )


@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket):
    token = ws.query_params.get("token")
    if not token:
        await ws.close(code=4401)
        return
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        user_id = int(payload["sub"])
    except jwt.PyJWTError:
        await ws.close(code=4401)
        return

    async with AsyncSessionLocal() as session:
        urow = await session.get(User, user_id)
        if urow is None or subscription_expired(urow):
            await ws.close(code=4403)
            return

    hub = await get_or_create_log_hub(user_id)
    await hub.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        await hub.disconnect(ws)


@app.post("/api/logs/test")
async def log_test(user: User = Depends(require_active_subscription)):
    hub = await get_or_create_log_hub(user.id)
    await hub.push(LogLevel.success, "配置加载成功（测试）")
    return {"ok": True}


@app.post("/api/logs/clear")
async def logs_clear(user: User = Depends(require_active_subscription)):
    """清空当前用户在服务端的日志缓冲（与前端「清空日志」配合，避免重连时旧日志回放）。"""
    hub = await get_or_create_log_hub(user.id)
    await hub.clear_history()
    return {"ok": True}
