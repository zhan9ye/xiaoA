# 交易端 RPC 接口说明（售卖链路）

本文档描述本项目中 **登录 → 拉子账号 → Mnemonic_Get01 → ACE_Sell_Son** 四条远程接口的实际调用方式（与 `app/settings.py`、`app/rpc_v.py`、`app/services/*.py` 一致）。

## 通用约定

| 项 | 说明 |
|----|------|
| 基础域名 | 默认 `https://www.akapi1.com`，具体路径见下表；可通过环境变量覆盖 `Settings` 中的 URL。 |
| 方法 | 均为 **POST**。 |
| 正文 | **application/x-www-form-urlencoded**（`httpx` 使用 `data=dict`）。 |
| 请求头 | 见 `app/services/rpc_common.py` 中 `get_rpc_browser_headers()`：`Content-Type`、`Origin`、`Referer`、`User-Agent` 等，与浏览器抓包风格一致。 |
| 会话 | **Login / My_Subaccount / Mnemonic_Get01 / ACE_Sell_Son** 共用同一 `httpx` 客户端，依赖 **Login 返回的 Cookie** 维持会话；后续接口需在同一 Session 上调用。 |
| 版本参数 `v` | 与站点 `base.js` 中 `APP.GLOBAL.ajax` 一致：`年 + (月-1) + 日 + 时 + 分`（月同 JS 为 0～11），由 `app/rpc_v.py` 按 **Asia/Shanghai** 当前时刻在每次请求前计算。 |

---

## 1. Login（登录）

| 项 | 值 |
|----|-----|
| 默认 URL | `https://www.akapi1.com/RPC/Login`（`settings.login_url`） |
| 代码 | `app/services/login_service.py` → `rpc_login()` |

**表单字段（`data`）**

| 字段 | 说明 |
|------|------|
| `account` | 交易端登录账号 |
| `password` | 交易端密码 |
| `client` | 固定 `"WEB"` |
| `v` | 客户端版本号 |
| `lang` | 固定 `"cn"` |

**响应解析（本项目）**

- 成功时从 JSON 顶层取 **`Key`** → 存为会话 `rpc_login_key`（供子账号、Mnemonic、售卖等接口的 `key` 参数）。
- 从 **`UserData.Id`** → 存为 `rpc_user_id`（`UserID` 参数）。
- 详见 `app/services/login_response_parse.py` → `merge_from_rpc_login()`。

---

## 2. My_Subaccount（获取子账号列表）

| 项 | 值 |
|----|-----|
| 默认 URL | `https://www.akapi1.com/RPC/My_Subaccount`（`settings.subaccount_url`） |
| 代码 | `app/services/subaccount_service.py` → `post_my_subaccount_json()` / `fetch_all_subaccounts()` |

**前置条件**

- 已完成 **Login**，且具备有效的 **`rpc_login_key`**（`key`）与 **`rpc_user_id`**（`UserID`）。

**表单字段（`data`）**

| 字段 | 说明 |
|------|------|
| `p` | 页码，字符串，从 `1` 起 |
| `size` | 每页条数（`settings.subaccount_page_size`，默认 15） |
| `key` | Login 返回的会话 Key |
| `UserID` | Login 解析出的用户 ID |
| `v` | 版本号 |
| `lang` | 默认 `"cn"` |

**分页**

- `fetch_all_subaccounts()` 会按页递增 `p`，直到 `should_request_next_page()` 判断结束或达到 `subaccount_max_pages`。

**响应解析**

- 列表数据从多种 JSON 结构中抽取，见 `app/services/subaccount_parse.py` → `extract_subaccount_rows()`；单行可为字典原样保留。

---

## 3. Mnemonic_Get01

| 项 | 值 |
|----|-----|
| 默认 URL | `https://www.akapi1.com/RPC/Mnemonic_Get01`（`settings.mnemonic_get01_url`） |
| 代码 | `app/services/mnemonic_rpc_service.py` → `post_mnemonic_get01()` / `fetch_mnemonic_meta()` |

**前置条件**

- 与 **Login** 同会话 Cookie。
- 需要 **`key`**（会话 Key）、**`UserID`**。

**表单字段（`data`）**

| 字段 | 说明 |
|------|------|
| `key` | Login 返回的会话 Key |
| `UserID` | 用户 ID |
| `v` | 版本号 |
| `lang` | 默认 `"cn"` |

**成功时解析（本项目）**

- 期望 JSON 中 **`Error` 不为 true**，且存在 **`mnemonicid1`**、**`mnemonickey`**（可选 `mnemonictitle`）。
- 见 `parse_mnemonic_get01_response()`。

**售卖侧用途**

- `mnemonicid1`：与配置中 12 段助记词按序号取对应一段，得到 `mnemonicstr1`。
- `mnemonickey`：作为 `ACE_Sell_Son` 的 `mnemonickey`。

---

## 4. ACE_Sell_Son（子账号售卖）

| 项 | 值 |
|----|-----|
| 默认 URL | `https://www.akapi1.com/RPC/ACE_Sell_Son`（`settings.ace_sell_son_url`） |
| 代码 | `app/services/ace_sell_son_service.py` → `post_ace_sell_son()` |

**前置条件**

- 与 **Login** 同会话 Cookie。
- 需要会话 **`key`**、**`UserID`**；`mnemonicid1` / `mnemonickey` 通常来自 **Mnemonic_Get01**；`gCode` 由配置 **Google 共享密钥（TOTP）** 生成。

**表单字段（`data`）**

| 字段 | 说明 |
|------|------|
| `amount` | 卖出数量（字符串，与子账号 AceAmount 等一致时由业务层传入） |
| `password` | 交易端密码 |
| `sonId` | 子账号 ID |
| `mnemonicid1` | 助记词段序号（来自 Mnemonic_Get01） |
| `mnemonickey` | 助记词密钥（来自 Mnemonic_Get01） |
| `mnemonicstr1` | 由配置助记词与 `mnemonicid1` 推导的一段 4 位数字 |
| `gCode` | 6 位动态码（TOTP，由 `key_token` 计算） |
| `count` | 与业务一致的数量字符串（常与 `amount` 同源） |
| `key` | Login 会话 Key |
| `UserID` | 用户 ID |
| `v` | 版本号 |
| `lang` | 默认 `"cn"` |

**说明**

- 业务层会按运行参数（日期区间、数量限额等）筛选子账号后再调用；限流时可能返回 **HTTP 429**（含 HTML 正文），详见运行日志与 `describe_ace_sell_response()`。

---

## 定时任务中的调用顺序（参考）

运行任务（`app/services/runner.py`）大致顺序为：

1. **Login** → 更新 `rpc_user_id`、`rpc_login_key`（并持久化配置）。
2. **My_Subaccount**（翻页）→ 刷新内存中的子账号列表。
3. **Mnemonic_Get01** → 取 `mnemonicid1` / `mnemonickey`。
4. 对每条符合运行参数的子账号，依次 **ACE_Sell_Son**（并按 `request_interval_ms` 等节奏控制请求）。

---

## 配置项与 URL 的对应关系

| 环境变量 / Settings 字段 | 含义 |
|--------------------------|------|
| `login_url` | Login 地址 |
| `subaccount_url` | My_Subaccount 地址 |
| `mnemonic_get01_url` | Mnemonic_Get01 地址 |
| `ace_sell_son_url` | ACE_Sell_Son 地址 |

参数 `v` 无环境变量或用户配置项；数据库 `trading_configs.rpc_version` 列仍保留，保存配置时写入空串以兼容旧库。

## 出站代理池（User–Proxy 静态绑定）

- **表** `proxy_pool_entries`：`proxy_url`（完整 `http(s)://用户:密码@主机:端口`）、`label`、`is_active`、`assigned_user_id`（绑定到的平台用户，空闲为 `NULL`）。
- **行为**：表中**没有任何** `is_active=1` 的记录时，所有用户出站 **直连**（与旧版一致）。只要存在可用池条目，用户**首次**需要 `SessionManager`（任意 RPC）时会 **原子领取**一条空闲代理并绑定；之后进程内始终用该代理建 `httpx.AsyncClient`。
- **池满**：`PROXY_POOL_REQUIRE_AVAILABLE=true`（默认）时返回 **503**；设为 `false` 则回退直连（慎用）。
- **管理**：`GET/POST /api/admin/proxy-pool`（管理员 JWT）查看与新增池条目；删除平台用户时 `assigned_user_id` 因外键 **ON DELETE SET NULL** 自动回收。
