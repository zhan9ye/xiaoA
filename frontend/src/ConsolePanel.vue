<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";

const props = defineProps({
  token: { type: String, required: true },
});
const emit = defineEmits(["logout"]);

const tradeUser = ref("");
const tradePassword = ref("");
const keyToken = ref("");
/** 12 段，每段最多 4 位数字；保存时拼接为逗号分隔写入 mnemonic */
const MNEMONIC_SEGMENTS = 12;
const mnemonicParts = ref(Array.from({ length: MNEMONIC_SEGMENTS }, () => ""));
const quantityStartLimit = ref(1000);
const requestIntervalMs = ref(1000);
/** 北京时间开售 HH:mm（由下拉框同步）；默认 12:00 */
const sellStartTime = ref("12:00");
/** 下拉：空字符串表示不指定（立即开售）；否则 '00'–'23' */
const sellHourSel = ref("12");
/** 下拉：'00'–'59' */
const sellMinuteSel = ref("00");

const SELL_HOUR_OPTS = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, "0"));
const SELL_MINUTE_OPTS = Array.from({ length: 60 }, (_, i) => String(i).padStart(2, "0"));

/** 上次已成功落库的开售时间（与 sellStartTime 同格式），用于「保存时间」按钮状态 */
const sellTimeCommitted = ref("");

function normalizeSellStart(s) {
  return (s || "").trim();
}

/** 当前选择与已保存值不一致 → 可点击「保存时间」 */
const sellTimeIsDirty = computed(
  () => normalizeSellStart(sellStartTime.value) !== normalizeSellStart(sellTimeCommitted.value),
);

function applySellStartFromApi(raw) {
  const t = (raw || "").trim();
  if (!t) {
    sellHourSel.value = "";
    sellMinuteSel.value = "00";
    sellStartTime.value = "";
    sellTimeCommitted.value = "";
    return;
  }
  const m = t.match(/^(\d{1,2}):(\d{2})$/);
  if (m) {
    const hh = String(Number(m[1])).padStart(2, "0");
    const mm = String(Number(m[2])).padStart(2, "0");
    sellHourSel.value = hh;
    sellMinuteSel.value = mm;
    sellStartTime.value = `${hh}:${mm}`;
  }
  sellTimeCommitted.value = normalizeSellStart(sellStartTime.value);
}

watch([sellHourSel, sellMinuteSel], () => {
  if (!sellHourSel.value) {
    sellStartTime.value = "";
    return;
  }
  const mm = sellMinuteSel.value || "00";
  sellStartTime.value = `${sellHourSel.value}:${mm}`;
});
const runPeriodStart = ref("");
const runPeriodEnd = ref("");
/** 来自 /api/run/status：全站 floor 与 SR₄₂₉ 窗口 */
const floorCurrMs = ref(50);
const sr429Window = ref(null);
const windowSamples = ref(0);
const displayName = ref("");
const platformUserId = ref(null);

const subaccounts = ref([]);
const subaccountsRefreshBusy = ref(false);
/** 子账号刷新结果提示（错误来自接口 detail，成功显示条数） */
const subaccountsRefreshMsg = ref("");

const logs = ref([]);
const logBox = ref(null);
const wsConnected = ref(false);
const running = ref(false);
const saveMsg = ref("");
const saveRunParamsMsg = ref("");
const configCollapsed = ref(false);
const runParamsCollapsed = ref(false);
/** true：仅显示符合当前运行参数的可卖子账号 */
const showOnlyEligible = ref(false);

/** GET /api/credits/overview */
const creditsOverview = ref(null);
const redeemBusy = ref(false);
const creditsMsg = ref("");
const redeemConfirmOpen = ref(false);
const redeemPendingPackage = ref(null);
const redeemPreviewLoading = ref(false);
const redeemPreviewEndIso = ref("");
const redeemPreviewErr = ref("");

/** sonId → 挂售数量字符串；无键表示挂售全部股数 */
const listingAmountsMap = ref({});

const listingEditOpen = ref(false);
const listingEditSonId = ref("");
const listingEditFullShares = ref("");
const listingEditInput = ref("");
const listingEditErr = ref("");
const listingEditBusy = ref(false);

let ws = null;

const headers = () => ({
  "Content-Type": "application/json",
  Authorization: `Bearer ${props.token}`,
});

const wsUrl = () => {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const q = `?token=${encodeURIComponent(props.token)}`;
  return `${proto}//${location.host}/ws/logs${q}`;
};

const levelClass = (level) => {
  if (level === "success") return "text-emerald-400";
  if (level === "warn") return "text-amber-400";
  if (level === "error") return "text-red-400";
  return "text-zinc-400";
};

const scrollLog = () => {
  requestAnimationFrame(() => {
    const el = logBox.value;
    if (el) el.scrollTop = el.scrollHeight;
  });
};

const connectWs = () => {
  if (ws) {
    try {
      ws.close();
    } catch (_) {}
    ws = null;
  }
  if (!props.token) return;
  ws = new WebSocket(wsUrl());
  ws.onopen = () => {
    wsConnected.value = true;
  };
  ws.onclose = () => {
    wsConnected.value = false;
  };
  ws.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      logs.value.push(data);
      if (logs.value.length > 2000) logs.value = logs.value.slice(-2000);
      scrollLog();
    } catch (_) {}
  };
};

function cellVal(v) {
  if (v == null || v === "") return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** 子账号表不展示的列（接口字段名）。 */
const SUBACCOUNT_COLUMNS_HIDDEN = new Set([
  "id",
  "Id",
  "ID",
  "IsMemberNo",
  "AvatarImage",
  "LevelNumber",
  "ListingQty",
]);

/** 表头显示名（接口字段名 → 文案）。 */
const SUBACCOUNT_COLUMN_LABELS = {
  FlowNumber: "ID",
  MemberNo: "子账户名",
  AceAmount: "股数",
  ACEAmount: "股数",
  aceAmount: "股数",
  CreateTime: "创建时间",
};

function subaccountColumnLabel(key) {
  return SUBACCOUNT_COLUMN_LABELS[key] ?? key;
}

/** 子账户名列（表头「子账户名」），用于后缀「售」标记 */
const SUBACCOUNT_MEMBER_NO_KEYS = new Set(["MemberNo", "memberNo", "MemberNO"]);

function isMemberNoColumnKey(k) {
  return SUBACCOUNT_MEMBER_NO_KEYS.has(k);
}

/** 股数字段名（单独占第 4 列，不出现在前置/其余动态列） */
const SUBACCOUNT_AMOUNT_KEYS = ["AceAmount", "ACEAmount", "aceAmount", "Ace_Count"];

function isSubaccountAmountKey(k) {
  return SUBACCOUNT_AMOUNT_KEYS.includes(k);
}

function collectVisibleSubaccountKeys(rows) {
  if (!rows.length) return [];
  const keys = new Set();
  for (const row of rows.slice(0, 80)) {
    Object.keys(row).forEach((x) => keys.add(x));
  }
  return Array.from(keys).filter((x) => !SUBACCOUNT_COLUMNS_HIDDEN.has(x));
}

/** 将 CreateTime 等值格式化为 YYYY-MM-DD（去掉时分秒）。 */
function dateOnlyFromSubaccountField(v) {
  if (v == null || v === "") return null;
  if (typeof v === "number" && Number.isFinite(v)) {
    const ms = v > 1e12 ? v : v * 1000;
    const d = new Date(ms);
    if (!Number.isNaN(d.getTime())) {
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    }
    return null;
  }
  const s = String(v).trim();
  if (!s) return null;
  const m = s.match(/^(\d{4})[/-](\d{1,2})[/-](\d{1,2})/);
  if (m) {
    return `${m[1]}-${String(Number(m[2])).padStart(2, "0")}-${String(Number(m[3])).padStart(2, "0")}`;
  }
  if (s.length >= 10 && s[4] === "-" && s[7] === "-") return s.slice(0, 10);
  return null;
}

function subaccountTableCell(row, key) {
  if (key === "CreateTime") {
    const only = dateOnlyFromSubaccountField(row[key]);
    if (only) return only;
  }
  return cellVal(row[key]);
}

function parseAceAmount(row) {
  for (const k of ["AceAmount", "ACEAmount", "aceAmount", "Ace_Count"]) {
    const v = row?.[k];
    if (v == null || v === "") continue;
    const n = Number(String(v).replaceAll(",", "").trim());
    if (Number.isFinite(n)) return n;
  }
  return null;
}

/** 与后端 resolve_son_id 一致，用于挂售数量绑定 */
function resolveSonId(row) {
  for (const k of ["SonId", "sonId", "Id", "ID", "SubAccountId", "SubId"]) {
    const v = row?.[k];
    if (v != null && String(v).trim() !== "") return String(v).trim();
  }
  return "";
}

function aceAmountStringForRow(row) {
  const n = parseAceAmount(row);
  if (n == null) return "";
  if (Number.isFinite(n) && n === Math.floor(n)) return String(Math.floor(n));
  return String(n);
}

function listingAmountDisplay(row) {
  const fromApi = row?.ListingQty;
  if (fromApi != null && String(fromApi).trim() !== "") return String(fromApi).trim();
  const sid = resolveSonId(row);
  const full = aceAmountStringForRow(row);
  if (!sid || !full) return "—";
  const o = listingAmountsMap.value[sid];
  if (o != null && String(o).trim() !== "") return String(o).trim();
  return full;
}

function openListingEdit(row) {
  listingEditErr.value = "";
  const sid = resolveSonId(row);
  const full = aceAmountStringForRow(row);
  if (!sid || !full) return;
  listingEditSonId.value = sid;
  listingEditFullShares.value = full;
  listingEditInput.value = listingAmountDisplay(row);
  listingEditOpen.value = true;
}

function closeListingEdit() {
  listingEditOpen.value = false;
}

async function submitListingEdit() {
  listingEditErr.value = "";
  const sid = listingEditSonId.value;
  const full = String(listingEditFullShares.value).replaceAll(",", "").trim();
  const raw = String(listingEditInput.value).replaceAll(",", "").trim();
  if (!raw) {
    listingEditErr.value = "请输入挂售数量，或点「恢复全部股数」";
    return;
  }
  if (!/^[0-9]+(\.[0-9]+)?$/.test(raw)) {
    listingEditErr.value = "须为数字";
    return;
  }
  const num = parseFloat(raw);
  const fullNum = parseFloat(full);
  if (!(num > 0)) {
    listingEditErr.value = "须大于 0";
    return;
  }
  if (Number.isFinite(fullNum) && num > fullNum + 1e-9) {
    listingEditErr.value = "不能大于当前股数";
    return;
  }
  const sameAsFull = Number.isFinite(fullNum) && Math.abs(num - fullNum) < 1e-9;
  const amountPayload = sameAsFull ? "" : raw;
  listingEditBusy.value = true;
  try {
    const r = await fetch("/api/config/listing-amount", {
      method: "PATCH",
      headers: headers(),
      body: JSON.stringify({ son_id: sid, amount: amountPayload }),
    });
    if (r.status === 401) {
      emit("logout");
      return;
    }
    if (!r.ok) {
      try {
        const err = await r.json();
        const d = err.detail;
        listingEditErr.value =
          typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg || String(x)).join("；") : "保存失败";
      } catch {
        listingEditErr.value = "保存失败";
      }
      return;
    }
    const j = await r.json();
    if (j.listing_amounts && typeof j.listing_amounts === "object") {
      listingAmountsMap.value = { ...j.listing_amounts };
    }
    await loadSubaccounts();
    listingEditOpen.value = false;
  } catch {
    listingEditErr.value = "网络错误";
  } finally {
    listingEditBusy.value = false;
  }
}

async function resetListingToFullShares() {
  listingEditErr.value = "";
  const sid = listingEditSonId.value;
  if (!sid) return;
  listingEditBusy.value = true;
  try {
    const r = await fetch("/api/config/listing-amount", {
      method: "PATCH",
      headers: headers(),
      body: JSON.stringify({ son_id: sid, amount: "" }),
    });
    if (r.status === 401) {
      emit("logout");
      return;
    }
    if (!r.ok) {
      try {
        const err = await r.json();
        listingEditErr.value = typeof err.detail === "string" ? err.detail : "恢复失败";
      } catch {
        listingEditErr.value = "恢复失败";
      }
      return;
    }
    const j = await r.json();
    if (j.listing_amounts && typeof j.listing_amounts === "object") {
      listingAmountsMap.value = { ...j.listing_amounts };
    }
    await loadSubaccounts();
    listingEditOpen.value = false;
  } catch {
    listingEditErr.value = "网络错误";
  } finally {
    listingEditBusy.value = false;
  }
}

function parseCreatedDay(row) {
  const keys = [
    "CreateTime",
    "CreateDate",
    "CreatedAt",
    "AddTime",
    "RegisterTime",
    "RegisterDate",
    "CreateTimeStr",
    "Time",
    "time",
  ];
  for (const k of keys) {
    const v = row?.[k];
    if (v == null || v === "") continue;
    if (typeof v === "number") {
      const d = new Date(v * 1000);
      if (!Number.isNaN(d.getTime())) {
        return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      }
      continue;
    }
    const s = String(v).trim();
    if (!s) continue;
    const m = s.match(/^(\d{4})[/-](\d{1,2})[/-](\d{1,2})/);
    if (m) {
      return `${m[1]}-${String(Number(m[2])).padStart(2, "0")}-${String(Number(m[3])).padStart(2, "0")}`;
    }
    if (s.length >= 10 && s[4] === "-" && s[7] === "-") return s.slice(0, 10);
  }
  return null;
}

function isEligibleByRunParams(row) {
  const ace = parseAceAmount(row);
  if (ace == null) return false;
  const limit = Number(quantityStartLimit.value) || 0;
  if (!(ace > limit)) return false;

  const rs = String(runPeriodStart.value || "").trim();
  const re = String(runPeriodEnd.value || "").trim();
  if (!rs && !re) return true;

  const day = parseCreatedDay(row);
  if (!day) return false;
  if (rs && day < rs) return false;
  if (re && day > re) return false;
  return true;
}

/**
 * 列顺序：# 、第2–3列 leading、第4列股数、第5列挂售数量、rest、操作。
 * leading 优先 FlowNumber → 子账户名 → 创建时间，不足则从其余非股数字段按出现顺序补齐到 2 列。
 */
const subaccountTableLayout = computed(() => {
  const keys = collectVisibleSubaccountKeys(subaccounts.value);
  const used = new Set();
  const leading = [];
  const preferred = ["FlowNumber", "MemberNo", "memberNo", "MemberNO", "CreateTime"];
  for (const k of preferred) {
    if (!keys.includes(k) || isSubaccountAmountKey(k) || used.has(k)) continue;
    leading.push(k);
    used.add(k);
    if (leading.length >= 2) break;
  }
  if (leading.length < 2) {
    for (const k of keys) {
      if (isSubaccountAmountKey(k) || used.has(k)) continue;
      leading.push(k);
      used.add(k);
      if (leading.length >= 2) break;
    }
  }
  while (leading.length < 2) leading.push(null);
  for (const k of SUBACCOUNT_AMOUNT_KEYS) {
    if (keys.includes(k)) used.add(k);
  }
  const restKeys = keys.filter((k) => !used.has(k)).slice(0, 12);
  return { leadingKeys: leading, restKeys };
});

const eligibleCount = computed(() => {
  return subaccounts.value.filter((row) => isEligibleByRunParams(row)).length;
});

const displayedSubaccounts = computed(() => {
  const list = subaccounts.value;
  const byOriginalIndex = new Map();
  for (let i = 0; i < list.length; i++) byOriginalIndex.set(list[i], i);
  if (showOnlyEligible.value) {
    return list.filter((row) => isEligibleByRunParams(row));
  }
  return [...list].sort((a, b) => {
    const ea = isEligibleByRunParams(a) ? 0 : 1;
    const eb = isEligibleByRunParams(b) ? 0 : 1;
    if (ea !== eb) return ea - eb;
    return (byOriginalIndex.get(a) ?? 0) - (byOriginalIndex.get(b) ?? 0);
  });
});

function tradeCredStorageKey() {
  if (platformUserId.value == null) return null;
  return `xiaoA_trade_terminal_${platformUserId.value}`;
}

function readTradeCredsFromStorage() {
  const k = tradeCredStorageKey();
  if (!k || typeof localStorage === "undefined") return null;
  try {
    const raw = localStorage.getItem(k);
    if (!raw) return null;
    const o = JSON.parse(raw);
    return {
      username: (o.username && String(o.username).trim()) || "",
      password: typeof o.password === "string" ? o.password : "",
    };
  } catch (_) {
    return null;
  }
}

/** 下次打开网页时自动填入：从本机读取交易用户名 + 密码（按平台账号隔离） */
function applyTradeCredsFromStorageFull() {
  const ls = readTradeCredsFromStorage();
  if (!ls) return;
  if (ls.username) tradeUser.value = ls.username;
  if (ls.password) tradePassword.value = ls.password;
}

function splitMnemonicCsv(csv) {
  const parts = String(csv || "")
    .split(",")
    .map((x) => x.trim().replace(/[^\d]/g, "").slice(0, 4));
  const out = Array.from({ length: MNEMONIC_SEGMENTS }, () => "");
  for (let i = 0; i < MNEMONIC_SEGMENTS; i++) out[i] = parts[i] || "";
  return out;
}

function joinMnemonicCsv() {
  const parts = mnemonicParts.value.map((s) =>
    String(s).trim().replace(/[^\d]/g, "").slice(0, 4)
  );
  if (parts.every((p) => !p)) return "";
  return parts.join(",");
}

function onMnemonicPartInput(index, e) {
  const t = String(e.target.value).replace(/[^\d]/g, "").slice(0, 4);
  mnemonicParts.value[index] = t;
  if (t.length === 4 && index < MNEMONIC_SEGMENTS - 1) {
    const next = document.getElementById(`mnemonic-part-${index + 1}`);
    if (next) next.focus();
  }
}

function persistTradeCredsToLocal() {
  const k = tradeCredStorageKey();
  if (!k || typeof localStorage === "undefined") return;
  try {
    localStorage.setItem(
      k,
      JSON.stringify({
        username: tradeUser.value.trim(),
        password: tradePassword.value,
      })
    );
  } catch (_) {}
}

async function loadMe() {
  try {
    const r = await fetch("/api/auth/me", { headers: headers() });
    if (r.ok) {
      const j = await r.json();
      displayName.value = j.username || "";
      if (j.id != null) platformUserId.value = j.id;
    }
  } catch (_) {}
}

function formatSubscriptionEnd(iso) {
  if (iso == null || iso === "") return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  // 固定按北京时间展示，避免用户电脑时区非 UTC+8 时把服务端 UTC 时刻误读成「同日」错误时间
  return d.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" });
}

async function loadCreditsOverview() {
  creditsMsg.value = "";
  try {
    const r = await fetch("/api/credits/overview", { headers: headers() });
    if (r.status === 401) {
      emit("logout");
      return;
    }
    if (r.ok) {
      creditsOverview.value = await r.json();
    }
  } catch (_) {}
}

async function redeemCredits(days) {
  creditsMsg.value = "";
  redeemBusy.value = true;
  try {
    const r = await fetch("/api/credits/redeem", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ days }),
    });
    if (r.status === 401) {
      emit("logout");
      return false;
    }
    if (!r.ok) {
      try {
        const err = await r.json();
        const d = err.detail;
        creditsMsg.value = typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg || String(x)).join("；") : "兑换失败";
      } catch {
        creditsMsg.value = "兑换失败";
      }
      return false;
    }
    await loadCreditsOverview();
    creditsMsg.value = "兑换成功";
    connectWs();
    await loadConfig();
    await loadSubaccounts();
    await refreshStatus();
    return true;
  } catch {
    creditsMsg.value = "网络错误";
    return false;
  } finally {
    redeemBusy.value = false;
  }
}

async function openRedeemConfirm(pkg) {
  if (redeemBusy.value || !creditsOverview.value) return;
  if (creditsOverview.value.points_balance < pkg.points_cost) return;
  redeemPendingPackage.value = pkg;
  redeemPreviewEndIso.value = "";
  redeemPreviewErr.value = "";
  redeemPreviewLoading.value = true;
  redeemConfirmOpen.value = true;
  const days = Number(pkg.days);
  try {
    const r = await fetch("/api/credits/preview-redeem", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ days }),
    });
    if (r.status === 401) {
      redeemConfirmOpen.value = false;
      redeemPendingPackage.value = null;
      emit("logout");
      return;
    }
    if (!r.ok) {
      redeemPreviewErr.value = "无法计算到期时间";
      return;
    }
    const j = await r.json();
    const raw = j.subscription_end_at;
    if (raw != null && raw !== "") {
      redeemPreviewEndIso.value = typeof raw === "string" ? raw : new Date(raw).toISOString();
    }
  } catch {
    redeemPreviewErr.value = "网络错误";
  } finally {
    redeemPreviewLoading.value = false;
  }
}

function closeRedeemConfirm() {
  if (redeemBusy.value) return;
  redeemConfirmOpen.value = false;
  redeemPendingPackage.value = null;
  redeemPreviewEndIso.value = "";
  redeemPreviewErr.value = "";
  redeemPreviewLoading.value = false;
}

async function confirmRedeem() {
  if (!redeemPendingPackage.value) return;
  const days = Number(redeemPendingPackage.value.days);
  const ok = await redeemCredits(days);
  if (ok) {
    redeemConfirmOpen.value = false;
    redeemPendingPackage.value = null;
  }
}

async function loadRunParamsFromServer() {
  try {
    const r = await fetch("/api/config/run-params", { headers: headers() });
    if (r.status === 401) {
      emit("logout");
      return;
    }
    if (!r.ok) return;
    const rp = await r.json();
    quantityStartLimit.value = rp.quantity_start_limit ?? 1000;
    requestIntervalMs.value = rp.request_interval_ms ?? 1000;
    applySellStartFromApi(rp.sell_start_time);
    runPeriodStart.value = rp.run_period_start || "";
    runPeriodEnd.value = rp.run_period_end || "";
  } catch (_) {}
}

async function loadConfig() {
  try {
    await loadRunParamsFromServer();
    const r = await fetch("/api/config", { headers: headers() });
    if (!r.ok) {
      if (r.status === 401) emit("logout");
      return;
    }
    const j = await r.json();
    if (j && j.username) {
      const serverUser = String(j.username).trim();
      tradeUser.value = serverUser;
      if (j.key_token != null) keyToken.value = j.key_token;
      mnemonicParts.value = splitMnemonicCsv(j.mnemonic || "");
      quantityStartLimit.value = j.quantity_start_limit ?? 1000;
      requestIntervalMs.value = j.request_interval_ms ?? 1000;
      applySellStartFromApi(j.sell_start_time);
      runPeriodStart.value = j.run_period_start || "";
      runPeriodEnd.value = j.run_period_end || "";
      if (j.listing_amounts && typeof j.listing_amounts === "object") {
        listingAmountsMap.value = { ...j.listing_amounts };
      } else {
        listingAmountsMap.value = {};
      }
      const ls = readTradeCredsFromStorage();
      if (ls && ls.username === serverUser && ls.password) {
        tradePassword.value = ls.password;
      } else {
        tradePassword.value = "";
      }
    } else {
      mnemonicParts.value = Array.from({ length: MNEMONIC_SEGMENTS }, () => "");
      listingAmountsMap.value = {};
      applyTradeCredsFromStorageFull();
    }
  } catch (_) {}
}

async function loadSubaccounts() {
  try {
    const r = await fetch("/api/subaccounts", {
      headers: headers(),
      cache: "no-store",
    });
    if (r.status === 401) {
      emit("logout");
      return;
    }
    if (!r.ok) return;
    const j = await r.json();
    subaccounts.value = Array.isArray(j.items) ? j.items : [];
  } catch (_) {}
}

/** 从 RPC 复用会话或登录后拉取全部子账号，更新列表。 */
async function refreshSubaccounts() {
  if (subaccountsRefreshBusy.value) return;
  subaccountsRefreshMsg.value = "";
  subaccountsRefreshBusy.value = true;
  try {
    const r = await fetch("/api/subaccounts/refresh", {
      method: "POST",
      headers: headers(),
    });
    if (r.status === 401) {
      emit("logout");
      return;
    }
    if (!r.ok) {
      try {
        const err = await r.json();
        const d = err.detail;
        if (typeof d === "string") subaccountsRefreshMsg.value = d;
        else if (Array.isArray(d))
          subaccountsRefreshMsg.value = d.map((x) => x.msg || String(x)).join("；");
        else subaccountsRefreshMsg.value = `刷新失败（HTTP ${r.status}）`;
      } catch {
        subaccountsRefreshMsg.value = `刷新失败（HTTP ${r.status}）`;
      }
      return;
    }
    const j = await r.json();
    subaccounts.value = Array.isArray(j.items) ? j.items : [];
    const n = subaccounts.value.length;
    subaccountsRefreshMsg.value = n ? `已刷新，共 ${n} 条` : "已刷新（当前 0 条）";
  } catch {
    subaccountsRefreshMsg.value = "网络错误";
  } finally {
    subaccountsRefreshBusy.value = false;
  }
}

async function saveConfig() {
  saveMsg.value = "";
  const body = {
    username: tradeUser.value.trim(),
    password: tradePassword.value,
    key_token: keyToken.value.trim(),
    mnemonic: joinMnemonicCsv(),
    quantity_start_limit: Number(quantityStartLimit.value) || 0,
    request_interval_ms: Math.max(1000, Number(requestIntervalMs.value) || 1000),
    run_period_start: runPeriodStart.value || "",
    run_period_end: runPeriodEnd.value || "",
    sell_start_time: sellStartTime.value.trim(),
  };
  try {
    const r = await fetch("/api/config", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify(body),
    });
    if (r.status === 401) {
      emit("logout");
      return;
    }
    if (!r.ok) {
      try {
        const err = await r.json();
        const d = err.detail;
        if (typeof d === "string") saveMsg.value = d;
        else if (Array.isArray(d)) saveMsg.value = d.map((x) => x.msg || String(x)).join("；");
        else saveMsg.value = "保存失败";
      } catch {
        saveMsg.value = "保存失败";
      }
      return;
    }
    const saved = await r.json();
    if (saved.username) tradeUser.value = saved.username;
    if (saved.key_token != null) keyToken.value = saved.key_token;
    if (saved.mnemonic != null) mnemonicParts.value = splitMnemonicCsv(saved.mnemonic);
    if (saved.quantity_start_limit != null) quantityStartLimit.value = saved.quantity_start_limit;
    if (saved.request_interval_ms != null) requestIntervalMs.value = saved.request_interval_ms;
    if (saved.run_period_start != null) runPeriodStart.value = saved.run_period_start || "";
    if (saved.run_period_end != null) runPeriodEnd.value = saved.run_period_end || "";
    if (saved.sell_start_time != null) {
      applySellStartFromApi(saved.sell_start_time);
    } else {
      sellTimeCommitted.value = normalizeSellStart(sellStartTime.value);
    }
    if (saved.listing_amounts && typeof saved.listing_amounts === "object") {
      listingAmountsMap.value = { ...saved.listing_amounts };
    }
    persistTradeCredsToLocal();
    saveMsg.value = "已保存";
    connectWs();
    await loadSubaccounts();
  } catch {
    saveMsg.value = "网络错误";
  }
}

async function saveRunParams() {
  saveRunParamsMsg.value = "";
  const body = {
    quantity_start_limit: Number(quantityStartLimit.value) || 0,
    request_interval_ms: Math.max(1000, Number(requestIntervalMs.value) || 1000),
    run_period_start: runPeriodStart.value || "",
    run_period_end: runPeriodEnd.value || "",
    sell_start_time: sellStartTime.value.trim(),
  };
  try {
    const r = await fetch("/api/config/run-params", {
      method: "PATCH",
      headers: headers(),
      body: JSON.stringify(body),
    });
    if (r.status === 401) {
      emit("logout");
      return;
    }
    if (!r.ok) {
      try {
        const err = await r.json();
        const d = err.detail;
        if (typeof d === "string") saveRunParamsMsg.value = d;
        else if (Array.isArray(d))
          saveRunParamsMsg.value = d.map((x) => x.msg || String(x)).join("；");
        else saveRunParamsMsg.value = "保存失败";
      } catch {
        saveRunParamsMsg.value = "保存失败";
      }
      return;
    }
    const saved = await r.json();
    if (saved.quantity_start_limit != null) quantityStartLimit.value = saved.quantity_start_limit;
    if (saved.request_interval_ms != null) requestIntervalMs.value = saved.request_interval_ms;
    if (saved.run_period_start != null) runPeriodStart.value = saved.run_period_start || "";
    if (saved.run_period_end != null) runPeriodEnd.value = saved.run_period_end || "";
    if (saved.sell_start_time != null) {
      applySellStartFromApi(saved.sell_start_time);
    } else {
      sellTimeCommitted.value = normalizeSellStart(sellStartTime.value);
    }
    saveRunParamsMsg.value = "已保存";
  } catch {
    saveRunParamsMsg.value = "网络错误";
  }
}

async function clearLogs() {
  logs.value = [];
  try {
    const r = await fetch("/api/logs/clear", {
      method: "POST",
      headers: headers(),
    });
    if (r.status === 401) emit("logout");
  } catch (_) {}
}

async function refreshStatus() {
  try {
    const r = await fetch("/api/run/status", { headers: headers() });
    if (r.status === 401) {
      emit("logout");
      return;
    }
    if (r.ok) {
      const j = await r.json();
      running.value = !!j.running;
      if (j.floor_curr_ms != null) floorCurrMs.value = Number(j.floor_curr_ms);
      sr429Window.value = j.sr429_window != null && j.sr429_window !== undefined ? Number(j.sr429_window) : null;
      windowSamples.value = j.window_samples != null ? Number(j.window_samples) : 0;
    }
  } catch (_) {}
}

async function startRun() {
  const r = await fetch("/api/run/start", { method: "POST", headers: headers() });
  if (r.status === 401) emit("logout");
  await refreshStatus();
}

async function stopRun() {
  const r = await fetch("/api/run/stop", { method: "POST", headers: headers() });
  if (r.status === 401) emit("logout");
  await refreshStatus();
}

watch(
  () => props.token,
  async () => {
    logs.value = [];
    await loadMe();
    await loadCreditsOverview();
    applyTradeCredsFromStorageFull();
    await loadConfig();
    await loadSubaccounts();
    connectWs();
    refreshStatus();
  }
);

onMounted(async () => {
  await loadMe();
  await loadCreditsOverview();
  applyTradeCredsFromStorageFull();
  await loadConfig();
  await loadSubaccounts();
  connectWs();
  refreshStatus();
  const t = setInterval(refreshStatus, 3000);
  onBeforeUnmount(() => clearInterval(t));
});

onBeforeUnmount(() => {
  if (ws) ws.close();
});
</script>

<template>
  <div class="min-h-full bg-panel p-4 md:p-6">
    <div class="mx-auto mb-4 flex max-w-[1400px] flex-wrap items-center justify-between gap-2">
      <p class="text-sm text-zinc-400">
        当前用户 <span class="text-zinc-200">{{ displayName || "…" }}</span>
      </p>
      <div class="flex flex-wrap items-center gap-2">
        <button
          type="button"
          class="rounded-lg border border-zinc-600 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
          @click="emit('logout')"
        >
          退出登录
        </button>
      </div>
    </div>
    <div class="mx-auto flex max-w-[1400px] flex-col gap-4 lg:flex-row lg:items-start">
      <aside class="w-full shrink-0 space-y-4 lg:w-[300px]">
        <section
          v-if="creditsOverview"
          class="rounded-xl border border-line bg-card p-4 shadow-lg"
          :class="creditsOverview.subscription_active ? 'border-line' : 'border-amber-800/70 bg-amber-950/20'"
        >
          <h2 class="mb-2 text-sm font-semibold text-zinc-100">积分与时长</h2>
          <p v-if="!creditsOverview.subscription_active" class="mb-2 text-xs text-amber-200/90">
            <template v-if="creditsOverview.subscription_end_at == null || creditsOverview.subscription_end_at === ''">
              尚未开通使用时长，交易与运行相关功能不可用，请兑换下方套餐或联系管理员充值积分。
            </template>
            <template v-else>
              订阅已到期，相关功能已暂停，请兑换续期或联系管理员充值积分。
            </template>
          </p>
          <div class="mb-2 space-y-1 text-xs text-zinc-400">
            <p>
              当前积分 <span class="font-mono text-amber-300/90">{{ creditsOverview.points_balance }}</span>
            </p>
            <p v-if="creditsOverview.subscription_active">
              使用至
              <span class="font-mono text-zinc-200">{{ formatSubscriptionEnd(creditsOverview.subscription_end_at) }}</span>
              <span class="text-emerald-500/90">（有效）</span>
            </p>
            <p v-else-if="creditsOverview.subscription_end_at == null || creditsOverview.subscription_end_at === ''">
              时长状态：<span class="text-amber-200/90">未开通</span>（须积分兑换）
            </p>
            <p v-else>
              已于
              <span class="font-mono text-zinc-200">{{ formatSubscriptionEnd(creditsOverview.subscription_end_at) }}</span>
              到期
            </p>
          </div>
          <div class="max-h-[200px] space-y-1 overflow-auto rounded-lg border border-line/80 bg-black/25 p-2">
            <div
              v-for="p in creditsOverview.packages"
              :key="p.days"
              class="flex items-center justify-between gap-2 rounded border border-transparent px-1 py-1 text-[11px] hover:border-zinc-700"
            >
              <span class="text-zinc-400">{{ p.days }} 天 · {{ p.points_cost }} 积分</span>
              <button
                type="button"
                class="shrink-0 rounded bg-violet-700/80 px-2 py-0.5 text-[11px] text-white hover:bg-violet-600 disabled:opacity-40"
                :disabled="redeemBusy || creditsOverview.points_balance < p.points_cost"
                @click="openRedeemConfirm(p)"
              >
                兑换
              </button>
            </div>
          </div>
          <p v-if="creditsMsg" class="mt-2 text-center text-xs text-zinc-400">{{ creditsMsg }}</p>
        </section>

        <section class="rounded-xl border border-line bg-card p-4 shadow-lg">
          <div class="mb-3 flex items-center justify-between">
            <h2 class="text-sm font-semibold text-zinc-100">交易端配置</h2>
            <button
              type="button"
              class="rounded border border-zinc-600 px-2 py-0.5 text-xs text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
              @click="configCollapsed = !configCollapsed"
            >
              {{ configCollapsed ? "展开" : "折叠" }}
            </button>
          </div>
          <div v-show="!configCollapsed">
          <label class="mb-1 block text-xs text-zinc-500">交易用户名</label>
          <input
            v-model="tradeUser"
            class="mb-3 w-full rounded-lg border border-line bg-black/40 px-3 py-2 text-sm outline-none ring-blue-500 focus:ring-2"
            autocomplete="username"
            @blur="persistTradeCredsToLocal"
          />
          <label class="mb-1 block text-xs text-zinc-500">交易密码</label>
          <input
            v-model="tradePassword"
            type="password"
            class="mb-1 w-full rounded-lg border border-line bg-black/40 px-3 py-2 text-sm outline-none ring-blue-500 focus:ring-2"
            autocomplete="current-password"
            @blur="persistTradeCredsToLocal"
          />
          <label class="mb-1 block text-xs text-zinc-500">Google 共享密钥（16位大写字母或数字）</label>
          <input
            v-model="keyToken"
            class="mb-1 w-full rounded-lg border border-line bg-black/40 px-3 py-2 font-mono text-sm uppercase outline-none ring-blue-500 focus:ring-2"
            placeholder="例如 UVT8Q7Q775XXXXXX"
            autocomplete="off"
          />
          <label class="mb-1 block text-xs text-zinc-500">助记词（12 段，每段 4 位数字）</label>
          <div class="mb-2 grid grid-cols-3 gap-2">
            <div v-for="(_p, i) in mnemonicParts" :key="i" class="flex flex-col gap-0.5">
              <span class="text-center text-[10px] text-zinc-600">{{ i + 1 }}</span>
              <input
                :id="'mnemonic-part-' + i"
                :value="mnemonicParts[i]"
                type="text"
                inputmode="numeric"
                maxlength="4"
                class="w-full rounded border border-line bg-black/40 px-1.5 py-1.5 text-center font-mono text-xs tabular-nums outline-none ring-blue-500 focus:ring-2"
                placeholder="0000"
                autocomplete="off"
                @input="onMnemonicPartInput(i, $event)"
              />
            </div>
          </div>
          <button
            type="button"
            class="w-full rounded-lg bg-blue-600 py-2.5 text-sm font-medium text-white hover:bg-blue-500 active:bg-blue-700"
            @click="saveConfig"
          >
            保存配置
          </button>
          <p v-if="saveMsg" class="mt-2 text-center text-xs text-zinc-400">{{ saveMsg }}</p>
          </div>
        </section>

        <section class="rounded-xl border border-line bg-card p-4 shadow-lg">
          <div class="mb-3 flex items-center justify-between">
            <h2 class="text-sm font-semibold text-zinc-100">运行参数</h2>
            <button
              type="button"
              class="rounded border border-zinc-600 px-2 py-0.5 text-xs text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
              @click="runParamsCollapsed = !runParamsCollapsed"
            >
              {{ runParamsCollapsed ? "展开" : "折叠" }}
            </button>
          </div>
          <div v-show="!runParamsCollapsed">
          <p class="mb-2 text-xs text-zinc-500">日期区间（子账号创建时间）</p>
          <div class="mb-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
            <div>
              <label class="mb-1 block text-xs text-zinc-500">开始日期</label>
              <input
                v-model="runPeriodStart"
                type="date"
                class="w-full rounded-lg border border-line bg-black/40 px-3 py-2 text-sm text-zinc-200 outline-none ring-blue-500 focus:ring-2"
              />
            </div>
            <div>
              <label class="mb-1 block text-xs text-zinc-500">结束日期</label>
              <input
                v-model="runPeriodEnd"
                type="date"
                class="w-full rounded-lg border border-line bg-black/40 px-3 py-2 text-sm text-zinc-200 outline-none ring-blue-500 focus:ring-2"
              />
            </div>
          </div>
          <label class="mb-1 block text-xs text-zinc-500">数量起始限额（AceAmount 须大于此值）</label>
          <input
            v-model.number="quantityStartLimit"
            type="number"
            min="0"
            class="mb-3 w-full rounded-lg border border-line bg-black/40 px-3 py-2 text-sm outline-none ring-blue-500 focus:ring-2"
          />
          <label class="mb-1 block text-xs text-zinc-500">请求间隔（毫秒，≥1000）</label>
          <input
            v-model.number="requestIntervalMs"
            type="number"
            min="1000"
            step="50"
            class="mb-3 w-full rounded-lg border border-line bg-black/40 px-3 py-2 text-sm outline-none ring-blue-500 focus:ring-2"
          />
          <button
            type="button"
            class="mb-3 w-full rounded-lg border border-emerald-700/80 bg-emerald-950/50 py-2.5 text-sm font-medium text-emerald-100 hover:bg-emerald-900/50"
            @click="saveRunParams"
          >
            保存运行参数
          </button>
          </div>
        </section>
      </aside>

      <main class="min-w-0 flex-1 space-y-4">
        <section class="rounded-xl border border-line bg-card p-4 sm:p-5">
          <div class="flex flex-col gap-4">
            <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <h2 class="text-sm font-semibold tracking-tight text-zinc-100">运行控制</h2>
              <div class="flex w-full shrink-0 gap-2 sm:w-auto">
                <button
                  type="button"
                  class="min-h-[2.5rem] flex-1 rounded-lg bg-emerald-600 px-4 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-40 sm:flex-initial"
                  :disabled="running"
                  @click="startRun"
                >
                  开始
                </button>
                <button
                  type="button"
                  class="min-h-[2.5rem] flex-1 rounded-lg bg-red-600 px-4 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-40 sm:flex-initial"
                  :disabled="!running"
                  @click="stopRun"
                >
                  停止
                </button>
              </div>
            </div>

            <div
              class="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line/70 pb-3 text-[11px] leading-relaxed text-zinc-500 sm:text-xs"
            >
              <span class="inline-flex items-center gap-1.5">
                <span class="h-1.5 w-1.5 shrink-0 rounded-full" :class="wsConnected ? 'bg-emerald-500' : 'bg-zinc-600'" />
                WebSocket {{ wsConnected ? "已连接" : "未连接" }}
              </span>
              <span class="text-zinc-600">·</span>
              <span>任务 {{ running ? "运行中" : "已停止" }}</span>
              <span class="text-zinc-600">·</span>
              <span>floor {{ floorCurrMs }}ms</span>
              <template v-if="sr429Window != null && windowSamples >= 100">
                <span class="text-zinc-600">·</span>
                <span class="font-mono text-zinc-400">
                  SR₄₂₉ {{ (sr429Window * 100).toFixed(1) }}%（{{ windowSamples }}/100）
                </span>
              </template>
              <template v-else-if="windowSamples > 0">
                <span class="text-zinc-600">·</span>
                <span>样本 {{ windowSamples }}/100</span>
              </template>
            </div>

            <div class="space-y-2">
              <label class="text-xs font-medium text-zinc-400">开售时间（北京时间）</label>
              <div class="flex flex-wrap items-center gap-2">
                <div class="flex max-w-[14rem] flex-1 flex-wrap items-center gap-2 sm:flex-initial">
                  <select
                    v-model="sellHourSel"
                    class="min-w-0 flex-1 basis-0 rounded-lg border border-line bg-black/40 px-2 py-2 font-mono text-sm text-zinc-200 outline-none ring-blue-500 focus:ring-2"
                  >
                    <option value="">不指定（立即开售）</option>
                    <option v-for="h in SELL_HOUR_OPTS" :key="h" :value="h">{{ h }} 时</option>
                  </select>
                  <span class="shrink-0 text-zinc-500">:</span>
                  <select
                    v-model="sellMinuteSel"
                    :disabled="!sellHourSel"
                    class="min-w-0 flex-1 basis-0 rounded-lg border border-line bg-black/40 px-2 py-2 font-mono text-sm text-zinc-200 outline-none ring-blue-500 focus:ring-2 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <option v-for="m in SELL_MINUTE_OPTS" :key="m" :value="m">{{ m }} 分</option>
                  </select>
                </div>
                <button
                  type="button"
                  class="min-h-[2.5rem] rounded-lg border px-4 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-50"
                  :class="
                    sellTimeIsDirty
                      ? 'border-zinc-600 bg-zinc-800/80 text-zinc-200 hover:bg-zinc-700'
                      : 'border-zinc-700/80 bg-zinc-900/60 text-zinc-500'
                  "
                  :disabled="!sellTimeIsDirty"
                  @click="saveRunParams"
                >
                  {{ sellTimeIsDirty ? "保存时间" : "已保存" }}
                </button>
              </div>
              <p v-if="saveRunParamsMsg" class="text-xs text-emerald-400/90">{{ saveRunParamsMsg }}</p>
            </div>
          </div>
        </section>

        <section class="rounded-xl border border-line bg-card shadow-inner">
          <div class="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-2">
            <h2 class="text-sm font-medium text-zinc-300">子账号列表</h2>
            <div class="flex flex-wrap items-center gap-2">
              <span class="text-xs text-zinc-500">
                可卖 <span class="text-amber-300/90">{{ eligibleCount }}</span> 个 / 共 {{ subaccounts.length }} 个
              </span>
              <button
                type="button"
                class="rounded border border-zinc-600 px-2 py-0.5 text-xs text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
                @click="showOnlyEligible = !showOnlyEligible"
              >
                {{ showOnlyEligible ? "显示全部" : "只显示可卖" }}
              </button>
              <button
                type="button"
                class="rounded border border-zinc-600 px-2 py-0.5 text-xs text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200 disabled:cursor-not-allowed disabled:opacity-40"
                :disabled="subaccountsRefreshBusy"
                @click="refreshSubaccounts"
              >
                {{ subaccountsRefreshBusy ? "加载中…" : "刷新" }}
              </button>
            </div>
          </div>
          <p
            v-if="subaccountsRefreshMsg"
            class="border-b border-line px-4 py-1.5 text-xs"
            :class="
              subaccountsRefreshMsg.startsWith('已刷新')
                ? 'text-emerald-400/90'
                : 'text-amber-400/90'
            "
          >
            {{ subaccountsRefreshMsg }}
          </p>
          <div class="max-h-[320px] overflow-auto">
            <table
              v-if="subaccounts.length && displayedSubaccounts.length"
              class="w-full min-w-[720px] border-collapse text-left text-xs"
            >
              <thead>
                <tr class="border-b border-line bg-black/30 text-zinc-400">
                  <th class="sticky left-0 z-10 bg-panel px-2 py-2 font-medium">#</th>
                  <th
                    v-for="(lk, li) in subaccountTableLayout.leadingKeys"
                    :key="'lead-' + li"
                    class="whitespace-nowrap px-2 py-2 font-medium"
                  >
                    {{ lk ? subaccountColumnLabel(lk) : "" }}
                  </th>
                  <th class="whitespace-nowrap px-2 py-2 font-medium">股数</th>
                  <th class="whitespace-nowrap px-2 py-2 font-medium">挂售数量</th>
                  <th
                    v-for="k in subaccountTableLayout.restKeys"
                    :key="k"
                    class="whitespace-nowrap px-2 py-2 font-medium"
                  >
                    {{ subaccountColumnLabel(k) }}
                  </th>
                  <th class="whitespace-nowrap px-2 py-2 text-center font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(row, i) in displayedSubaccounts"
                  :key="resolveSonId(row) || `r-${i}`"
                  class="border-b border-line/80 hover:bg-white/[0.03]"
                  :class="isEligibleByRunParams(row) ? 'text-amber-300' : 'text-zinc-300'"
                >
                  <td class="sticky left-0 z-10 bg-panel px-2 py-1.5 text-zinc-500">{{ i + 1 }}</td>
                  <td
                    v-for="(lk, li) in subaccountTableLayout.leadingKeys"
                    :key="'lead-' + li"
                    class="max-w-[220px] truncate px-2 py-1.5 font-mono"
                    :title="lk ? subaccountTableCell(row, lk) : ''"
                  >
                    <template v-if="lk">
                      <template v-if="isMemberNoColumnKey(lk)">
                        <span>{{ subaccountTableCell(row, lk) }}</span>
                        <span
                          v-if="isEligibleByRunParams(row)"
                          class="ml-0.5 inline-block shrink-0 font-medium text-emerald-500"
                          aria-label="可卖"
                        >售</span>
                      </template>
                      <template v-else>
                        {{ subaccountTableCell(row, lk) }}
                      </template>
                    </template>
                    <template v-else>—</template>
                  </td>
                  <td class="whitespace-nowrap px-2 py-1.5 font-mono" :title="aceAmountStringForRow(row) || '—'">
                    {{ aceAmountStringForRow(row) || "—" }}
                  </td>
                  <td class="whitespace-nowrap px-2 py-1.5 font-mono" :title="listingAmountDisplay(row)">
                    {{ listingAmountDisplay(row) }}
                  </td>
                  <td
                    v-for="k in subaccountTableLayout.restKeys"
                    :key="k"
                    class="max-w-[220px] truncate px-2 py-1.5 font-mono"
                    :title="subaccountTableCell(row, k)"
                  >
                    {{ subaccountTableCell(row, k) }}
                  </td>
                  <td class="px-2 py-1.5 text-center">
                    <button
                      type="button"
                      class="rounded border border-zinc-600 px-2 py-0.5 text-[11px] text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
                      :disabled="!resolveSonId(row) || !aceAmountStringForRow(row)"
                      @click="openListingEdit(row)"
                    >
                      编辑
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
            <p v-else-if="subaccounts.length && !displayedSubaccounts.length" class="p-6 text-center text-sm text-zinc-600">
              当前筛选下无可卖子账号，可点「显示全部」
            </p>
            <p v-else class="p-6 text-center text-sm text-zinc-600">暂无数据，请先保存配置并完成登录拉取</p>
          </div>
        </section>

        <section class="flex min-h-[360px] flex-col rounded-xl border border-line bg-black shadow-inner">
          <div class="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-2">
            <h2 class="text-sm font-medium text-zinc-300">运行日志</h2>
            <button
              type="button"
              class="rounded border border-zinc-600 px-2 py-1 text-xs text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
              @click="clearLogs"
            >
              清空日志
            </button>
          </div>
          <div ref="logBox" class="flex-1 overflow-y-auto p-3 font-mono text-[13px] leading-relaxed">
            <div v-for="(line, i) in logs" :key="i" class="whitespace-pre-wrap break-all">
              <span class="text-emerald-500/90">[{{ line.ts }}]</span>
              <span class="mx-1" :class="levelClass(line.level)">{{ line.message }}</span>
            </div>
            <p v-if="!logs.length" class="text-zinc-600">等待日志推送…</p>
          </div>
        </section>
      </main>
    </div>
  </div>

  <Teleport to="body">
    <div
      v-if="redeemConfirmOpen && redeemPendingPackage && creditsOverview"
      class="fixed inset-0 z-[200] flex items-center justify-center bg-black/65 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="redeem-confirm-title"
      @click.self="closeRedeemConfirm"
    >
      <div class="w-full max-w-sm rounded-xl border border-zinc-700 bg-zinc-900 p-5 shadow-2xl ring-1 ring-black/50">
        <h3 id="redeem-confirm-title" class="mb-1 text-sm font-semibold text-zinc-100">确认兑换时长</h3>
        <p class="mb-3 text-xs text-zinc-500">
          套餐：<span class="font-mono text-zinc-300">{{ redeemPendingPackage.days }}</span> 天
        </p>
        <ul class="space-y-2 text-xs text-zinc-300">
          <li>
            将扣除积分
            <span class="font-mono text-amber-300/90">{{ redeemPendingPackage.points_cost }}</span>
            <span class="text-zinc-500">（当前余额 {{ creditsOverview.points_balance }}）</span>
          </li>
          <li>
            预计订阅延长至（北京时间）
            <span v-if="redeemPreviewLoading" class="mt-0.5 block text-zinc-500">正在计算…</span>
            <span v-else-if="redeemPreviewErr" class="mt-0.5 block text-amber-400/90">{{ redeemPreviewErr }}</span>
            <span
              v-else-if="redeemPreviewEndIso"
              class="mt-0.5 block font-mono text-emerald-400/90"
            >{{ formatSubscriptionEnd(redeemPreviewEndIso) }}</span>
            <span v-else class="mt-0.5 block text-zinc-500">—</span>
          </li>
        </ul>
        <p class="mt-3 text-[11px] leading-relaxed text-zinc-600">
          到期时间由服务端按与正式兑换相同的规则计算；若计算失败仍可尝试确认兑换，以成功后的「使用至」为准。
        </p>
        <div class="mt-5 flex justify-end gap-2">
          <button
            type="button"
            class="rounded-lg border border-zinc-600 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
            :disabled="redeemBusy || redeemPreviewLoading"
            @click="closeRedeemConfirm"
          >
            取消
          </button>
          <button
            type="button"
            class="rounded-lg bg-violet-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-violet-500 disabled:opacity-40"
            :disabled="redeemBusy || redeemPreviewLoading"
            @click="confirmRedeem"
          >
            {{ redeemBusy ? "提交中…" : "确认兑换" }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>

  <Teleport to="body">
    <div
      v-if="listingEditOpen"
      class="fixed inset-0 z-[200] flex items-center justify-center bg-black/65 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="listing-edit-title"
      @click.self="closeListingEdit"
    >
      <div class="w-full max-w-sm rounded-xl border border-zinc-700 bg-zinc-900 p-5 shadow-2xl ring-1 ring-black/50">
        <h3 id="listing-edit-title" class="mb-3 text-sm font-semibold text-zinc-100">修改挂售数量</h3>
        <p class="mb-2 text-xs text-zinc-500">
          子账号 <span class="font-mono text-zinc-400">{{ listingEditSonId }}</span>
        </p>
        <p class="mb-3 text-xs text-zinc-500">
          当前股数（默认挂售全部）：
          <span class="font-mono text-zinc-300">{{ listingEditFullShares }}</span>
        </p>
        <label class="mb-1 block text-xs text-zinc-500">挂售数量</label>
        <input
          v-model="listingEditInput"
          type="text"
          inputmode="decimal"
          autocomplete="off"
          class="mb-2 w-full rounded border border-zinc-600 bg-black/40 px-2 py-1.5 font-mono text-sm text-zinc-200 outline-none focus:border-amber-500/60"
          @keyup.enter="submitListingEdit"
        />
        <p v-if="listingEditErr" class="mb-2 text-xs text-amber-400/90">{{ listingEditErr }}</p>
        <button
          type="button"
          class="mb-4 text-xs text-zinc-500 underline decoration-zinc-600 hover:text-zinc-300"
          :disabled="listingEditBusy"
          @click="resetListingToFullShares"
        >
          恢复为全部股数
        </button>
        <div class="flex justify-end gap-2">
          <button
            type="button"
            class="rounded-lg border border-zinc-600 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
            :disabled="listingEditBusy"
            @click="closeListingEdit"
          >
            取消
          </button>
          <button
            type="button"
            class="rounded-lg bg-amber-600/90 px-3 py-1.5 text-xs font-medium text-zinc-950 hover:bg-amber-500 disabled:opacity-40"
            :disabled="listingEditBusy"
            @click="submitListingEdit"
          >
            {{ listingEditBusy ? "保存中…" : "保存" }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
input[type="date"]::-webkit-calendar-picker-indicator {
  filter: invert(1);
  opacity: 1;
}
</style>
