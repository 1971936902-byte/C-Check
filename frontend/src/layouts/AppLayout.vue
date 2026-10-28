<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Clock, DataAnalysis, EditPen, Guide, HomeFilled, Setting, User, SwitchButton, Monitor, ChatDotRound } from '@element-plus/icons-vue'
import { useAuthStore } from '../stores/auth'
import { terminalUrl } from '../utils/terminal-config'
const auth = useAuthStore(); const route = useRoute(); const router = useRouter()
const terminalHref = ref<string | null>(null)
const terminalLoading = ref(true)
const configAbort = new AbortController()
onMounted(async () => {
  const timer = window.setTimeout(() => configAbort.abort(), 5000)
  try {
    const response = await fetch(`${import.meta.env.BASE_URL}terminal-config.json`, {
      signal: configAbort.signal, cache: 'no-store', credentials: 'omit',
    })
    if (response.ok) terminalHref.value = terminalUrl(await response.json())
  } catch { terminalHref.value = null }
  finally { window.clearTimeout(timer); terminalLoading.value = false }
})
onUnmounted(() => configAbort.abort())
const items = computed(() => [
  { path: '/dashboard', label: '工作台概览', icon: HomeFilled },
  { path: '/workspace', label: '代码审查', icon: EditPen },
  { path: '/history', label: '历史报告', icon: Clock },
  { path: '/assistant', label: 'AI智能助手', icon: ChatDotRound },
  ...(auth.isAdmin ? [{ path: '/admin', label: '后台管理', icon: DataAnalysis }] : []),
  { path: '/profile', label: '个人中心', icon: User },
  { path: '/help', label: '使用帮助', icon: Guide },
])
function logout() { auth.logout(); router.push('/login') }
</script>
<template>
  <div class="app-shell">
    <aside class="sidebar glass">
      <div class="brand"><div class="brand-mark">C</div><div><strong>C-Check</strong><span>智能代码审查</span></div></div>
      <nav><router-link v-for="item in items" :key="item.path" :to="item.path" :aria-label="item.label" :title="item.label" :class="{ active: route.path.startsWith(item.path) }"><el-icon><component :is="item.icon" /></el-icon>{{ item.label }}</router-link>
        <template v-if="auth.isAdmin">
          <a v-if="terminalHref" :href="terminalHref" target="_blank" rel="noopener noreferrer" aria-label="远程终端（新窗口）" title="远程终端（新窗口）"><el-icon><Monitor /></el-icon>远程终端</a>
          <span v-else class="terminal-unavailable" role="link" aria-disabled="true" tabindex="0" :title="terminalLoading ? '正在加载终端配置' : '未配置远程终端'" :aria-label="terminalLoading ? '远程终端，正在加载配置' : '远程终端，未配置'"><el-icon><Monitor /></el-icon>远程终端</span>
        </template>
      </nav>
      <button class="mobile-logout" aria-label="退出登录" title="退出登录" @click="logout"><el-icon><SwitchButton /></el-icon></button>
      <div class="sidebar-bottom"><router-link to="/profile"><el-icon><Setting /></el-icon>{{ auth.user?.username }}</router-link><button @click="logout"><el-icon><SwitchButton /></el-icon>退出登录</button></div>
    </aside>
    <main class="main-area"><router-view /></main>
  </div>
</template>
<style scoped>
.terminal-unavailable { display: flex; align-items: center; gap: 10px; padding: 11px 12px; font-size: 14px; color: var(--el-text-color-placeholder); cursor: not-allowed; }
.terminal-unavailable:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: -2px; }
@media (max-width: 900px) { .terminal-unavailable { font-size: 12px; padding: 10px 8px; } }
@media (max-width: 520px) {
  .sidebar nav { overflow-x: auto; justify-content: flex-start; }
  .sidebar nav a, .sidebar nav .terminal-unavailable { flex: 0 0 44px; min-height: 44px; }
  .terminal-unavailable { font-size: 0; gap: 0; justify-content: center; min-width: 0; flex: 1; padding: 9px 4px; }
  .terminal-unavailable .el-icon { font-size: 20px; }
}
</style>
