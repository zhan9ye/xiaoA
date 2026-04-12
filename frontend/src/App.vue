<script setup>
import { onMounted, onUnmounted, ref } from "vue";
import AdminPanel from "./AdminPanel.vue";
import ContactPage from "./ContactPage.vue";
import ConsolePanel from "./ConsolePanel.vue";
import LoginScreen from "./LoginScreen.vue";

const LS_TOKEN = "access_token";

const token = ref(typeof localStorage !== "undefined" ? localStorage.getItem(LS_TOKEN) || "" : "");

function adminHash() {
  if (typeof window === "undefined") return false;
  const h = window.location.hash || "";
  return h === "#/admin" || h === "#admin";
}

function contactHash() {
  if (typeof window === "undefined") return false;
  const h = (window.location.hash || "").toLowerCase();
  return h === "#/contact" || h === "#contact";
}

const isAdminView = ref(adminHash());
const isContactView = ref(contactHash());

function onHashChange() {
  isAdminView.value = adminHash();
  isContactView.value = contactHash();
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
  <template v-else-if="token">
    <ConsolePanel :token="token" @logout="logout" />
  </template>
  <template v-else>
    <ContactPage v-if="isContactView" />
    <LoginScreen v-else @logged-in="onLoggedIn" />
  </template>
</template>
