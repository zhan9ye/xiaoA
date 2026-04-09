<script setup>
import { onMounted, onUnmounted, ref } from "vue";
import AdminPanel from "./AdminPanel.vue";
import ConsolePanel from "./ConsolePanel.vue";
import LoginScreen from "./LoginScreen.vue";

const LS_TOKEN = "access_token";

const token = ref(typeof localStorage !== "undefined" ? localStorage.getItem(LS_TOKEN) || "" : "");

function adminHash() {
  if (typeof window === "undefined") return false;
  const h = window.location.hash || "";
  return h === "#/admin" || h === "#admin";
}

const isAdminView = ref(adminHash());

function onHashChange() {
  isAdminView.value = adminHash();
}

onMounted(() => {
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
  <AdminPanel v-if="isAdminView" />
  <template v-else>
    <LoginScreen v-if="!token" @logged-in="onLoggedIn" />
    <ConsolePanel v-else :token="token" @logout="logout" />
  </template>
</template>
