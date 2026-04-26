<script setup>
import { onMounted, onUnmounted, ref } from "vue";
import AdminOperationLogsPanel from "./AdminOperationLogsPanel.vue";
import AdminPanel from "./AdminPanel.vue";
import ContactPage from "./ContactPage.vue";
import ConsolePanel from "./ConsolePanel.vue";
import ImpersonateBridge from "./ImpersonateBridge.vue";
import LoginScreen from "./LoginScreen.vue";

const LS_TOKEN = "access_token";

const token = ref(typeof localStorage !== "undefined" ? localStorage.getItem(LS_TOKEN) || "" : "");

function adminHash() {
  if (typeof window === "undefined") return false;
  const h = window.location.hash || "";
  return h === "#/admin" || h === "#admin" || h.startsWith("#/admin/");
}

function adminOperationLogsHash() {
  if (typeof window === "undefined") return false;
  const h = (window.location.hash || "").toLowerCase();
  return h === "#/admin/operation-logs" || h === "#/admin/operation-logs/";
}

function contactHash() {
  if (typeof window === "undefined") return false;
  const h = (window.location.hash || "").toLowerCase();
  return h === "#/contact" || h === "#contact";
}

function impersonateBridgeHash() {
  if (typeof window === "undefined") return false;
  const h = window.location.hash || "";
  return /^#\/impersonate\/[A-Za-z0-9_-]+$/.test(h);
}

const isAdminView = ref(adminHash());
const isAdminOperationLogsView = ref(adminOperationLogsHash());
const isContactView = ref(contactHash());

const isImpersonateBridge = ref(impersonateBridgeHash());

function onHashChange() {
  isAdminView.value = adminHash();
  isAdminOperationLogsView.value = adminOperationLogsHash();
  isContactView.value = contactHash();
  isImpersonateBridge.value = impersonateBridgeHash();
}

onMounted(() => {
  isImpersonateBridge.value = impersonateBridgeHash();
  window.addEventListener("hashchange", onHashChange);
});

onUnmounted(() => {
  window.removeEventListener("hashchange", onHashChange);
});

function onLoggedIn(t) {
  token.value = t;
  localStorage.setItem(LS_TOKEN, t);
}

function logout() {
  token.value = "";
  localStorage.removeItem(LS_TOKEN);
}
</script>

<template>
  <ImpersonateBridge v-if="isImpersonateBridge" />
  <AdminOperationLogsPanel v-else-if="isAdminView && isAdminOperationLogsView" />
  <AdminPanel v-else-if="isAdminView" />
  <template v-else-if="token">
    <ContactPage v-if="isContactView" :logged-in="true" />
    <ConsolePanel v-else :token="token" @logout="logout" />
  </template>
  <template v-else>
    <ContactPage v-if="isContactView" />
    <LoginScreen v-else @logged-in="onLoggedIn" />
  </template>
</template>
