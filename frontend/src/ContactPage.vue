<script setup>
import { ref } from "vue";

defineProps({
  /** 已登录控制台时从 #/contact 进入，返回按钮文案为「返回控制台」 */
  loggedIn: { type: Boolean, default: false },
});

const WECHAT_ID = "Feng199003026";
const QR_SRC = "/contact/wechat.jpg";

const imgError = ref(false);
const copied = ref(false);
const downloadErr = ref("");

function backToLogin() {
  window.location.hash = "";
}

async function copyWechatId() {
  copied.value = false;
  try {
    await navigator.clipboard.writeText(WECHAT_ID);
    copied.value = true;
    setTimeout(() => {
      copied.value = false;
    }, 2000);
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = WECHAT_ID;
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      copied.value = true;
      setTimeout(() => {
        copied.value = false;
      }, 2000);
    } catch {
      copied.value = false;
    }
  }
}

async function downloadQr() {
  downloadErr.value = "";
  try {
    const r = await fetch(QR_SRC);
    if (!r.ok) throw new Error("not found");
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "wechat-qrcode.jpg";
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  } catch {
    downloadErr.value = "下载失败，请确认已放置 wechat.jpg";
  }
}

function onImgError() {
  imgError.value = true;
}
</script>

<template>
  <div class="flex min-h-full flex-col items-center justify-center bg-[#0c0a12] px-4 py-12">
    <div class="w-full max-w-md">
      <button
        type="button"
        class="mb-6 text-sm text-violet-400/90 hover:text-violet-300"
        @click="backToLogin"
      >
        {{ loggedIn ? "← 返回控制台" : "← 返回登录" }}
      </button>

      <div class="mb-8 text-center">
        <h1 class="text-2xl font-semibold tracking-tight text-violet-300">联系我</h1>
        <p class="mt-2 text-sm text-zinc-500">添加微信领取账号</p>
      </div>

      <div class="space-y-8 rounded-2xl border border-violet-500/20 bg-zinc-900/60 p-6 shadow-[0_0_40px_-10px_rgba(139,92,246,0.35)]">
        <div>
          <h2 class="mb-3 text-sm font-medium text-zinc-300">微信二维码</h2>
          <div
            class="mx-auto flex max-w-[240px] flex-col items-center rounded-xl border border-zinc-800 bg-black/40 p-3"
          >
            <img
              v-if="!imgError"
              :src="QR_SRC"
              alt="微信二维码"
              class="h-auto w-full max-w-[216px] rounded-lg"
              @error="onImgError"
            />
            <p v-else class="py-8 text-center text-xs text-zinc-500">
              请将图片命名为 <code class="text-zinc-400">wechat.jpg</code><br />
              放入目录 <code class="text-zinc-400">frontend/public/contact/</code>
            </p>
            <button
              type="button"
              class="mt-3 w-full rounded-lg border border-violet-600/50 bg-violet-950/40 py-2 text-sm text-violet-200 hover:bg-violet-950/70"
              @click="downloadQr"
            >
              下载二维码
            </button>
            <p v-if="downloadErr" class="mt-2 text-center text-xs text-amber-400/90">{{ downloadErr }}</p>
          </div>
        </div>

        <div>
          <h2 class="mb-3 text-sm font-medium text-zinc-300">微信号</h2>
          <div class="flex flex-wrap items-center gap-2 rounded-xl border border-zinc-800 bg-black/40 px-4 py-3">
            <span class="font-mono text-lg text-zinc-100">{{ WECHAT_ID }}</span>
            <button
              type="button"
              class="ml-auto shrink-0 rounded-lg border border-emerald-800/60 bg-emerald-950/50 px-3 py-1.5 text-xs text-emerald-200 hover:bg-emerald-950/80"
              @click="copyWechatId"
            >
              {{ copied ? "已复制" : "复制" }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>