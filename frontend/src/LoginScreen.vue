<script setup>
import { ref } from "vue";

const emit = defineEmits(["logged-in"]);

const username = ref("");
const password = ref("");
const err = ref("");
const loading = ref(false);
const isRegister = ref(false);

async function submit() {
  err.value = "";
  loading.value = true;
  try {
    const path = isRegister.value ? "/api/auth/register" : "/api/auth/token";
    const r = await fetch(path, {
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
      if (typeof d === "string") err.value = d;
      else if (Array.isArray(d)) err.value = d.map((x) => x.msg || String(x)).join("；");
      else err.value = "请求失败";
      loading.value = false;
      return;
    }
    if (isRegister.value) {
      isRegister.value = false;
      err.value = "注册成功，请登录";
      loading.value = false;
      return;
    }
    emit("logged-in", j.access_token);
  } catch {
    err.value = "网络错误";
  }
  loading.value = false;
}
</script>

<template>
  <div class="flex min-h-full items-center justify-center bg-[#0c0a12] px-4 py-12">
    <div class="w-full max-w-md">
      <div class="mb-8 text-center">
        <h1 class="text-3xl font-semibold tracking-tight text-violet-300">登录系统</h1>
        <p class="mt-2 text-sm text-zinc-500">
          {{ isRegister ? "注册新账户以使用控制台" : "请使用平台账号与密码登录" }}
        </p>
      </div>

      <div class="mb-6 rounded-xl border border-zinc-800/80 bg-zinc-900/40 px-4 py-3 text-xs leading-relaxed text-zinc-400">
        <p class="font-medium text-zinc-300">全自动操作，无需人工干预</p>
        <ul class="mt-2 list-inside list-disc space-y-1 text-zinc-500">
          <li>云端加密传输，账号隔离存储</li>
          <li>多用户独立会话与运行任务</li>
          <li>登录后可配置交易端账号与运行参数</li>
        </ul>
      </div>

      <div class="rounded-2xl border border-violet-500/20 bg-zinc-900/60 p-6 shadow-[0_0_40px_-10px_rgba(139,92,246,0.35)]">
        <label class="mb-1 block text-sm text-zinc-300">平台账号</label>
        <input
          v-model="username"
          type="text"
          autocomplete="username"
          class="mb-4 w-full rounded-lg border border-zinc-700 bg-black/50 px-3 py-2.5 text-sm text-zinc-100 outline-none ring-violet-500 focus:border-violet-500/50 focus:ring-2"
          placeholder="请输入账号"
          @keyup.enter="submit"
        />
        <label class="mb-1 block text-sm text-zinc-300">密码</label>
        <input
          v-model="password"
          type="password"
          autocomplete="current-password"
          class="mb-2 w-full rounded-lg border border-zinc-700 bg-black/50 px-3 py-2.5 text-sm text-zinc-100 outline-none ring-violet-500 focus:border-violet-500/50 focus:ring-2"
          placeholder="请输入密码"
          @keyup.enter="submit"
        />
        <p v-if="err" class="mb-3 text-xs text-amber-400/90">{{ err }}</p>
        <button
          type="button"
          :disabled="loading"
          class="w-full rounded-lg bg-gradient-to-r from-violet-600 to-fuchsia-600 py-3 text-sm font-semibold text-white shadow-lg transition hover:from-violet-500 hover:to-fuchsia-500 disabled:opacity-50"
          @click="submit"
        >
          {{ isRegister ? "注册账户" : "进入系统" }}
        </button>
        <button
          type="button"
          class="mt-3 w-full text-center text-xs text-violet-400/80 hover:text-violet-300"
          @click="isRegister = !isRegister; err = ''"
        >
          {{ isRegister ? "已有账号？去登录" : "没有账号？注册" }}
        </button>
      </div>

      <p class="mt-6 text-center text-xs text-zinc-600">
        请使用本人账号登录；交易端凭证在登录后的控制台中单独配置
      </p>
    </div>
  </div>
</template>
