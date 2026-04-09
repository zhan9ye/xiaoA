# 部署文档

本文说明如何在 Linux 服务器（或同类环境）上部署「控制台」项目：FastAPI 后端 + Vue 前端，面向公网访问时的推荐做法与安全注意点。

## 1. 架构说明

| 组件 | 说明 |
|------|------|
| 后端 | Python 3.9+，`uvicorn` 运行 `app.main:app`，默认监听 `127.0.0.1:8000`（建议仅本机，由 Nginx 对外） |
| 前端 | `vite build` 产出静态文件，由 Nginx 托管；`/api`、`/ws` 反代到后端 |
| 数据库 | SQLite，默认文件为 `backend/data/app.db` |
| 认证 | JWT；用户与密码哈希存在 SQLite |
| 订阅与积分 | `users` 表含 `subscription_end_at`、`points_balance`；`subscription_end_at` 为空表示**未开通时长**（不可用控制台核心能力）；非空且未到期为有效；积分兑换见 `POST /api/credits/redeem` |
| 管理后台 | 配置 `ADMIN_USERNAME` + `ADMIN_PASSWORD_HASH`（或明文密码）后开放 `GET/POST /api/admin/*`，前端一般为 `/#/admin`（列表用户、改密、调积分、禁用/删除等，**不含**「创建新用户」接口） |

**重要限制（部署前必读）：**

- 运行时配置、任务状态、第三方 RPC 的 Cookie 会话等仍在**进程内存**中，**重启进程会丢失**；多副本（多 Worker）之间**不共享**这些状态。若需多机/多进程一致会话，需后续引入 Redis 等，并改造存储层。
- **售卖 runner 的 floor**（见下文 4.5）同样只在**单进程内**按用户维护，**重启后从默认下限重新累计**；多 Worker 下各进程 floor 互不一致，属当前设计。
- **SQLite** 不适合高并发写入；单 Worker 部署最简单。若使用 Gunicorn 多 Worker，请改用 PostgreSQL 等并评估会话设计。
- **进程启动**时会执行 `init_db()`：自动 `create_all`，并对已有 SQLite 库做**增量列**补齐（见 `app/db.py`）。升级代码后一般只需**重启后端**，无需单独跑迁移命令。

## 2. 服务器环境要求

- Python 3.9 或以上（推荐 3.11+）
- Node.js 18+（仅构建前端时需要）
- Nginx（或其它支持 WebSocket 的反代）
- 可选：Certbot 等，用于 HTTPS 证书

## 3. 目录与代码

将仓库放到服务器，例如：

```bash
sudo mkdir -p /opt/xiaoA
sudo chown $USER:$USER /opt/xiaoA
# 将项目同步到 /opt/xiaoA
```

下文假设项目根目录为 `/opt/xiaoA`，即包含 `backend/` 与 `frontend/`。

## 4. 后端部署

### 4.1 虚拟环境与依赖

```bash
cd /opt/xiaoA/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

### 4.2 环境变量

在 `backend` 目录创建 `.env`（不要提交到版本库）：

```env
# 必填：使用 openssl 等生成足够长的随机串
JWT_SECRET=此处替换为长随机密钥

# 公网强烈建议关闭自助注册，由管理员在库中预置用户或后续做管理接口
REGISTRATION_OPEN=false

# 后台管理（/api/admin/*；前端路径一般为 /#/admin）。不配用户名或密码则管理接口不可用。
# ADMIN_USERNAME=admin
# 生产环境请使用 bcrypt 哈希，勿在 .env 写明文密码（与平台用户密码同一套算法）
# 在 backend 目录执行：.venv/bin/python -c "from app.auth_crypto import hash_password; print(hash_password('你的强密码'))"
# 整行建议单引号包住（哈希含 $）
# ADMIN_PASSWORD_HASH='$2b$12$......................................................'
# 仅本地/过渡可选用明文（不推荐上公网）
# ADMIN_PASSWORD=
# ADMIN_JWT_EXPIRE_HOURS=8

# 可选：用户 JWT 有效期（小时）
# JWT_EXPIRE_HOURS=72

# 可选：订阅/试用（新注册试用天数；已有用户迁移时 subscription 空档补齐天数，见代码注释）
# 新用户注册时的订阅截止：默认 0 表示不设期限；设为正整数则注册日起算试用天数
# NEW_USER_TRIAL_DAYS=0
# EXISTING_USER_SUBSCRIPTION_GRACE_DAYS=3650

# 可选：RPC 与 runner（一般保持默认即可）
# LOGIN_URL=...
# ACE_SELL_SON_URL=...
# SUBACCOUNT_URL=...
# MNEMONIC_GET01_URL=...
# RUNNER_LOOP_INTERVAL_SECONDS=10
# SUBACCOUNT_PAGE_SIZE=15
# SUBACCOUNT_MAX_PAGES=200

# 可选
# DATABASE_URL=sqlite+aiosqlite:///./data/app.db
```

生成密钥示例：

```bash
openssl rand -base64 48
```

确保 `backend/data` 目录存在且运行用户对之有读写权限（首次启动会自动创建库文件）。

### 4.3 启动命令（手动验证）

```bash
cd /opt/xiaoA/backend
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

确认本机可访问：

```bash
curl -s http://127.0.0.1:8000/api/health
# 期望: {"ok":true}
```

### 4.4 使用 systemd 守护（推荐）

创建 `/etc/systemd/system/xiaoA-backend.service`（注意用户与路径按实际修改）：

```ini
[Unit]
Description=xiaoA FastAPI backend
After=network.target

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=/opt/xiaoA/backend
Environment=PATH=/opt/xiaoA/backend/.venv/bin
ExecStart=/opt/xiaoA/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now xiaoA-backend.service
sudo systemctl status xiaoA-backend.service
```

### 4.5 售卖 runner、floor 与交易配置（运维须知）

控制台侧的「子账号卖出」链路在代码里由 runner 驱动（Login → 子账号 → Mnemonic → ACE_Sell_Son 循环）。与部署相关的要点如下。

| 主题 | 说明 |
|------|------|
| **实际请求间隔** | 用户在界面配置的 `request_interval_ms` 至少 **1000ms**；与 **floor_curr_ms** 取 **较大值** 作为本轮 ACE_Sell_Son 之间的等待（见 `app/services/runner.py`）。 |
| **floor 含义** | 动态下限间隔（毫秒），仅根据 **ACE_Sell_Son 是否返回 HTTP 429** 在滑动窗口内统计后调节；**每个用户、每个进程**各有一套状态，**重启进程后 floor 从默认下限重新学习**。上下界与步进等为代码常量（约 50～500ms 等），见 `app/services/global_floor.py`。 |
| **持久化** | 交易端账号口令、助记词等敏感字段在库中 **Fernet 加密**，密钥由 **`JWT_SECRET` 派生**（`app/trading_crypto.py`）。**更换 `JWT_SECRET` 后，旧数据将无法解密**，部署前须评估备份与迁移。 |
| **与 JWT 的关系** | 平台登录 JWT 签名也用 `JWT_SECRET`；交易字段加密与之同源，请勿随意轮换而不做数据迁移。 |
| **RPC 细节** | 各远程接口 URL、表单字段等见仓库内 `backend/api.md`。 |

环境变量里与 RPC/runner 相关的项已列在上方 4.2 可选块中（如各 RPC URL、`RUNNER_LOOP_INTERVAL_SECONDS`）。参数 `v` 由代码按官网 `base.js` 规则动态计算，不再通过环境变量配置。

### 4.6 订阅、积分与访问控制（运维须知）

| 行为 | 说明 |
|------|------|
| **谁需要有效订阅** | 依赖 `require_active_subscription` 的接口包括：`/api/config`（读写）、`/api/config/run-params`、`/api/trade/*`、`/api/subaccounts`、`/api/external/rpc-login`、`/api/run/*` 等。**`subscription_end_at` 为空视为未开通**，与已过期相同，返回 **403**，提示兑换或联系管理员。 |
| **订阅从哪来** | 新用户：`NEW_USER_TRIAL_DAYS=0`（默认）时注册后 **`subscription_end_at` 为空即未开通**，须 `POST /api/credits/redeem` 兑换后才有截止日期；若 `NEW_USER_TRIAL_DAYS` 大于 0，则从注册时刻起算试用天数。老库首次出现 `subscription_end_at` 列时，原空值会按 `EXISTING_USER_SUBSCRIPTION_GRACE_DAYS` 一次性补齐（见 `app/db.py`）。 |
| **积分** | 用户通过 `POST /api/credits/redeem` 按套餐兑换延长订阅（仅需登录，不要求当前订阅有效）。管理员可在 `/#/admin` 调整 `points_balance`（需已配置管理员账号）。 |
| **禁用账号** | `is_disabled` 为真时，任意需登录接口返回 **403**（先于订阅校验）。 |
| **Runner 恢复** | 进程启动会尝试恢复 `runner_enabled=true` 的任务；若对应用户**未开通或订阅已过期**，则**不会**自动拉起该用户的 runner（见 `app/main.py` 中 `_resume_runner_tasks`）。 |
| **日志 WebSocket** | `GET /ws/logs?token=<JWT>`：需合法用户 JWT 且**已开通且未过期**，否则连接会以 4401/4403 关闭（见 `app/main.py`）。 |

当前后端 **CORS** 为 `allow_origins=["*"]`（`app/main.py`）。若生产环境需限制浏览器来源，应改为显式域名列表并重新部署。

## 5. 前端构建

```bash
cd /opt/xiaoA/frontend
npm ci
npm run build
```

产物在 `frontend/dist/`，由 Nginx 的 `root` 指向该目录。

## 6. Nginx 配置示例

以下示例将 HTTPS 站点根目录指向前端 `dist`，并把 `/api` 与 `/ws` 转发到本机 8000 端口。**请将 `server_name` 与证书路径换成你的域名与证书位置。**

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 443 ssl http2;
    server_name your.domain.com;

    ssl_certificate     /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    root /opt/xiaoA/frontend/dist;
    index index.html;

    # 前端路由（Vue SPA）
    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }
}

server {
    listen 80;
    server_name your.domain.com;
    return 301 https://$host$request_uri;
}
```

重载 Nginx：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

浏览器访问 `https://your.domain.com`：先完成平台注册/登录（若已关闭注册，需另行在数据库中插入用户或临时开启 `REGISTRATION_OPEN` 创建首个账号后再关闭）。

## 7. 首个用户（关闭注册时）

若 `REGISTRATION_OPEN=false`，可临时改为 `true` 并重启后端，通过页面注册后再改回 `false`；或在安全环境下用脚本/SQL 插入用户（密码需为 bcrypt 哈希，维护成本高）。**创建新用户**目前仍依赖开放注册或手工入库；若已配置管理员（4.2），登录 `/#/admin` 可对**已有**用户改密、调积分、禁用或删除，便于开户后运维，但**不能**在后台直接新建账号。

## 8. 安全清单

- [ ] `JWT_SECRET` 为强随机且仅服务器可见（同时用于交易配置字段加密，轮换前须处理已入库密文）  
- [ ] 公网关闭 `REGISTRATION_OPEN`，或仅在内网开放  
- [ ] 全站 **HTTPS**（避免 JWT 与密码在明文链路传输）  
- [ ] 后端只监听 `127.0.0.1`，不直接暴露 8000 到公网  
- [ ] 定期备份 `backend/data/app.db`（含平台用户账号）  
- [ ] 防火墙仅开放 80/443  
- [ ] 若对浏览器来源有合规要求，评估将 CORS 从 `*` 改为指定域名（需改代码后发布）  

## 9. 更新发布流程（简要）

```bash
# 拉取新代码后
cd /opt/xiaoA/backend && source .venv/bin/activate && pip install -r requirements.txt
sudo systemctl restart xiaoA-backend.service

cd /opt/xiaoA/frontend && npm ci && npm run build
sudo systemctl reload nginx
```

## 10. 故障排查

| 现象 | 排查 |
|------|------|
| 502 / 无法连接 API | 后端是否运行、`curl 127.0.0.1:8000/api/health` |
| WebSocket 断连 | Nginx 是否配置 `Upgrade` / `Connection`，`location` 是否为 `/ws/` |
| 登录后立即 401 | `JWT_SECRET` 是否变更、浏览器是否混用旧 Token；清除站点本地存储后重登 |
| 403 未开通或订阅已过期 | 用户在「积分与时长」兑换，或由管理员在 `/#/admin` 增加积分后再兑换 |
| 403 账号已禁用 | 管理员在 `/#/admin` 取消禁用，或检查库中 `users.is_disabled` |
| 数据库权限错误 | `backend/data` 目录所有者与 systemd `User` 一致 |

## 11. 本地开发（对照）

开发时通常开两个进程：

```bash
# 终端 1
cd backend && source .venv/bin/activate && uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# 终端 2
cd frontend && npm run dev
```

`npm run dev` 通过 Vite 代理访问 `/api` 与 `/ws`，与生产环境 Nginx 职责类似。

---

文档版本与项目代码同步维护；若增加数据库类型、进程模型或 Docker 化，请在本文件中补充对应章节。
