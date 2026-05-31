<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Clock, DataAnalysis, EditPen, Setting, User, SwitchButton } from '@element-plus/icons-vue'
import { useAuthStore } from '../stores/auth'
const auth = useAuthStore(); const route = useRoute(); const router = useRouter()
const items = computed(() => [
  { path: '/workspace', label: '代码审查', icon: EditPen },
  { path: '/history', label: '历史报告', icon: Clock },
  ...(auth.isAdmin ? [{ path: '/admin', label: '后台管理', icon: DataAnalysis }] : []),
  { path: '/profile', label: '个人中心', icon: User },
])
function logout() { auth.logout(); router.push('/login') }
</script>
<template>
  <div class="app-shell">
    <aside class="sidebar glass">
      <div class="brand"><div class="brand-mark">C</div><div><strong>C-Check</strong><span>智能代码审查</span></div></div>
      <nav><router-link v-for="item in items" :key="item.path" :to="item.path" :class="{ active: route.path.startsWith(item.path) }"><el-icon><component :is="item.icon" /></el-icon>{{ item.label }}</router-link></nav>
      <div class="sidebar-bottom"><router-link to="/profile"><el-icon><Setting /></el-icon>{{ auth.user?.username }}</router-link><button @click="logout"><el-icon><SwitchButton /></el-icon>退出登录</button></div>
    </aside>
    <main class="main-area"><router-view /></main>
  </div>
</template>
