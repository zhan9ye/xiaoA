<script setup>
import { computed, onMounted, ref } from "vue";

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

const createUserForm = ref({ username: "", password: "" });
const createUserBusy = ref(false);

const pwdModal = ref({ open: false, userId: null, username: "", value: "" });
const ptsModal = ref({ open: false, userId: null, username: "", value: "" });
const bindModal = ref({ open: false, userId: null, username: "", poolEntryId: "" });
const poolEditModal = ref({ open: false, id: null, label: "", proxy_url: "" });

/** 阿里云 ECS 管理端测试（按启动模板创建 / 释放） */
const ecsTestAmount = ref(1);
const ecsTestBusy = ref(false);
const ecsDeleteId = ref("");
const ecsDeleteBusy = ref(false);
const ecsLastIds = ref([]);

/** ECS 实例列表（与代理池关联） */
const ecsList = ref([]);
const ecsListErr = ref("");
const ecsListLoading = ref(false);
const ecsPage = ref(1);
const ecsPageSize = ref(20);
const ecsTotal = ref(0);
const ecsAddPoolBusyId = ref("");
const ecsLockBusyId = ref("");
const ecsReleaseBusyId = ref("");
const poolDeleteBusyId = ref(null);

/** 当前列表里是否显示为锁定（仅当前页；释放时后端仍会强校验） */
const ecsDeleteLocked = computed(() => {
  const id = ecsDeleteId.value.trim();
  if (!id) return false;
  const hit = ecsList.value.find((e) => e.instance_id === id);
  return Boolean(hit && hit.locked);
});

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
  await Promise.all([loadUsers(), loadProxyPool(), loadEcsList()]);
}

async function loadEcsList() {
  ecsListErr.value = "";
  ecsListLoading.value = true;
  try {
    const r = await fetch(
      `/api/admin/aliyun-ecs/instances?page=${ecsPage.value}&page_size=${ecsPageSize.value}`,
      { headers: headers() },
    );
    if (r.status === 401) {
      adminLogout();
      loginErr.value = "登录已过期，请重新登录";
      return;
    }
    if (r.status === 503) {
      ecsList.value = [];
      ecsTotal.value = 0;
      const j = await r.json().catch(() => ({}));
      ecsListErr.value = typeof j.detail === "string" ? j.detail : "未配置阿里云或不可用";
      return;
    }
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      ecsListErr.value = typeof j.detail === "string" ? j.detail : "加载 ECS 列表失败";
      ecsList.value = [];
      ecsTotal.value = 0;
      return;
    }
    const j = await r.json();
    ecsList.value = Array.isArray(j.instances) ? j.instances : [];
    ecsTotal.value = Number(j.total_count) || 0;
  } catch {
    ecsListErr.value = "网络错误";
    ecsList.value = [];
    ecsTotal.value = 0;
  } finally {
    ecsListLoading.value = false;
  }
}

function ecsHasNextPage() {
  return ecsPage.value * ecsPageSize.value < ecsTotal.value;
}

async function ecsGoPrevPage() {
  if (ecsPage.value <= 1) return;
  ecsPage.value -= 1;
  await loadEcsList();
}

async function ecsGoNextPage() {
  if (!ecsHasNextPage()) return;
  ecsPage.value += 1;
  await loadEcsList();
}

function canAddPoolForEcs(row) {
  return !row.pool_entry_id && (row.public_ip || "").trim().length > 0;
}

async function releaseEcsInstance(row) {
  if (row.locked) return;
  if (
    !window.confirm(
      `确定释放并删除 ECS「${row.instance_id}」？将解绑/删除对应代理池条目并调用阿里云删除，不可恢复。`,
    )
  ) {
    return;
  }
  actionMsg.value = "";
  ecsReleaseBusyId.value = row.instance_id;
  try {
    const r = await fetch("/api/admin/aliyun-ecs/delete-instance", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ instance_id: row.instance_id }),
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) {
      adminLogout();
      return;
    }
    if (!r.ok) {
      actionMsg.value = typeof j.detail === "string" ? j.detail : "释放失败";
      return;
    }
    const rm = Array.isArray(j.removed_pool_entry_ids) ? j.removed_pool_entry_ids : [];
    const ub = Array.isArray(j.unbound_user_ids) ? j.unbound_user_ids : [];
    const parts = [`已释放 ECS：${row.instance_id}；RequestId=${j.request_id || ""}`];
    if (rm.length) parts.push(`已删代理池 #${rm.join(", #")}`);
    if (ub.length) parts.push(`已解绑用户 id：${ub.join(", ")}`);
    actionMsg.value = parts.join(" | ");
    await Promise.all([loadProxyPool(), loadEcsList()]);
  } catch {
    actionMsg.value = "网络错误";
  } finally {
    ecsReleaseBusyId.value = "";
  }
}

async function toggleEcsLock(row) {
  actionMsg.value = "";
  const next = !row.locked;
  ecsLockBusyId.value = row.instance_id;
  try {
    const r = await fetch("/api/admin/aliyun-ecs/instance-lock", {
      method: "PUT",
      headers: headers(),
      body: JSON.stringify({ instance_id: row.instance_id, locked: next }),
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) {
      adminLogout();
      return;
    }
    if (!r.ok) {
      actionMsg.value = typeof j.detail === "string" ? j.detail : "锁定操作失败";
      return;
    }
    actionMsg.value = next ? `已锁定实例 ${row.instance_id}` : `已取消锁定 ${row.instance_id}`;
    await loadEcsList();
  } catch {
    actionMsg.value = "网络错误";
  } finally {
    ecsLockBusyId.value = "";
  }
}

async function addPoolEntryForEcs(row) {
  if (!canAddPoolForEcs(row)) return;
  actionMsg.value = "";
  ecsAddPoolBusyId.value = row.instance_id;
  try {
    const r = await fetch("/api/admin/aliyun-ecs/proxy-pool-entry", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ instance_id: row.instance_id }),
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) {
      adminLogout();
      return;
    }
    if (!r.ok) {
      actionMsg.value = typeof j.detail === "string" ? j.detail : "补录失败";
      return;
    }
    actionMsg.value = `已补录代理池 #${j.pool_entry_id}（${j.proxy_url || ""}）`;
    await Promise.all([loadProxyPool(), loadEcsList()]);
  } catch {
    actionMsg.value = "网络错误";
  } finally {
    ecsAddPoolBusyId.value = "";
  }
}

async function deleteProxyPoolEntry(p) {
  if (!window.confirm(`确定删除代理池条目 #${p.id}？若已绑定用户将解绑并失效其出站会话。`)) return;
  actionMsg.value = "";
  poolDeleteBusyId.value = p.id;
  try {
    const r = await fetch(`/api/admin/proxy-pool/${p.id}`, { method: "DELETE", headers: headers() });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) {
      adminLogout();
      return;
    }
    if (!r.ok) {
      actionMsg.value = typeof j.detail === "string" ? j.detail : "删除失败";
      return;
    }
    actionMsg.value =
      j.unbound_user_id != null ? `已删除代理池 #${p.id}，已解绑用户 #${j.unbound_user_id}` : `已删除代理池 #${p.id}`;
    await Promise.all([loadProxyPool(), loadEcsList()]);
  } catch {
    actionMsg.value = "网络错误";
  } finally {
    poolDeleteBusyId.value = null;
  }
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

async function submitCreateUser() {
  actionMsg.value = "";
  const u = createUserForm.value.username.trim();
  const p = createUserForm.value.password;
  if (u.length < 2) {
    actionMsg.value = "用户名至少 2 个字符";
    return;
  }
  if (p.length < 6) {
    actionMsg.value = "密码至少 6 位";
    return;
  }
  createUserBusy.value = true;
  try {
    const r = await fetch("/api/admin/users", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ username: u, password: p }),
    });
    if (r.status === 401) {
      adminLogout();
      loginErr.value = "登录已过期，请重新登录";
      return;
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const d = j.detail;
      if (typeof d === "string") actionMsg.value = d;
      else if (Array.isArray(d))
        actionMsg.value = d.map((x) => x.msg || String(x)).join("；");
      else actionMsg.value = "创建失败";
      return;
    }
    createUserForm.value = { username: "", password: "" };
    await loadUsers();
    actionMsg.value = `已创建用户 #${j.id}「${j.username || u}」`;
  } catch {
    actionMsg.value = "网络错误";
  } finally {
    createUserBusy.value = false;
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

async function adminEcsRunTest() {
  actionMsg.value = "";
  const n = Math.max(1, Math.min(10, Number(ecsTestAmount.value) || 1));
  ecsTestBusy.value = true;
  try {
    const r = await fetch("/api/admin/aliyun-ecs/run-instances", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ amount: n }),
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) {
      adminLogout();
      return;
    }
    if (!r.ok) {
      actionMsg.value = typeof j.detail === "string" ? j.detail : "创建失败";
      return;
    }
    ecsLastIds.value = Array.isArray(j.instance_ids) ? j.instance_ids : [];
    const added = Array.isArray(j.pool_entries_added) ? j.pool_entries_added : [];
    const skipIp = Array.isArray(j.pool_skipped_no_public_ip) ? j.pool_skipped_no_public_ip : [];
    const skipDup = Array.isArray(j.pool_skipped_duplicate_url) ? j.pool_skipped_duplicate_url : [];
    const parts = [
      `ECS 创建成功：${ecsLastIds.value.join(", ") || "(无返回 ID)"}；RequestId=${j.request_id || ""}`,
    ];
    if (added.length) {
      parts.push(
        `已加入出站代理池 ${added.length} 条：` +
          added.map((x) => `#${x.pool_entry_id} ${x.label || ""} (${x.proxy_url || ""})`).join("；"),
      );
    }
    if (skipIp.length) parts.push(`未拿到公网 IP（未入库）：${skipIp.join(", ")}`);
    if (skipDup.length) parts.push(`代理 URL 已存在（跳过）：${skipDup.join(", ")}`);
    actionMsg.value = parts.join(" | ");
    await Promise.all([loadProxyPool(), loadEcsList()]);
  } catch {
    actionMsg.value = "网络错误";
  } finally {
    ecsTestBusy.value = false;
  }
}

async function adminEcsDeleteTest() {
  actionMsg.value = "";
  const iid = ecsDeleteId.value.trim();
  if (!iid) {
    actionMsg.value = "请填写实例 ID";
    return;
  }
  if (ecsDeleteLocked.value) {
    actionMsg.value = "该实例在当前列表中标记为已锁定，禁止释放。请先取消锁定。";
    return;
  }
  ecsDeleteBusy.value = true;
  try {
    const r = await fetch("/api/admin/aliyun-ecs/delete-instance", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ instance_id: iid }),
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) {
      adminLogout();
      return;
    }
    if (!r.ok) {
      actionMsg.value = typeof j.detail === "string" ? j.detail : "释放失败";
      return;
    }
    const rm = Array.isArray(j.removed_pool_entry_ids) ? j.removed_pool_entry_ids : [];
    const ub = Array.isArray(j.unbound_user_ids) ? j.unbound_user_ids : [];
    const parts = [`已提交释放 ECS：${iid}；RequestId=${j.request_id || ""}`];
    if (rm.length) parts.push(`已删代理池条目 #${rm.join(", #")}`);
    if (ub.length) parts.push(`已解绑用户 id：${ub.join(", ")}`);
    actionMsg.value = parts.join(" | ");
    ecsDeleteId.value = "";
    await Promise.all([loadProxyPool(), loadEcsList()]);
  } catch {
    actionMsg.value = "网络错误";
  } finally {
    ecsDeleteBusy.value = false;
  }
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
            :disabled="listLoading || poolLoading || ecsListLoading"
            @click="refreshAll"
          >
            刷新列表
          </button>
          <span v-if="listLoading || poolLoading || ecsListLoading" class="text-xs text-zinc-500">加载中…</span>
        </div>
        <p v-if="listErr" class="mb-2 text-xs text-amber-400">{{ listErr }}</p>
        <p v-if="poolErr" class="mb-2 text-xs text-amber-400">{{ poolErr }}</p>
        <p v-if="ecsListErr" class="mb-2 text-xs text-amber-400">{{ ecsListErr }}</p>
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
                      <button
                        type="button"
                        class="rounded border border-rose-900/50 px-1.5 py-0.5 text-rose-400/90 hover:bg-rose-950/30 disabled:opacity-40"
                        :disabled="poolDeleteBusyId === p.id"
                        @click="deleteProxyPoolEntry(p)"
                      >
                        {{ poolDeleteBusyId === p.id ? "删除中…" : "删除" }}
                      </button>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
            <p v-if="!poolLoading && proxyEntries.length === 0" class="px-3 py-4 text-center text-zinc-500">暂无池条目</p>
          </div>
        </div>

        <h2 class="mb-2 text-sm font-medium text-zinc-300">ECS 实例列表</h2>
        <p class="mb-3 text-xs text-zinc-500">
          当前地域分页展示；若实例在出站代理池中无对应条目（按实例 ID 标签或 <code class="text-zinc-400">http://公网IP:3128</code> 匹配），且已有公网
          IP，可点击「补录代理池」。
        </p>
        <div class="mb-6 rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
          <div class="mb-2 flex flex-wrap items-center gap-2 text-xs text-zinc-500">
            <span>共 {{ ecsTotal }} 台</span>
            <span>·</span>
            <span>第 {{ ecsPage }} 页</span>
            <button
              type="button"
              class="rounded border border-zinc-600 px-2 py-0.5 hover:bg-zinc-800 disabled:opacity-40"
              :disabled="ecsPage <= 1 || ecsListLoading"
              @click="ecsGoPrevPage"
            >
              上一页
            </button>
            <button
              type="button"
              class="rounded border border-zinc-600 px-2 py-0.5 hover:bg-zinc-800 disabled:opacity-40"
              :disabled="!ecsHasNextPage() || ecsListLoading"
              @click="ecsGoNextPage"
            >
              下一页
            </button>
            <button
              type="button"
              class="rounded border border-zinc-600 px-2 py-0.5 hover:bg-zinc-800"
              :disabled="ecsListLoading"
              @click="loadEcsList"
            >
              刷新 ECS
            </button>
          </div>
          <div class="overflow-x-auto rounded-lg border border-zinc-800/80">
            <table class="w-full min-w-[720px] border-collapse text-left text-sm">
              <thead>
                <tr class="border-b border-zinc-800 text-xs text-zinc-500">
                  <th class="w-14 px-2 py-3 font-medium" scope="col">
                    <span class="sr-only">锁定</span>
                  </th>
                  <th class="px-3 py-3 font-medium">实例 ID</th>
                  <th class="px-3 py-3 font-medium">状态</th>
                  <th class="px-3 py-3 font-medium">可用区</th>
                  <th class="px-3 py-3 font-medium">公网 IP</th>
                  <th class="px-3 py-3 font-medium">代理池</th>
                  <th class="min-w-[17rem] px-3 py-3 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="e in ecsList" :key="e.instance_id" class="border-b border-zinc-800/80 hover:bg-white/[0.02]">
                  <td class="px-2 py-3 align-middle text-center">
                    <button
                      type="button"
                      class="inline-flex h-10 w-10 items-center justify-center rounded-lg border transition-colors disabled:opacity-40"
                      :class="
                        e.locked
                          ? 'border-amber-700/60 bg-amber-950/40 text-amber-300 hover:bg-amber-950/60'
                          : 'border-zinc-600 bg-zinc-900/50 text-zinc-400 hover:border-zinc-500 hover:bg-zinc-800 hover:text-zinc-200'
                      "
                      :disabled="ecsLockBusyId === e.instance_id"
                      :title="e.locked ? '已锁定：点击取消锁定' : '未锁定：点击锁定（禁止误释放）'"
                      @click="toggleEcsLock(e)"
                    >
                      <!-- 已锁定：闭合锁 -->
                      <svg
                        v-if="e.locked"
                        class="h-5 w-5"
                        viewBox="0 0 24 24"
                        fill="currentColor"
                        aria-hidden="true"
                      >
                        <path
                          d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1 1.71 0 3.1 1.39 3.1 3.1v2z"
                        />
                      </svg>
                      <!-- 未锁定：开锁 -->
                      <svg v-else class="h-5 w-5" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                        <path
                          d="M12 17c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm6-9h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6h1.9c0-1.71 1.39-3.1 3.1-3.1S16 4.29 16 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm0 12H6V10h12v10z"
                        />
                      </svg>
                    </button>
                  </td>
                  <td class="px-3 py-3 align-middle font-mono text-xs text-zinc-300">{{ e.instance_id }}</td>
                  <td class="px-3 py-3 align-middle text-sm text-zinc-300">{{ e.status || "—" }}</td>
                  <td class="px-3 py-3 align-middle font-mono text-xs text-zinc-500">{{ e.zone_id || "—" }}</td>
                  <td class="px-3 py-3 align-middle font-mono text-xs text-zinc-400">{{ e.public_ip || "—" }}</td>
                  <td class="px-3 py-3 align-middle text-sm">
                    <span v-if="e.pool_entry_id" class="text-emerald-500/90">
                      #{{ e.pool_entry_id }}
                      <span v-if="e.pool_match" class="text-xs text-zinc-500">（{{ e.pool_match }}）</span>
                    </span>
                    <span v-else class="text-zinc-600">无</span>
                  </td>
                  <td class="px-3 py-3 align-middle">
                    <div class="flex flex-row flex-wrap items-center gap-1.5">
                      <button
                        type="button"
                        class="shrink-0 rounded-lg border border-rose-800/70 bg-rose-950/45 px-2.5 py-1.5 text-xs font-medium text-rose-100 hover:bg-rose-950/75 disabled:cursor-not-allowed disabled:opacity-40"
                        :disabled="e.locked || ecsReleaseBusyId === e.instance_id"
                        :title="e.locked ? '已锁定，不可释放' : '释放实例并清理代理池'"
                        @click="releaseEcsInstance(e)"
                      >
                        {{ ecsReleaseBusyId === e.instance_id ? "释放中…" : "释放" }}
                      </button>
                      <button
                        type="button"
                        class="shrink-0 rounded-lg border border-cyan-800/60 bg-cyan-950/40 px-2.5 py-1.5 text-xs font-medium text-cyan-100 hover:bg-cyan-950/70 disabled:cursor-not-allowed disabled:opacity-40"
                        :disabled="!canAddPoolForEcs(e) || ecsAddPoolBusyId === e.instance_id"
                        @click="addPoolEntryForEcs(e)"
                      >
                        {{
                          ecsAddPoolBusyId === e.instance_id
                            ? "提交中…"
                            : !e.public_ip
                              ? "无公网 IP"
                              : e.pool_entry_id
                                ? "已有条目"
                                : "补录代理池"
                        }}
                      </button>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
            <p v-if="!ecsListLoading && ecsList.length === 0 && !ecsListErr" class="px-3 py-4 text-center text-zinc-500">
              暂无实例或未配置阿里云
            </p>
          </div>
        </div>

        <h2 class="mb-2 text-sm font-medium text-zinc-300">阿里云 ECS（测试）</h2>
        <p class="mb-3 text-xs text-zinc-500">
          使用 <code class="text-zinc-400">.env</code> 中的
          <code class="text-zinc-400">ALIYUN_*</code> 与
          <code class="text-zinc-400">ALIYUN_ECS_LAUNCH_TEMPLATE_*</code>，按启动模板调用
          <code class="text-zinc-400">RunInstances</code> /
          <code class="text-zinc-400">DeleteInstance(force)</code>。创建会产生按量费用，请小步验证。
        </p>
        <div class="mb-6 rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
          <div class="mb-3 flex flex-wrap items-end gap-2">
            <div class="w-24">
              <label class="mb-1 block text-xs text-zinc-500">数量</label>
              <input
                v-model.number="ecsTestAmount"
                type="number"
                min="1"
                max="10"
                class="w-full rounded-lg border border-zinc-700 bg-black/50 px-2 py-1.5 font-mono text-xs outline-none ring-rose-500/40 focus:ring-2"
              />
            </div>
            <button
              type="button"
              class="rounded-lg border border-cyan-800/60 bg-cyan-950/50 px-3 py-2 text-xs text-cyan-100 hover:bg-cyan-950/80 disabled:opacity-50"
              :disabled="ecsTestBusy"
              @click="adminEcsRunTest"
            >
              {{ ecsTestBusy ? "创建中…" : "创建 ECS（测试）" }}
            </button>
          </div>
          <div class="mb-2 flex flex-wrap items-end gap-2">
            <div class="min-w-[200px] flex-1 font-mono">
              <label class="mb-1 block text-xs text-zinc-500">实例 ID</label>
              <input
                v-model="ecsDeleteId"
                type="text"
                placeholder="i-xxxxxxxxxxxxxxxxx"
                class="w-full rounded-lg border border-zinc-700 bg-black/50 px-2 py-1.5 text-xs outline-none ring-rose-500/40 focus:ring-2"
              />
              <p v-if="ecsDeleteLocked" class="mt-1 text-xs text-amber-400/90">
                当前列表中该实例为「已锁定」，不可在此释放；请先在上表取消锁定。
              </p>
            </div>
            <button
              type="button"
              class="rounded-lg border border-rose-800/60 bg-rose-950/50 px-3 py-2 text-xs text-rose-100 hover:bg-rose-950/80 disabled:opacity-50"
              :disabled="ecsDeleteBusy || ecsDeleteLocked"
              @click="adminEcsDeleteTest"
            >
              {{ ecsDeleteBusy ? "提交中…" : "释放 ECS（测试）" }}
            </button>
          </div>
          <p class="mb-2 text-xs text-zinc-500">
            主程序等非代理 ECS 请在实例列表中点击「锁定」；锁定后即使填写实例 ID 也无法通过本页释放（后端同步校验）。
          </p>
          <p v-if="ecsLastIds.length" class="text-xs text-zinc-500">
            上次创建返回：
            <span class="font-mono text-zinc-300">{{ ecsLastIds.join(", ") }}</span>
          </p>
        </div>

        <h2 class="mb-2 text-sm font-medium text-zinc-300">平台用户</h2>
        <p class="mb-3 text-xs text-zinc-500">
          创建账号不受前台「注册开关」限制；试用时长与 <code class="text-zinc-400">NEW_USER_TRIAL_DAYS</code> 一致（为 0 则订阅为未开通）。
        </p>
        <div class="mb-4 flex flex-wrap items-end gap-2 rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
          <div class="min-w-[140px] flex-1 sm:max-w-xs">
            <label class="mb-1 block text-xs text-zinc-500">新用户名</label>
            <input
              v-model="createUserForm.username"
              type="text"
              autocomplete="off"
              class="w-full rounded-lg border border-zinc-700 bg-black/50 px-3 py-1.5 text-sm outline-none ring-rose-500/40 focus:ring-2"
              placeholder="2–64 字符"
            />
          </div>
          <div class="min-w-[140px] flex-1 sm:max-w-xs">
            <label class="mb-1 block text-xs text-zinc-500">初始密码</label>
            <input
              v-model="createUserForm.password"
              type="password"
              autocomplete="new-password"
              class="w-full rounded-lg border border-zinc-700 bg-black/50 px-3 py-1.5 text-sm outline-none ring-rose-500/40 focus:ring-2"
              placeholder="≥6 位"
              @keyup.enter="submitCreateUser"
            />
          </div>
          <button
            type="button"
            class="rounded-lg border border-emerald-800/60 bg-emerald-950/50 px-4 py-2 text-sm text-emerald-200 hover:bg-emerald-950/80 disabled:cursor-not-allowed disabled:opacity-50"
            :disabled="createUserBusy"
            @click="submitCreateUser"
          >
            {{ createUserBusy ? "创建中…" : "创建账号" }}
          </button>
        </div>
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
