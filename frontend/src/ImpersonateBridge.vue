<script setup>
import { onMounted } from "vue";

const LS_TOKEN = "access_token";

onMounted(() => {
  const raw = window.location.hash || "";
  const m = /^#\/impersonate\/([A-Za-z0-9_-]+)$/.exec(raw);
  if (!m || !m[1]) {
    window.location.hash = "#/";
    return;
  }
  const key = m[1];
  const tok = localStorage.getItem(key);
  if (tok) {
    localStorage.setItem(LS_TOKEN, tok);
    localStorage.removeItem(key);
  }
  window.location.hash = "#/";
  window.location.reload();
});
</script>

<template>
  <div class="flex min-h-screen items-center justify-center bg-[#0c0a12] text-sm text-zinc-400">正在进入用户控制台…</div>
</template>
