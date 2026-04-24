<script setup>
import { computed, onMounted, ref, watch } from "vue";

const LS_ADMIN = "admin_access_token";

const token = ref(typeof localStorage !== "undefined" ? localStorage.getItem(LS_ADMIN) || "" : "");
const items = ref([]);
const total = ref(0);
const err = ref("");
const loading = ref(false);
const clearBusy = ref(false);
const clearMsg = ref("");

const limit = ref(50);
const offset = ref(0);
/** 按登录名精确筛选（与表格「用户」列一致，不暴露数字 id） */
const filterUsername = ref("");

function headers() {
  return {
    "Content-Type": "application/json",
    Authorization: `Bearer ${token.value}`,
  };
}

function backToAdminHome() {
  window.location.hash = "#/admin";
}

function adminLogout() {
  token.value = "";
  localStorage.removeItem(LS_ADMIN);
  items.value = [];
  total.value = 0;
}

const pageStart = computed(() => (total.value === 0 ? 0 : offset.value + 1));
const pageEnd = computed(() => Math.min(offset.value + items.value.length, total.value));
const hasPrev = computed(() => offset.value > 0);
const hasNext = computed(() => offset.value + limit.value < total.value);

async function loadLogs() {
  err.value = "";
  loading.value = true;
  token.value = localStorage.getItem(LS_ADMIN) || "";
  if (!token.value) {
    loading.value = false;
    return;
  }
  try {
    const q = new URLSearchParams();
    q.set("limit", String(limit.value));
    q.set("offset", String(offset.value));
    const uname = String(filterUsername.value || "").trim();
    if (uname) q.set("username", uname);
    const r = await fetch(`/api/admin/operation-logs?${q.toString()}`, { headers: headers() });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) {
      adminLogout();
      err.value = "登录已过期，请返回管理后台重新登录";
      return;
    }
    if (!r.ok) {
      err.value = typeof j.detail === "string" ? j.detail : "加载失败";
      items.value = [];
      total.value = 0;
      return;
    }
    items.value = Array.isArray(j.items) ? j.items : [];
    total.value = Number(j.total) || 0;
  } catch {
    err.value = "网络错误";
    items.value = [];
    total.value = 0;
  } finally {
    loading.value = false;
  }
}

function prettyParams(raw) {
  if (!raw || typeof raw !== "string") return "";
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

function displayUser(row) {
  if (row.username) return row.username;
  if (row.is_admin_action) return "（管理员）";
  return "—";
}

function formatTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleString("zh-CN", { hour12: false });
  } catch {
    return String(iso);
  }
}

async function applyFilter() {
  offset.value = 0;
  await loadLogs();
}

async function clearAllOperationLogs() {
  clearMsg.value = "";
  if (
    !window.confirm(
      "确定清空数据库中的全部用户操作日志？此操作不可恢复（与控制台「清空日志」无关）。",
    )
  ) {
    return;
  }
  clearBusy.value = true;
  try {
    const r = await fetch("/api/admin/operation-logs/clear", {
      method: "POST",
      headers: headers(),
    });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) {
      adminLogout();
      err.value = "登录已过期，请返回管理后台重新登录";
      return;
    }
    if (!r.ok) {
      clearMsg.value = typeof j.detail === "string" ? j.detail : "清空失败";
      return;
    }
    const n = Number(j.removed);
    clearMsg.value = `已清空 ${Number.isFinite(n) ? n : 0} 条操作日志`;
    offset.value = 0;
    await loadLogs();
  } catch {
    clearMsg.value = "网络错误";
  } finally {
    clearBusy.value = false;
  }
}

async function goPrev() {
  if (!hasPrev.value) return;
  offset.value = Math.max(0, offset.value - limit.value);
  await loadLogs();
}

async function goNext() {
  if (!hasNext.value) return;
  offset.value += limit.value;
  await loadLogs();
}

watch(limit, async () => {
  offset.value = 0;
  await loadLogs();
});

onMounted(() => {
  loadLogs();
});
</script>

<template>
  <div class="min-h-full bg-[#0c0a12] px-4 py-8 text-zinc-200">
    <div class="mx-auto max-w-6xl">
      <div class="mb-6 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 class="text-xl font-semibold text-rose-300/90">用户操作日志</h1>
          <p class="mt-1 text-xs text-zinc-500">记录控制台用户对 API 的调用（参数已脱敏）；管理端与出站 RPC 不写入此表。</p>
        </div>
        <div class="flex flex-wrap gap-2">
          <button
            type="button"
            class="rounded-lg border border-zinc-600 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
            @click="backToAdminHome"
          >
            返回后台首页
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

      <div v-if="!token" class="rounded-xl border border-zinc-800 bg-zinc-900/40 p-6 text-sm text-zinc-400">
        未登录管理后台，请先到
        <button type="button" class="text-rose-400 underline hover:text-rose-300" @click="backToAdminHome">后台首页</button>
        登录。
      </div>

      <template v-else>
        <div class="mb-4 flex flex-wrap items-end gap-3 rounded-xl border border-zinc-800 bg-zinc-900/40 p-4">
          <div>
            <label class="mb-1 block text-xs text-zinc-500">用户名（精确筛选，可选）</label>
            <input
              v-model="filterUsername"
              type="text"
              autocomplete="off"
              placeholder="留空表示全部用户"
              class="min-w-[10rem] rounded-lg border border-zinc-700 bg-black/50 px-3 py-1.5 text-sm outline-none ring-rose-500/40 focus:ring-2 sm:w-44"
              @keyup.enter="applyFilter"
            />
          </div>
          <div>
            <label class="mb-1 block text-xs text-zinc-500">每页条数</label>
            <select
              v-model.number="limit"
              class="rounded-lg border border-zinc-700 bg-black/50 px-2 py-1.5 text-sm text-zinc-200 outline-none ring-rose-500/40 focus:ring-2"
            >
              <option :value="25">25</option>
              <option :value="50">50</option>
              <option :value="100">100</option>
            </select>
          </div>
          <button
            type="button"
            class="rounded-lg bg-rose-800 px-4 py-1.5 text-sm text-white hover:bg-rose-700"
            :disabled="loading"
            @click="applyFilter"
          >
            查询
          </button>
          <button
            type="button"
            class="rounded-lg border border-zinc-600 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800"
            :disabled="loading"
            @click="loadLogs"
          >
            刷新
          </button>
          <button
            type="button"
            class="rounded-lg border border-amber-900/60 bg-amber-950/40 px-3 py-1.5 text-sm text-amber-200 hover:bg-amber-950/70 disabled:opacity-50"
            :disabled="loading || clearBusy"
            @click="clearAllOperationLogs"
          >
            清空全部日志
          </button>
        </div>

        <p v-if="clearMsg" class="mb-2 text-xs text-emerald-400/90">{{ clearMsg }}</p>
        <p v-if="err" class="mb-3 text-sm text-amber-400">{{ err }}</p>
        <p v-if="loading" class="mb-3 text-xs text-zinc-500">加载中…</p>

        <div class="mb-3 flex flex-wrap items-center justify-between gap-2 text-xs text-zinc-500">
          <span v-if="total > 0">第 {{ pageStart }}–{{ pageEnd }} 条，共 {{ total }} 条</span>
          <span v-else>暂无数据</span>
          <div class="flex gap-2">
            <button
              type="button"
              class="rounded border border-zinc-600 px-2 py-1 hover:bg-zinc-800 disabled:opacity-40"
              :disabled="!hasPrev || loading"
              @click="goPrev"
            >
              上一页
            </button>
            <button
              type="button"
              class="rounded border border-zinc-600 px-2 py-1 hover:bg-zinc-800 disabled:opacity-40"
              :disabled="!hasNext || loading"
              @click="goNext"
            >
              下一页
            </button>
          </div>
        </div>

        <div class="overflow-x-auto rounded-xl border border-zinc-800">
          <table class="w-full min-w-[720px] border-collapse text-left text-xs">
            <thead>
              <tr class="border-b border-zinc-800 bg-zinc-900/80 text-zinc-400">
                <th class="whitespace-nowrap px-2 py-2 font-medium">时间</th>
                <th class="min-w-[140px] px-2 py-2 font-medium">业务说明</th>
                <th class="whitespace-nowrap px-2 py-2 font-medium">用户</th>
                <th class="whitespace-nowrap px-2 py-2 font-medium">管理</th>
                <th class="whitespace-nowrap px-2 py-2 font-medium">方法</th>
                <th class="px-2 py-2 font-medium">路径</th>
                <th class="whitespace-nowrap px-2 py-2 font-medium">结果</th>
                <th class="px-2 py-2 font-medium">失败原因</th>
                <th class="min-w-[200px] px-2 py-2 font-medium">参数</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in items" :key="row.id" class="border-b border-zinc-800/80 align-top hover:bg-zinc-900/30">
                <td class="whitespace-nowrap px-2 py-2 text-zinc-300">{{ formatTime(row.created_at) }}</td>
                <td class="max-w-[200px] px-2 py-2 text-sm text-zinc-200">{{ row.business_summary || "—" }}</td>
                <td class="whitespace-nowrap px-2 py-2 text-zinc-300">{{ displayUser(row) }}</td>
                <td class="whitespace-nowrap px-2 py-2">{{ row.is_admin_action ? "是" : "否" }}</td>
                <td class="whitespace-nowrap px-2 py-2 font-mono text-cyan-600/90">{{ row.method }}</td>
                <td class="break-all px-2 py-2 font-mono text-zinc-300">{{ row.path }}</td>
                <td class="whitespace-nowrap px-2 py-2">
                  <span :class="row.success ? 'text-emerald-500' : 'text-rose-400'">{{ row.success ? "成功" : "失败" }}</span>
                </td>
                <td class="max-w-[200px] break-words px-2 py-2 text-amber-200/90">{{ row.failure_reason || "—" }}</td>
                <td class="px-2 py-2">
                  <pre
                    class="max-h-36 max-w-md overflow-auto whitespace-pre-wrap break-all rounded border border-zinc-800/80 bg-black/40 p-2 font-mono text-[10px] leading-relaxed text-zinc-400"
                    >{{ prettyParams(row.params_json) }}</pre
                  >
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </div>
  </div>
</template>
