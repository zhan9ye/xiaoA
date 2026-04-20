"""阿里云 ECS：管理端测试用创建/释放（按启动模板 RunInstances / DeleteInstance）。"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Tuple

from Tea.exceptions import TeaException
from alibabacloud_ecs20140526 import models as ecs_models
from alibabacloud_ecs20140526.client import Client as EcsClient
from alibabacloud_tea_openapi import models as open_api_models

from app.settings import settings


def aliyun_ecs_run_configured() -> bool:
    return bool(
        (settings.aliyun_access_key_id or "").strip()
        and (settings.aliyun_access_key_secret or "").strip()
        and (settings.aliyun_region_id or "").strip()
        and (settings.aliyun_ecs_launch_template_id or "").strip()
        and str(settings.aliyun_ecs_launch_template_version or "").strip()
    )


def _ecs_client() -> EcsClient:
    cfg = open_api_models.Config(
        access_key_id=(settings.aliyun_access_key_id or "").strip(),
        access_key_secret=(settings.aliyun_access_key_secret or "").strip(),
        region_id=(settings.aliyun_region_id or "").strip(),
    )
    return EcsClient(cfg)


def _tea_error_message(exc: TeaException) -> str:
    parts: List[str] = []
    m = getattr(exc, "message", None)
    if m:
        parts.append(str(m))
    data = getattr(exc, "data", None)
    if isinstance(data, dict):
        for k in ("Message", "message", "Recommend", "HostId", "Code"):
            if k in data and data[k]:
                parts.append(f"{k}={data[k]}")
    if not parts:
        parts.append(str(exc))
    return " | ".join(parts)[:2000]


def run_instances_from_launch_template_sync(amount: int) -> Tuple[List[str], str]:
    """
    同步调用 RunInstances；由路由层 asyncio.to_thread 包裹。
    返回 (instance_ids, request_id)。
    """
    region = (settings.aliyun_region_id or "").strip()
    lt_id = (settings.aliyun_ecs_launch_template_id or "").strip()
    lt_ver = str(settings.aliyun_ecs_launch_template_version or "").strip()
    client = _ecs_client()
    req = ecs_models.RunInstancesRequest(
        region_id=region,
        launch_template_id=lt_id,
        launch_template_version=lt_ver,
        amount=int(amount),
    )
    try:
        resp = client.run_instances(req)
    except TeaException as e:
        raise ValueError(_tea_error_message(e)) from e

    rid = ""
    ids: List[str] = []
    if resp.body:
        rid = (resp.body.request_id or "").strip()
        sets = resp.body.instance_id_sets
        if sets and sets.instance_id_set:
            ids = [str(x).strip() for x in sets.instance_id_set if str(x).strip()]
    return ids, rid


def _best_public_ip_from_instance(inst: Any) -> str:
    """DescribeInstances 单条实例：优先绑定 EIP，否则取公网 IP 列表首项。"""
    if inst is None:
        return ""
    eip = getattr(inst, "eip_address", None)
    if eip is not None:
        addr = getattr(eip, "ip_address", None) or ""
        s = str(addr).strip()
        if s:
            return s
    pub = getattr(inst, "public_ip_address", None)
    if pub is None:
        return ""
    ips = getattr(pub, "ip_address", None) or []
    if isinstance(ips, list):
        for x in ips:
            if x and str(x).strip():
                return str(x).strip()
    if isinstance(ips, str) and ips.strip():
        return ips.strip()
    return ""


def list_ecs_instances_page_sync(
    page_number: int = 1,
    page_size: int = 50,
) -> Tuple[List[Dict[str, str]], int, str]:
    """
    分页查询当前地域下 ECS 实例摘要。
    返回 (rows, total_count, request_id)；rows 每项含 instance_id, status, instance_name, zone_id, public_ip。
    """
    region = (settings.aliyun_region_id or "").strip()
    if not region:
        return [], 0, ""
    pn = max(1, int(page_number))
    ps = min(100, max(1, int(page_size)))
    client = _ecs_client()
    req = ecs_models.DescribeInstancesRequest(region_id=region, page_number=pn, page_size=ps)
    try:
        resp = client.describe_instances(req)
    except TeaException as e:
        raise ValueError(_tea_error_message(e)) from e
    rid = ""
    total = 0
    rows: List[Dict[str, str]] = []
    if not resp.body:
        return rows, total, rid
    rid = str(resp.body.request_id or "").strip()
    total = int(resp.body.total_count or 0)
    if not resp.body.instances:
        return rows, total, rid
    raw = resp.body.instances.instance
    if raw is None:
        return rows, total, rid
    insts: List[Any] = raw if isinstance(raw, list) else [raw]
    for inst in insts:
        iid = str(getattr(inst, "instance_id", None) or "").strip()
        if not iid:
            continue
        rows.append(
            {
                "instance_id": iid,
                "status": str(getattr(inst, "status", None) or ""),
                "instance_name": str(getattr(inst, "instance_name", None) or ""),
                "zone_id": str(getattr(inst, "zone_id", None) or ""),
                "public_ip": _best_public_ip_from_instance(inst),
            }
        )
    return rows, total, rid


def describe_instances_public_ip_map_sync(instance_ids: List[str]) -> Dict[str, str]:
    """
    单次 DescribeInstances，返回 instance_id -> 公网出口 IP（EIP 优先）。
    未返回或尚无 IP 的实例不会出现在 dict 中。
    """
    ids = [str(x).strip() for x in instance_ids if str(x).strip()]
    if not ids:
        return {}
    region = (settings.aliyun_region_id or "").strip()
    client = _ecs_client()
    req = ecs_models.DescribeInstancesRequest(region_id=region, instance_ids=json.dumps(ids))
    try:
        resp = client.describe_instances(req)
    except TeaException as e:
        raise ValueError(_tea_error_message(e)) from e
    out: Dict[str, str] = {}
    if not resp.body or not resp.body.instances:
        return out
    raw = resp.body.instances.instance
    if raw is None:
        return out
    insts: List[Any] = raw if isinstance(raw, list) else [raw]
    for inst in insts:
        iid = str(getattr(inst, "instance_id", None) or "").strip()
        if not iid:
            continue
        ip = _best_public_ip_from_instance(inst)
        if ip:
            out[iid] = ip
    return out


def poll_instance_public_ips_sync(
    instance_ids: List[str],
    *,
    timeout_sec: float = 120.0,
    interval_sec: float = 4.0,
) -> Dict[str, str]:
    """
    轮询直至每台实例都有公网出口 IP，或超时。
    在 asyncio.to_thread 中调用，避免阻塞事件循环。
    """
    want = [str(x).strip() for x in instance_ids if str(x).strip()]
    if not want:
        return {}
    deadline = time.monotonic() + max(5.0, float(timeout_sec))
    found: Dict[str, str] = {}
    remaining = set(want)
    while remaining and time.monotonic() < deadline:
        batch = describe_instances_public_ip_map_sync(list(remaining))
        for iid, ip in batch.items():
            if iid in remaining and ip:
                found[iid] = ip
                remaining.discard(iid)
        if remaining:
            time.sleep(max(1.0, float(interval_sec)))
    return found


def run_instances_then_poll_public_ips_sync(amount: int) -> Tuple[List[str], str, Dict[str, str]]:
    """创建实例后轮询公网 IP；供管理端一次线程调用。"""
    ids, rid = run_instances_from_launch_template_sync(amount)
    if not ids:
        return ids, rid, {}
    ip_map = poll_instance_public_ips_sync(ids)
    return ids, rid, ip_map


def delete_instance_sync(instance_id: str) -> str:
    """同步 DeleteInstance(force)；返回 request_id。"""
    iid = (instance_id or "").strip()
    if not iid:
        raise ValueError("instance_id 不能为空")
    client = _ecs_client()
    req = ecs_models.DeleteInstanceRequest(instance_id=iid, force=True)
    try:
        resp = client.delete_instance(req)
    except TeaException as e:
        raise ValueError(_tea_error_message(e)) from e
    if resp.body and resp.body.request_id:
        return str(resp.body.request_id).strip()
    return ""
