import hmac
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_crypto import hash_password, verify_password
from app.auth_jwt import create_admin_access_token
from app.db import get_db
from app.deps_admin import is_admin_auth_configured, require_admin
from app.models import ProxyPoolEntry, User
from app.schemas import (
    AdminCreateUserIn,
    AdminLoginIn,
    AdminProxyPoolAddIn,
    AdminProxyPoolListOut,
    AdminProxyPoolPatchIn,
    AdminProxyPoolRow,
    AdminSetDisabledIn,
    AdminSetPasswordIn,
    AdminSetPointsIn,
    AdminTokenOut,
    AdminUserListOut,
    AdminUserProxyIn,
    AdminUserRow,
    UserPublic,
)
from app.services.session_manager import normalize_proxy_url
from app.settings import settings
from app.user_registry import invalidate_user_outbound_session, remove_user_runtime

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _proxy_host_preview(url: str) -> str:
    """从代理 URL 解析 host[:port] 用于展示；与 normalize_proxy_url / SessionManager 对无协议写法的处理一致。"""
    raw = normalize_proxy_url(url)
    if not raw:
        return ""
    try:
        p = urlparse(raw)
        h = p.hostname
        port = p.port
        if h:
            if port:
                return f"{h}:{port}"
            return h
    except Exception:
        pass
    return ""


def _verify_admin_password(plain: str) -> bool:
    """优先校验 bcrypt 哈希（admin_password_hash）；否则回退明文 admin_password。"""
    h = (settings.admin_password_hash or "").strip()
    if h:
        return verify_password(plain, h)
    p = (settings.admin_password or "").strip()
    if not p:
        return False
    try:
        return hmac.compare_digest(plain.encode("utf-8"), p.encode("utf-8"))
    except Exception:
        return False


@router.post("/login", response_model=AdminTokenOut)
async def admin_login(body: AdminLoginIn) -> AdminTokenOut:
    if not is_admin_auth_configured():
        raise HTTPException(
            status_code=503,
            detail="未配置管理员（.env：admin_username + admin_password_hash 或 admin_password）",
        )
    u = body.username.strip()
    try:
        u_ok = hmac.compare_digest(u.encode("utf-8"), settings.admin_username.strip().encode("utf-8"))
    except Exception:
        raise HTTPException(status_code=401, detail="账号或密码错误")
    if not u_ok or not _verify_admin_password(body.password):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    return AdminTokenOut(access_token=create_admin_access_token())


@router.get("/proxy-pool", response_model=AdminProxyPoolListOut)
async def admin_proxy_pool_list(
    _auth: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminProxyPoolListOut:
    result = await db.execute(select(ProxyPoolEntry).order_by(ProxyPoolEntry.id.asc()))
    rows = list(result.scalars().all())
    out: List[AdminProxyPoolRow] = []
    for e in rows:
        uname: Optional[str] = None
        if e.assigned_user_id is not None:
            u = await db.get(User, e.assigned_user_id)
            uname = u.username if u else None
        out.append(
            AdminProxyPoolRow(
                id=e.id,
                proxy_url=e.proxy_url,
                label=e.label or "",
                is_active=bool(e.is_active),
                assigned_user_id=e.assigned_user_id,
                assigned_username=uname,
                proxy_host_preview=_proxy_host_preview(e.proxy_url),
            )
        )
    return AdminProxyPoolListOut(entries=out)


@router.post("/proxy-pool")
async def admin_proxy_pool_add(
    body: AdminProxyPoolAddIn,
    _auth: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    u = (body.proxy_url or "").strip()
    if not u:
        raise HTTPException(status_code=400, detail="proxy_url 不能为空")
    row = ProxyPoolEntry(proxy_url=u, label=(body.label or "").strip()[:128], is_active=True)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return {"ok": True, "id": row.id}


@router.patch("/proxy-pool/{entry_id}")
async def admin_proxy_pool_patch(
    entry_id: int,
    body: AdminProxyPoolPatchIn,
    _auth: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    entry = await db.get(ProxyPoolEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="池条目不存在")
    prev_uid: Optional[int] = None
    if body.release_assigned:
        prev_uid = entry.assigned_user_id
        entry.assigned_user_id = None
    if body.is_active is not None:
        entry.is_active = bool(body.is_active)
    if body.label is not None:
        entry.label = body.label.strip()[:128]
    if body.proxy_url is not None:
        nu = body.proxy_url.strip()
        if not nu:
            raise HTTPException(status_code=400, detail="proxy_url 不能为空")
        entry.proxy_url = nu
        if entry.assigned_user_id is not None:
            prev_uid = entry.assigned_user_id
    await db.commit()
    if prev_uid is not None:
        await invalidate_user_outbound_session(prev_uid)
    return {"ok": True}


@router.put("/users/{user_id}/proxy")
async def admin_set_user_proxy(
    user_id: int,
    body: AdminUserProxyIn,
    _auth: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    if body.pool_entry_id is None:
        r = await db.execute(select(ProxyPoolEntry).where(ProxyPoolEntry.assigned_user_id == user_id))
        for old in r.scalars().all():
            old.assigned_user_id = None
        await db.commit()
        await invalidate_user_outbound_session(user_id)
        return {"ok": True, "pool_entry_id": None}

    entry = await db.get(ProxyPoolEntry, body.pool_entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="池条目不存在")
    if not entry.is_active:
        raise HTTPException(status_code=400, detail="该代理条目已停用，请先在池中启用")
    if entry.assigned_user_id is not None and entry.assigned_user_id != user_id:
        raise HTTPException(status_code=400, detail="该代理已被其他用户占用")

    r2 = await db.execute(select(ProxyPoolEntry).where(ProxyPoolEntry.assigned_user_id == user_id))
    for old in r2.scalars().all():
        if old.id != entry.id:
            old.assigned_user_id = None
    entry.assigned_user_id = user_id
    await db.commit()
    await invalidate_user_outbound_session(user_id)
    return {"ok": True, "pool_entry_id": entry.id}


@router.get("/users", response_model=AdminUserListOut)
async def admin_list_users(
    _auth: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminUserListOut:
    result = await db.execute(select(User).order_by(User.id.asc()))
    rows: List[User] = list(result.scalars().all())
    users_out: List[AdminUserRow] = []
    for u in rows:
        pr = await db.execute(select(ProxyPoolEntry).where(ProxyPoolEntry.assigned_user_id == u.id).limit(1))
        p = pr.scalar_one_or_none()
        users_out.append(
            AdminUserRow(
                id=u.id,
                username=u.username,
                is_disabled=bool(u.is_disabled),
                points_balance=int(u.points_balance or 0),
                subscription_end_at=u.subscription_end_at,
                proxy_entry_id=p.id if p else None,
                proxy_label=(p.label or "") if p else None,
                proxy_host_preview=_proxy_host_preview(p.proxy_url) if p else None,
            )
        )
    return AdminUserListOut(users=users_out)


@router.post("/users", response_model=UserPublic)
async def admin_create_user(
    body: AdminCreateUserIn,
    _auth: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> UserPublic:
    """创建平台用户；规则与公开注册一致（试用天数 NEW_USER_TRIAL_DAYS），但不检查 registration_open。"""
    name = body.username.strip()
    if not name:
        raise HTTPException(status_code=400, detail="用户名不能为空")
    result = await db.execute(select(User).where(User.username == name))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=400, detail="用户名已存在")
    trial = max(0, int(settings.new_user_trial_days or 0))
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


@router.patch("/users/{user_id}/disabled")
async def admin_set_disabled(
    user_id: int,
    body: AdminSetDisabledIn,
    _auth: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    user.is_disabled = body.disabled
    await db.commit()
    return {"ok": True, "user_id": user_id, "disabled": body.disabled}


@router.post("/users/{user_id}/password")
async def admin_set_password(
    user_id: int,
    body: AdminSetPasswordIn,
    _auth: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    user.password_hash = hash_password(body.new_password)
    await db.commit()
    return {"ok": True, "user_id": user_id}


@router.patch("/users/{user_id}/points")
async def admin_set_points(
    user_id: int,
    body: AdminSetPointsIn,
    _auth: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    user.points_balance = body.points_balance
    await db.commit()
    await db.refresh(user)
    return {"ok": True, "user_id": user_id, "points_balance": user.points_balance}


@router.delete("/users/{user_id}")
async def admin_delete_user(
    user_id: int,
    _auth: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    await remove_user_runtime(user_id)
    await db.delete(user)
    await db.commit()
    return {"ok": True, "user_id": user_id}
