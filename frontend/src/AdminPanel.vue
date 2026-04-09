<script setup>
import { onMounted, ref } from "vue";

const LS_ADMIN = "admin_access_token";

const token = ref(typeof localStorage !== "undefined" ? localStorage.getItem(LS_ADMIN) || "" : "");
const username = ref("");
const password = ref("");
const loginErr = ref("");
const loginLoading = ref(false);

const users = ref([]);
const listErr = ref("");
const listLoading = ref(false);
const actionMsg = ref("");

const proxyEntries = ref([]);
const poolErr = ref("");
const poolLoading = ref(false);
const proxyForm = ref({ label: "", proxy_url: "" });

const pwdModal = ref({ open: false, userId: null, username: "", value: "" });
const ptsModal = ref({ open: false, userId: null, username: "", value: "" });
const bindModal = ref({ open: false, userId: null, username: "", poolEntryId: "" });
const poolEditModal = ref({ open: false, id: null, label: "", proxy_url: "" });

function headers() {
  return {
    "Content-Type": "application/json",
    Authorization: `Bearer ${token.value}`,
  };
}

async function adminLogin() {
  loginErr.value = "";
  loginLoading.value = true;
  try {
    const r = await fetch("/api/admin/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: username.value.trim(),
        password: password.value,
      }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const d = j.detail;
      loginErr.value = typeof d === "string" ? d : "登录失败";
      return;
    }
    token.value = j.access_token || "";
    localStorage.setItem(LS_ADMIN, token.value);
    await refreshAll();
  } catch {
    loginErr.value = "网络错误";
  } finally {
    loginLoading.value = false;
  }
}

function adminLogout() {
  token.value = "";
  localStorage.removeItem(LS_ADMIN);
  users.value = [];
  proxyEntries.value = [];
  actionMsg.value = "";
}

async function refreshAll() {
  await Promise.all([loadUsers(), loadProxyPool()]);
}

async function loadProxyPool() {
  poolErr.value = "";
  poolLoading.value = true;
  try {
    const r = await fetch("/api/admin/proxy-pool", { headers: headers() });
    if (r.status === 401) {
      adminLogout();
      loginErr.value = "登录已过期，请重新登录";
      return;
    }
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      poolErr.value = typeof j.detail === "string" ? j.detail : "加载代理池失败";
      return;
    }
    const j = await r.json();
    proxyEntries.value = Array.isArray(j.entries) ? j.entries : [];
  } catch {
    poolErr.value = "网络错误";
  } finally {
    poolLoading.value = false;
  }
}

async function loadUsers() {
  listErr.value = "";
  listLoading.value = true;
  actionMsg.value = "";
  try {
    const r = await fetch("/api/admin/users", { headers: headers() });
    if (r.status === 401) {
      adminLogout();
      loginErr.value = "登录已过期，请重新登录";
      return;
    }
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      listErr.value = typeof j.detail === "string" ? j.detail : "加载失败";
      return;
    }
    const j = await r.json();
    users.value = Array.isArray(j.users) ? j.users : [];
  } catch {
    listErr.value = "网络错误";
  } finally {
    listLoading.value = false;
  }
}

async function submitProxyAdd() {
  actionMsg.value = "";
  const label = proxyForm.value.label.trim();
  const proxy_url = proxyForm.value.proxy_url.trim();
  if (!proxy_url) {
    actionMsg.value = "请填写代理 URL";
    return;
  }
  const r = await fetch("/api/admin/proxy-pool", {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ label: label || "未命名", proxy_url }),
  });
  if (r.status === 401) {
    adminLogout();
    return;
  }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    actionMsg.value = typeof j.detail === "string" ? j.detail : "添加失败";
    return;
  }
  proxyForm.value = { label: "", proxy_url: "" };
  actionMsg.value = `已添加代理池条目 #${j.id ?? ""}`;
  await loadProxyPool();
}

function openPoolEdit(row) {
  poolEditModal.value = {
    open: true,
    id: row.id,
    label: row.label || "",
    proxy_url: row.proxy_url || "",
  };
}

async function submitPoolEdit() {
  actionMsg.value = "";
  const id = poolEditModal.value.id;
  const proxy_url = poolEditModal.value.proxy_url.trim();
  if (!proxy_url) {
    actionMsg.value = "代理 URL 不能为空";
    return;
  }
  const r = await fetch(`/api/admin/proxy-pool/${id}`, {
    method: "PATCH",
    headers: headers(),
    body: JSON.stringify({
      label: poolEditModal.value.label.trim(),
      proxy_url,
    }),
  });
  if (r.status === 401) {
    adminLogout();
    return;
  }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    actionMsg.value = typeof j.detail === "string" ? j.detail : "保存失败";
    return;
  }
  poolEditModal.value.open = false;
  actionMsg.value = `已更新代理池 #${id}`;
  await refreshAll();
}

async function releaseProxyEntry(row) {
  if (!row.assigned_user_id) return;
  if (!window.confirm(`释放条目 #${row.id} 与用户「${row.assigned_username || row.assigned_user_id}」的绑定？`)) return;
  actionMsg.value = "";
  const r = await fetch(`/api/admin/proxy-pool/${row.id}`, {
    method: "PATCH",
    headers: headers(),
    body: JSON.stringify({ release_assigned: true }),
  });
  if (r.status === 401) {
    adminLogout();
    return;
  }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    actionMsg.value = typeof j.detail === "string" ? j.detail : "释放失败";
    return;
  }
  actionMsg.value = `已释放 #${row.id}`;
  await refreshAll();
}

async function toggleProxyActive(row) {
  actionMsg.value = "";
  const r = await fetch(`/api/admin/proxy-pool/${row.id}`, {
    method: "PATCH",
    headers: headers(),
    body: JSON.stringify({ is_active: !row.is_active }),
  });
  if (r.status === 401) {
    adminLogout();
    return;
  }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    actionMsg.value = typeof j.detail === "string" ? j.detail : "更新失败";
    return;
  }
  await loadProxyPool();
}

function openBind(row) {
  bindModal.value = {
    open: true,
    userId: row.id,
    username: row.username,
    poolEntryId: row.proxy_entry_id != null ? String(row.proxy_entry_id) : "",
  };
}

function poolOptionsForBind() {
  const uid = bindModal.value.userId;
  return proxyEntries.value.filter(
    (p) => p.is_active && (p.assigned_user_id == null || p.assigned_user_id === uid),
  );
}

async function submitBind() {
  actionMsg.value = "";
  const uid = bindModal.value.userId;
  const raw = bindModal.value.poolEntryId;
  const pool_entry_id = raw === "" ? null : Number(raw);
  if (raw !== "" && !Number.isFinite(pool_entry_id)) {
    actionMsg.value = "请选择有效条目";
    return;
  }
  const r = await fetch(`/api/admin/users/${uid}/proxy`, {
    method: "PUT",
    headers: headers(),
    body: JSON.stringify({ pool_entry_id }),
  });
  if (r.status === 401) {
    adminLogout();
    return;
  }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    actionMsg.value = typeof j.detail === "string" ? j.detail : "绑定失败";
    return;
  }
  bindModal.value.open = false;
  actionMsg.value =
    pool_entry_id == null ? `用户 #${uid} 已解除代理绑定` : `用户 #${uid} 已绑定代理池 #${pool_entry_id}`;
  await refreshAll();
}

function formatEnd(iso) {
  if (iso == null || iso === "") return "未开通";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" });
}

async function toggleDisabled(row) {
  actionMsg.value = "";
  const r = await fetch(`/api/admin/users/${row.id}/disabled`, {
    method: "PATCH",
    headers: headers(),
    body: JSON.stringify({ disabled: !row.is_disabled }),
  });
  if (r.status === 401) {
    adminLogout();
    return;
  }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    actionMsg.value = typeof j.detail === "string" ? j.detail : "操作失败";
    return;
  }
  actionMsg.value = row.is_disabled ? `已启用 #${row.id}` : `已禁用 #${row.id}`;
  await loadUsers();
}

function openPwd(row) {
  pwdModal.value = { open: true, userId: row.id, username: row.username, value: "" };
}

function openPts(row) {
  ptsModal.value = { open: true, userId: row.id, username: row.username, value: String(row.points_balance ?? 0) };
}

async function submitPwd() {
  const v = pwdModal.value.value.trim();
  if (v.length < 6) {
    actionMsg.value = "密码至少 6 位";
    return;
  }
  const uid = pwdModal.value.userId;
  const r = await fetch(`/api/admin/users/${uid}/password`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ new_password: v }),
  });
  if (r.status === 401) {
    adminLogout();
    return;
  }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    actionMsg.value = typeof j.detail === "string" ? j.detail : "改密失败";
    return;
  }
  pwdModal.value.open = false;
  actionMsg.value = `用户 #${uid} 密码已更新`;
  await loadUsers();
}

async function submitPts() {
  const n = Number(ptsModal.value.value);
  if (!Number.isFinite(n) || n < 0) {
    actionMsg.value = "请输入非负整数";
    return;
  }
  const uid = ptsModal.value.userId;
  const r = await fetch(`/api/admin/users/${uid}/points`, {
    method: "PATCH",
    headers: headers(),
    body: JSON.stringify({ points_balance: Math.floor(n) }),
  });
  if (r.status === 401) {
    adminLogout();
    return;
  }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    actionMsg.value = typeof j.detail === "string" ? j.detail : "修改积分失败";
    return;
  }
  ptsModal.value.open = false;
  actionMsg.value = `用户 #${uid} 积分已更新`;
  await loadUsers();
}

async function deleteUser(row) {
  if (!window.confirm(`确定删除用户「${row.username}」（id=${row.id}）？不可恢复。`)) return;
  actionMsg.value = "";
  const r = await fetch(`/api/admin/users/${row.id}`, { method: "DELETE", headers: headers() });
  if (r.status === 401) {
    adminLogout();
    return;
  }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    actionMsg.value = typeof j.detail === "string" ? j.detail : "删除失败";
    return;
  }
  actionMsg.value = `已删除 #${row.id}`;
  await loadUsers();
}

function backToConsole() {
  window.location.hash = "";
}

onMounted(() => {
  if (token.value) refreshAll();
});
</script>

<template>
  <div class="min-h-full bg-[#0c0a12] px-4 py-8 text-zinc-200">
    <div class="mx-auto max-w-5xl">
      <div class="mb-6 flex flex-wrap items-center justify-between gap-3">
        <h1 class="text-xl font-semibold text-rose-300/90">后台管理</h1>
        <div class="flex flex-wrap gap-2">
          <button
            type="button"
            class="rounded-lg border border-zinc-600 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
            @click="backToConsole"
          >
            返回控制台登录
          </button>
          <button
            v-if="token"
            type="button"
            class="rounded-lg border border-zinc-600 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
            @click="adminLogout"
          >
            退出管理
          </button>
        </div>
      </div>

      <div v-if="!token" class="mx-auto max-w-md rounded-2xl border border-rose-500/25 bg-zinc-900/60 p-6">
        <p class="mb-4 text-xs text-zinc-500">使用 .env 中配置的 admin_username / admin_password 登录（与平台用户无关）。</p>
        <label class="mb-1 block text-xs text-zinc-400">管理员账号</label>
        <input
          v-model="username"
          type="text"
          autocomplete="username"
          class="mb-3 w-full rounded-lg border border-zinc-700 bg-black/50 px-3 py-2 text-sm outline-none ring-rose-500/50 focus:ring-2"
        />
        <label class="mb-1 block text-xs text-zinc-400">密码</label>
        <input
          v-model="password"
          type="password"
          autocomplete="current-password"
          class="mb-3 w-full rounded-lg border border-zinc-700 bg-black/50 px-3 py-2 text-sm outline-none ring-rose-500/50 focus:ring-2"
          @keyup.enter="adminLogin"
        />
        <p v-if="loginErr" class="mb-2 text-xs text-amber-400">{{ loginErr }}</p>
        <button
          type="button"
          :disabled="loginLoading"
          class="w-full rounded-lg bg-rose-700 py-2.5 text-sm font-medium text-white hover:bg-rose-600 disabled:opacity-50"
          @click="adminLogin"
        >
          登录
        </button>
      </div>

      <div v-else>
        <div class="mb-3 flex flex-wrap items-center gap-2">
          <button
            type="button"
            class="rounded-lg border border-zinc-600 bg-zinc-800/50 px-3 py-1.5 text-xs hover:bg-zinc-800"
            :disabled="listLoading || poolLoading"
            @click="refreshAll"
          >
            刷新列表
          </button>
          <span v-if="listLoading || poolLoading" class="text-xs text-zinc-500">加载中…</span>
        </div>
        <p v-if="listErr" class="mb-2 text-xs text-amber-400">{{ listErr }}</p>
        <p v-if="poolErr" class="mb-2 text-xs text-amber-400">{{ poolErr }}</p>
        <p v-if="actionMsg" class="mb-2 text-xs text-emerald-400/90">{{ actionMsg }}</p>

        <h2 class="mb-2 text-sm font-medium text-zinc-300">出站代理池</h2>
        <p class="mb-3 text-xs text-zinc-500">
          每条为完整 HTTP(S) 代理 URL，也支持无协议写法如 <code class="text-zinc-400">1.2.3.4:3128</code>（保存后内部按 HTTP 解析主机）。
          绑定到用户后 RPC 经此出口；表格「主机」列为解析后的 IP/域名与端口，编辑可改标签与完整 URL。
        </p>
        <div class="mb-6 rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
          <div class="mb-3 flex flex-wrap gap-2">
            <input
              v-model="proxyForm.label"
              type="text"
              placeholder="备注标签（可选）"
              class="min-w-[140px] flex-1 rounded-lg border border-zinc-700 bg-black/50 px-3 py-1.5 text-xs outline-none ring-rose-500/40 focus:ring-2 sm:max-w-xs"
            />
            <input
              v-model="proxyForm.proxy_url"
              type="text"
              placeholder="http(s)://user:pass@host:port"
              class="min-w-[200px] flex-[2] rounded-lg border border-zinc-700 bg-black/50 px-3 py-1.5 font-mono text-xs outline-none ring-rose-500/40 focus:ring-2"
            />
            <button
              type="button"
              class="rounded-lg border border-rose-700/50 bg-rose-950/40 px-3 py-1.5 text-xs text-rose-200 hover:bg-rose-950/70"
              @click="submitProxyAdd"
            >
              添加
            </button>
          </div>
          <div class="overflow-x-auto rounded-lg border border-zinc-800/80">
            <table class="w-full min-w-[640px] border-collapse text-left text-xs">
              <thead>
                <tr class="border-b border-zinc-800 text-zinc-500">
                  <th class="px-2 py-2 font-medium">ID</th>
                  <th class="px-2 py-2 font-medium">标签</th>
                  <th class="px-2 py-2 font-medium">主机</th>
                  <th class="px-2 py-2 font-medium">绑定用户</th>
                  <th class="px-2 py-2 font-medium">状态</th>
                  <th class="px-2 py-2 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="p in proxyEntries" :key="p.id" class="border-b border-zinc-800/80 hover:bg-white/[0.02]">
                  <td class="px-2 py-2 font-mono text-zinc-400">{{ p.id }}</td>
                  <td class="px-2 py-2">{{ p.label || "—" }}</td>
                  <td class="px-2 py-2 font-mono text-zinc-400">{{ p.proxy_host_preview || "—" }}</td>
                  <td class="px-2 py-2">
                    <span v-if="p.assigned_user_id" class="text-zinc-300">
                      {{ p.assigned_username || "?" }} (#{{ p.assigned_user_id }})
                    </span>
                    <span v-else class="text-zinc-600">空闲</span>
                  </td>
                  <td class="px-2 py-2">
                    <span :class="p.is_active ? 'text-emerald-500/90' : 'text-zinc-500'">
                      {{ p.is_active ? "启用" : "停用" }}
                    </span>
                  </td>
                  <td class="px-2 py-2">
                    <div class="flex flex-wrap gap-1">
                      <button
                        type="button"
                        class="rounded border border-zinc-600 px-1.5 py-0.5 hover:bg-zinc-800"
                        @click="openPoolEdit(p)"
                      >
                        编辑
                      </button>
                      <button
                        type="button"
                        class="rounded border border-zinc-600 px-1.5 py-0.5 hover:bg-zinc-800"
                        @click="toggleProxyActive(p)"
                      >
                        {{ p.is_active ? "停用" : "启用" }}
                      </button>
                      <button
                        v-if="p.assigned_user_id"
                        type="button"
                        class="rounded border border-amber-900/50 px-1.5 py-0.5 text-amber-400/90 hover:bg-amber-950/30"
                        @click="releaseProxyEntry(p)"
                      >
                        释放绑定
                      </button>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
            <p v-if="!poolLoading && proxyEntries.length === 0" class="px-3 py-4 text-center text-zinc-500">暂无池条目</p>
          </div>
        </div>

        <h2 class="mb-2 text-sm font-medium text-zinc-300">平台用户</h2>
        <div class="overflow-x-auto rounded-xl border border-zinc-800 bg-zinc-900/40">
          <table class="w-full min-w-[880px] border-collapse text-left text-xs">
            <thead>
              <tr class="border-b border-zinc-800 text-zinc-500">
                <th class="px-3 py-2 font-medium">ID</th>
                <th class="px-3 py-2 font-medium">用户名</th>
                <th class="px-3 py-2 font-medium">状态</th>
                <th class="px-3 py-2 font-medium">积分</th>
                <th class="px-3 py-2 font-medium">订阅至</th>
                <th class="px-3 py-2 font-medium">出站代理</th>
                <th class="px-3 py-2 font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="u in users" :key="u.id" class="border-b border-zinc-800/80 hover:bg-white/[0.02]">
                <td class="px-3 py-2 font-mono text-zinc-400">{{ u.id }}</td>
                <td class="px-3 py-2">{{ u.username }}</td>
                <td class="px-3 py-2">
                  <span :class="u.is_disabled ? 'text-amber-400' : 'text-emerald-500/90'">
                    {{ u.is_disabled ? "已禁用" : "正常" }}
                  </span>
                </td>
                <td class="px-3 py-2 font-mono text-zinc-300">{{ u.points_balance }}</td>
                <td class="px-3 py-2 font-mono text-zinc-500">{{ formatEnd(u.subscription_end_at) }}</td>
                <td class="max-w-[200px] px-3 py-2 text-zinc-400">
                  <span v-if="u.proxy_entry_id" class="block truncate" :title="u.proxy_label || ''">
                    <span class="font-mono text-zinc-300">{{ u.proxy_host_preview || "—" }}</span>
                    <span v-if="u.proxy_label" class="ml-1 text-zinc-500">· {{ u.proxy_label }}</span>
                  </span>
                  <span v-else class="text-zinc-600">未绑定</span>
                </td>
                <td class="px-3 py-2">
                  <div class="flex flex-wrap gap-1">
                    <button
                      type="button"
                      class="rounded border border-cyan-900/50 px-1.5 py-0.5 text-cyan-400/90 hover:bg-cyan-950/30"
                      @click="openBind(u)"
                    >
                      代理
                    </button>
                    <button
                      type="button"
                      class="rounded border border-zinc-600 px-1.5 py-0.5 hover:bg-zinc-800"
                      @click="toggleDisabled(u)"
                    >
                      {{ u.is_disabled ? "启用" : "禁用" }}
                    </button>
                    <button type="button" class="rounded border border-zinc-600 px-1.5 py-0.5 hover:bg-zinc-800" @click="openPwd(u)">
                      改密
                    </button>
                    <button type="button" class="rounded border border-zinc-600 px-1.5 py-0.5 hover:bg-zinc-800" @click="openPts(u)">
                      积分
                    </button>
                    <button
                      type="button"
                      class="rounded border border-red-900/60 px-1.5 py-0.5 text-red-400/90 hover:bg-red-950/40"
                      @click="deleteUser(u)"
                    >
                      删除
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
          <p v-if="!listLoading && users.length === 0" class="px-3 py-6 text-center text-zinc-500">暂无用户</p>
        </div>
      </div>
    </div>

    <div
      v-if="pwdModal.open"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 px-4"
      @click.self="pwdModal.open = false"
    >
      <div class="w-full max-w-sm rounded-xl border border-zinc-700 bg-zinc-900 p-4 shadow-xl">
        <h3 class="mb-2 text-sm font-medium text-zinc-200">修改密码 · {{ pwdModal.username }} (#{{ pwdModal.userId }})</h3>
        <input
          v-model="pwdModal.value"
          type="password"
          class="mb-3 w-full rounded-lg border border-zinc-700 bg-black/50 px-3 py-2 text-sm"
          placeholder="新密码（≥6 位）"
        />
        <div class="flex justify-end gap-2">
          <button type="button" class="rounded px-3 py-1.5 text-xs text-zinc-400 hover:bg-zinc-800" @click="pwdModal.open = false">
            取消
          </button>
          <button type="button" class="rounded bg-rose-700 px-3 py-1.5 text-xs text-white hover:bg-rose-600" @click="submitPwd">
            保存
          </button>
        </div>
      </div>
    </div>

    <div
      v-if="ptsModal.open"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 px-4"
      @click.self="ptsModal.open = false"
    >
      <div class="w-full max-w-sm rounded-xl border border-zinc-700 bg-zinc-900 p-4 shadow-xl">
        <h3 class="mb-2 text-sm font-medium text-zinc-200">修改积分 · {{ ptsModal.username }} (#{{ ptsModal.userId }})</h3>
        <input
          v-model="ptsModal.value"
          type="number"
          min="0"
          class="mb-3 w-full rounded-lg border border-zinc-700 bg-black/50 px-3 py-2 text-sm font-mono"
        />
        <div class="flex justify-end gap-2">
          <button type="button" class="rounded px-3 py-1.5 text-xs text-zinc-400 hover:bg-zinc-800" @click="ptsModal.open = false">
            取消
          </button>
          <button type="button" class="rounded bg-rose-700 px-3 py-1.5 text-xs text-white hover:bg-rose-600" @click="submitPts">
            保存
          </button>
        </div>
      </div>
    </div>

    <div
      v-if="poolEditModal.open"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 px-4"
      @click.self="poolEditModal.open = false"
    >
      <div class="w-full max-w-lg rounded-xl border border-zinc-700 bg-zinc-900 p-4 shadow-xl">
        <h3 class="mb-2 text-sm font-medium text-zinc-200">编辑代理池 #{{ poolEditModal.id }}</h3>
        <label class="mb-1 block text-xs text-zinc-500">标签</label>
        <input
          v-model="poolEditModal.label"
          type="text"
          class="mb-3 w-full rounded-lg border border-zinc-700 bg-black/50 px-3 py-2 text-sm"
          placeholder="备注"
        />
        <label class="mb-1 block text-xs text-zinc-500">代理 URL（含协议、账号密码与端口）</label>
        <input
          v-model="poolEditModal.proxy_url"
          type="text"
          class="mb-3 w-full rounded-lg border border-zinc-700 bg-black/50 px-3 py-2 font-mono text-xs"
          placeholder="http(s)://user:pass@host:port"
        />
        <div class="flex justify-end gap-2">
          <button type="button" class="rounded px-3 py-1.5 text-xs text-zinc-400 hover:bg-zinc-800" @click="poolEditModal.open = false">
            取消
          </button>
          <button type="button" class="rounded bg-rose-700 px-3 py-1.5 text-xs text-white hover:bg-rose-600" @click="submitPoolEdit">
            保存
          </button>
        </div>
      </div>
    </div>

    <div
      v-if="bindModal.open"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 px-4"
      @click.self="bindModal.open = false"
    >
      <div class="w-full max-w-md rounded-xl border border-zinc-700 bg-zinc-900 p-4 shadow-xl">
        <h3 class="mb-2 text-sm font-medium text-zinc-200">绑定出站代理 · {{ bindModal.username }} (#{{ bindModal.userId }})</h3>
        <p class="mb-2 text-xs text-zinc-500">仅列出已启用且空闲、或已绑定到该用户的池条目。</p>
        <select
          v-model="bindModal.poolEntryId"
          class="mb-3 w-full rounded-lg border border-zinc-700 bg-black/50 px-3 py-2 text-sm text-zinc-200"
        >
          <option value="">（不绑定 / 解除绑定）</option>
          <option v-for="opt in poolOptionsForBind()" :key="opt.id" :value="String(opt.id)">
            #{{ opt.id }} · {{ opt.proxy_host_preview || "?" }}{{ opt.label ? " · " + opt.label : "" }}
          </option>
        </select>
        <div class="flex justify-end gap-2">
          <button type="button" class="rounded px-3 py-1.5 text-xs text-zinc-400 hover:bg-zinc-800" @click="bindModal.open = false">
            取消
          </button>
          <button type="button" class="rounded bg-cyan-800 px-3 py-1.5 text-xs text-white hover:bg-cyan-700" @click="submitBind">
            保存
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
