"""根据 HTTP method + path 生成操作日志中的简短中文业务说明。"""

from __future__ import annotations

import re
from typing import List, Tuple

# (HTTP method 大写, path 正则, 业务说明)；按顺序匹配，先匹配的优先。
_RULES: List[Tuple[str, re.Pattern, str]] = [
    ("POST", re.compile(r"^/api/auth/register$"), "注册平台账号"),
    ("POST", re.compile(r"^/api/auth/token$"), "登录获取访问令牌"),
    ("GET", re.compile(r"^/api/auth/me$"), "查看当前登录用户信息"),
    ("POST", re.compile(r"^/api/auth/change-password$"), "修改登录密码"),
    ("GET", re.compile(r"^/api/auth/site-info$"), "查看站点是否开放注册等说明"),
    ("GET", re.compile(r"^/api/credits/overview$"), "查看积分与订阅时长概览"),
    ("POST", re.compile(r"^/api/credits/preview-redeem$"), "预览积分兑换时长"),
    ("POST", re.compile(r"^/api/credits/redeem$"), "使用积分兑换订阅时长"),
    ("POST", re.compile(r"^/api/config$"), "保存交易端配置"),
    ("GET", re.compile(r"^/api/config$"), "读取交易端配置"),
    ("POST", re.compile(r"^/api/config/switch$"), "切换当前使用的交易配置槽位"),
    ("PATCH", re.compile(r"^/api/config/listing-amount$"), "调整子账号挂售数量"),
    ("GET", re.compile(r"^/api/config/run-params$"), "读取运行参数（间隔等）"),
    ("PATCH", re.compile(r"^/api/config/run-params$"), "保存运行参数"),
    ("POST", re.compile(r"^/api/trade/ace-sell-son$"), "调用 ACE 挂售子账号（RPC）"),
    ("GET", re.compile(r"^/api/trade/mnemonic-get01$"), "查询助记词元数据（RPC）"),
    ("GET", re.compile(r"^/api/subaccounts$"), "读取已缓存的子账号列表"),
    ("POST", re.compile(r"^/api/subaccounts/refresh$"), "刷新子账号列表（含必要时重新登录）"),
    ("POST", re.compile(r"^/api/external/rpc-login$"), "交易端 RPC 登录"),
    ("GET", re.compile(r"^/api/run/status$"), "查询 Runner 运行状态"),
    ("POST", re.compile(r"^/api/run/start$"), "启动自动售卖任务（Runner）"),
    ("POST", re.compile(r"^/api/run/stop$"), "停止自动售卖任务（Runner）"),
    ("POST", re.compile(r"^/api/logs/test$"), "推送一条测试日志到控制台"),
    ("POST", re.compile(r"^/api/logs/clear$"), "清空控制台日志缓冲"),
    # 管理端 /api/admin/*
    ("POST", re.compile(r"^/api/admin/login$"), "管理员登录"),
    ("GET", re.compile(r"^/api/admin/proxy-pool$"), "列出出站代理池"),
    ("POST", re.compile(r"^/api/admin/proxy-pool$"), "新增代理池条目"),
    ("PATCH", re.compile(r"^/api/admin/proxy-pool/\d+$"), "修改代理池条目（启用状态、标签、URL 等）"),
    ("DELETE", re.compile(r"^/api/admin/proxy-pool/\d+$"), "删除代理池条目"),
    ("GET", re.compile(r"^/api/admin/aliyun-ecs/instances$"), "分页列出阿里云 ECS 实例"),
    ("PUT", re.compile(r"^/api/admin/aliyun-ecs/instance-lock$"), "锁定或解锁 ECS 实例（防误删）"),
    ("POST", re.compile(r"^/api/admin/aliyun-ecs/proxy-pool-entry$"), "从 ECS 实例添加代理池条目"),
    ("POST", re.compile(r"^/api/admin/aliyun-ecs/run-instances$"), "按模板创建 ECS 实例"),
    ("POST", re.compile(r"^/api/admin/aliyun-ecs/delete-instance$"), "释放并删除 ECS 及关联代理池"),
    ("PUT", re.compile(r"^/api/admin/users/\d+/proxy$"), "为用户绑定或解绑出站代理"),
    ("GET", re.compile(r"^/api/admin/users$"), "列出平台用户"),
    ("POST", re.compile(r"^/api/admin/users$"), "管理员创建平台用户"),
    ("PATCH", re.compile(r"^/api/admin/users/\d+/disabled$"), "启用或禁用平台用户"),
    ("POST", re.compile(r"^/api/admin/users/\d+/password$"), "重置平台用户密码"),
    ("PATCH", re.compile(r"^/api/admin/users/\d+/points$"), "调整平台用户积分"),
    ("DELETE", re.compile(r"^/api/admin/users/\d+$"), "删除平台用户"),
]


def business_summary_for_request(method: str, path: str) -> str:
    """
    返回简短中文业务说明；未命中规则时用「方法 + 路径」兜底。
    """
    mu = (method or "GET").upper()
    p = (path or "").split("?", 1)[0].rstrip("/") or "/"
    for mth, pat, label in _RULES:
        if mth != mu:
            continue
        if pat.match(p):
            return label
    return f"{mu} {p}"[:240]
