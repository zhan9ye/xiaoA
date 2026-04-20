import asyncio
import hmac
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth_crypto import hash_password, verify_password
from app.auth_jwt import create_admin_access_token
from app.db import get_db
from app.deps_admin import is_admin_auth_configured, require_admin
from app.models import AdminEcsInstanceLock, ProxyPoolEntry, User
from app.schemas import (
    AdminAliyunDeleteInstanceIn,
    AdminAliyunDeleteInstanceOut,
    AdminAliyunEcsInstanceLockIn,
    AdminAliyunEcsInstanceLockOut,
    AdminAliyunEcsInstanceRow,
    AdminAliyunEcsListOut,
    AdminAliyunEcsPoolEntryFromInstanceIn,
    AdminAliyunEcsPoolEntryFromInstanceOut,
    AdminAliyunPoolEntryAdded,
    AdminAliyunRunInstancesIn,
    AdminAliyunRunInstancesOut,
    AdminProxyPoolDeleteOut,
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
from app.services.aliyun_ecs_ops import (
    aliyun_ecs_run_configured,
    delete_instance_sync,
    describe_instances_public_ip_map_sync,
    list_ecs_instances_page_sync,
    run_instances_then_poll_public_ips_sync,
)
from app.services.session_manager import normalize_proxy_url
from app.settings import settings
from app.user_registry import invalidate_user_outbound_session, remove_user_runtime

router = APIRouter(prefix="/api/admin", tags=["admin"])


async def _pool_entry_match_for_instance(
    db: AsyncSession, instance_id: str, public_ip_hint: str
) -> Tuple[Optional[int], Optional[str]]:
    """若代理池已有对应条目，返回 (条目 id, 'label'|'proxy_url')；否则 (None, None)。"""
    iid = (instance_id or "").strip()
    if not iid:
        return None, None
    r = await db.execute(select(ProxyPoolEntry.id).where(ProxyPoolEntry.label == iid).limit(1))
    pid = r.scalar_one_or_none()
    if pid is not None:
        return int(pid), "label"
    pub = (public_ip_hint or "").strip()
    if pub:
        url = f"http://{pub}:3128"
        r2 = await db.execute(select(ProxyPoolEntry.id).where(ProxyPoolEntry.proxy_url == url).limit(1))
        pid2 = r2.scalar_one_or_none()
        if pid2 is not None:
            return int(pid2), "proxy_url"
    return None, None


async def _proxy_pool_entries_for_ecs_instance(db: AsyncSession, instance_id: str) -> List[ProxyPoolEntry]:
    """
    与指定 ECS 实例关联的代理池条目：优先 label=实例 ID（自动入池约定），否则按 DescribeInstances 公网 IP 匹配 http://IP:3128。
    """
    iid = (instance_id or "").strip()
    if not iid:
        return []
    r = await db.execute(select(ProxyPoolEntry).where(ProxyPoolEntry.label == iid))
    by_label = list(r.scalars().all())
    if by_label:
        return by_label
    try:
        ip_map = await asyncio.to_thread(describe_instances_public_ip_map_sync, [iid])
    except ValueError:
        ip_map = {}
    ip = (ip_map.get(iid) or "").strip()
    if not ip:
        return []
    proxy_url = f"http://{ip}:3128"
    r2 = await db.execute(select(ProxyPoolEntry).where(ProxyPoolEntry.proxy_url == proxy_url))
    return list(r2.scalars().all())


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


@router.delete("/proxy-pool/{entry_id}", response_model=AdminProxyPoolDeleteOut)
async def admin_proxy_pool_delete(
    entry_id: int,
    _auth: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminProxyPoolDeleteOut:
    """删除代理池条目；若已绑定用户则先解绑并失效出站会话。"""
    entry = await db.get(ProxyPoolEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="池条目不存在")
    prev_uid: Optional[int] = int(entry.assigned_user_id) if entry.assigned_user_id is not None else None
    if prev_uid is not None:
        await invalidate_user_outbound_session(prev_uid)
    await db.delete(entry)
    await db.commit()
    return AdminProxyPoolDeleteOut(ok=True, unbound_user_id=prev_uid)


@router.get("/aliyun-ecs/instances", response_model=AdminAliyunEcsListOut)
async def admin_aliyun_ecs_instances_list(
    _auth: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
) -> AdminAliyunEcsListOut:
    """当前地域 ECS 分页列表，并标注是否与出站代理池有关联。"""
    if not aliyun_ecs_run_configured():
        raise HTTPException(
            status_code=503,
            detail="未配置阿里云 ECS，无法列出实例",
        )
    try:
        raw_rows, total, rid = await asyncio.to_thread(list_ecs_instances_page_sync, page, page_size)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    locked_set: set[str] = set()
    if raw_rows:
        iids = [r["instance_id"] for r in raw_rows]
        lr = await db.execute(select(AdminEcsInstanceLock.instance_id).where(AdminEcsInstanceLock.instance_id.in_(iids)))
        locked_set = {str(x) for x in lr.scalars().all()}
    instances: List[AdminAliyunEcsInstanceRow] = []
    for row in raw_rows:
        iid = row["instance_id"]
        pid, how = await _pool_entry_match_for_instance(db, iid, row.get("public_ip") or "")
        instances.append(
            AdminAliyunEcsInstanceRow(
                instance_id=iid,
                status=row.get("status") or "",
                instance_name=row.get("instance_name") or "",
                zone_id=row.get("zone_id") or "",
                public_ip=row.get("public_ip") or "",
                locked=iid in locked_set,
                pool_entry_id=pid,
                pool_match=how,
            )
        )
    return AdminAliyunEcsListOut(page=page, page_size=page_size, total_count=total, request_id=rid, instances=instances)


@router.put("/aliyun-ecs/instance-lock", response_model=AdminAliyunEcsInstanceLockOut)
async def admin_aliyun_ecs_instance_lock(
    body: AdminAliyunEcsInstanceLockIn,
    _auth: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminAliyunEcsInstanceLockOut:
    """锁定/解锁实例：锁定后管理端「释放 ECS」将拒绝执行。"""
    iid = body.instance_id.strip()
    if not iid:
        raise HTTPException(status_code=400, detail="instance_id 不能为空")
    if body.locked:
        if await db.get(AdminEcsInstanceLock, iid) is None:
            db.add(AdminEcsInstanceLock(instance_id=iid))
        await db.commit()
        return AdminAliyunEcsInstanceLockOut(ok=True, instance_id=iid, locked=True)
    row = await db.get(AdminEcsInstanceLock, iid)
    if row is not None:
        await db.delete(row)
    await db.commit()
    return AdminAliyunEcsInstanceLockOut(ok=True, instance_id=iid, locked=False)


@router.post("/aliyun-ecs/proxy-pool-entry", response_model=AdminAliyunEcsPoolEntryFromInstanceOut)
async def admin_aliyun_ecs_add_proxy_pool_entry(
    body: AdminAliyunEcsPoolEntryFromInstanceIn,
    _auth: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminAliyunEcsPoolEntryFromInstanceOut:
    """按实例 ID 拉取公网 IP，写入代理池（label=实例 ID，proxy_url=http://IP:3128）；已有对应条目则 409。"""
    if not aliyun_ecs_run_configured():
        raise HTTPException(status_code=503, detail="未配置阿里云 ECS")
    iid = body.instance_id.strip()
    try:
        ip_map = await asyncio.to_thread(describe_instances_public_ip_map_sync, [iid])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    ip = (ip_map.get(iid) or "").strip()
    if not ip:
        raise HTTPException(status_code=400, detail="该实例当前无公网 IP 或 EIP，无法生成代理 URL")
    proxy_url = f"http://{ip}:3128"
    dup_id, _ = await _pool_entry_match_for_instance(db, iid, ip)
    if dup_id is not None:
        raise HTTPException(
            status_code=409,
            detail=f"代理池已存在对应条目（pool_entry_id={dup_id}）",
        )
    label = iid[:128]
    row = ProxyPoolEntry(proxy_url=proxy_url, label=label, is_active=True)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return AdminAliyunEcsPoolEntryFromInstanceOut(
        pool_entry_id=int(row.id),
        proxy_url=proxy_url,
        label=label,
    )


@router.post("/aliyun-ecs/run-instances", response_model=AdminAliyunRunInstancesOut)
async def admin_aliyun_ecs_run_instances(
    body: AdminAliyunRunInstancesIn,
    _auth: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminAliyunRunInstancesOut:
    """按启动模板创建 ECS（测试用）；依赖 .env 中 ALIYUN_* 与 LAUNCH_TEMPLATE。"""
    if not aliyun_ecs_run_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "未配置阿里云 ECS：请在 .env 填写 ALIYUN_ACCESS_KEY_ID、"
                "ALIYUN_ACCESS_KEY_SECRET、ALIYUN_REGION_ID、"
                "ALIYUN_ECS_LAUNCH_TEMPLATE_ID、ALIYUN_ECS_LAUNCH_TEMPLATE_VERSION"
            ),
        )
    try:
        ids, rid, ip_map = await asyncio.to_thread(run_instances_then_poll_public_ips_sync, body.amount)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    added: List[AdminAliyunPoolEntryAdded] = []
    skipped_no_ip: List[str] = []
    skipped_dup: List[str] = []

    for iid in ids:
        ip = (ip_map.get(iid) or "").strip()
        if not ip:
            skipped_no_ip.append(iid)
            continue
        proxy_url = f"http://{ip}:3128"
        dup = await db.execute(select(ProxyPoolEntry.id).where(ProxyPoolEntry.proxy_url == proxy_url).limit(1))
        if dup.scalar_one_or_none() is not None:
            skipped_dup.append(iid)
            continue
        label = iid[:128]
        row = ProxyPoolEntry(proxy_url=proxy_url, label=label, is_active=True)
        db.add(row)
        await db.flush()
        added.append(
            AdminAliyunPoolEntryAdded(
                pool_entry_id=int(row.id),
                instance_id=iid,
                proxy_url=proxy_url,
                label=label,
            )
        )

    await db.commit()
    return AdminAliyunRunInstancesOut(
        instance_ids=ids,
        request_id=rid,
        pool_entries_added=added,
        pool_skipped_no_public_ip=skipped_no_ip,
        pool_skipped_duplicate_url=skipped_dup,
    )


@router.post("/aliyun-ecs/delete-instance", response_model=AdminAliyunDeleteInstanceOut)
async def admin_aliyun_ecs_delete_instance(
    body: AdminAliyunDeleteInstanceIn,
    _auth: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminAliyunDeleteInstanceOut:
    """
    释放 ECS：1）若代理池条目已绑定用户则解绑并失效出站会话；2）删除对应代理池记录；3）调用阿里云 DeleteInstance（force）。
    """
    if not aliyun_ecs_run_configured():
        raise HTTPException(
            status_code=503,
            detail="未配置阿里云 ECS，无法调用删除接口",
        )
    iid = body.instance_id.strip()
    if await db.get(AdminEcsInstanceLock, iid) is not None:
        raise HTTPException(
            status_code=400,
            detail="该实例已锁定（主程序服务器等），禁止通过管理端释放。请先取消锁定或到阿里云控制台操作。",
        )
    entries = await _proxy_pool_entries_for_ecs_instance(db, iid)
    removed_ids: List[int] = [int(e.id) for e in entries]
    uids = sorted({int(e.assigned_user_id) for e in entries if e.assigned_user_id is not None})
    for uid in uids:
        await invalidate_user_outbound_session(uid)
    for e in entries:
        await db.delete(e)
    await db.commit()

    try:
        rid = await asyncio.to_thread(delete_instance_sync, iid)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"ECS 删除失败（代理池已按实例处理完毕，请到控制台核对）：{e}",
        ) from e
    return AdminAliyunDeleteInstanceOut(
        request_id=rid,
        removed_pool_entry_ids=removed_ids,
        unbound_user_ids=uids,
    )


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
