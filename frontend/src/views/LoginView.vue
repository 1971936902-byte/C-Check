<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Lock, User } from '@element-plus/icons-vue'
import { errorMessage } from '../api/client'
import { useAuthStore } from '../stores/auth'
const form = reactive({ username: '', password: '' }); const loading = ref(false)
const router = useRouter(); const auth = useAuthStore()
async function submit() { if (!form.username || !form.password) return ElMessage.warning('请输入账号和密码'); loading.value = true; try { await auth.login(form.username, form.password); router.push('/workspace') } catch (e) { ElMessage.error(errorMessage(e)) } finally { loading.value = false } }
</script>
<template><div class="login-page"><div class="login-card glass"><div class="login-brand"><div class="brand-mark large">C</div><h1>C-Check</h1><p>C 语言智能代码审查平台</p></div><el-form @submit.prevent="submit"><el-form-item><el-input v-model="form.username" size="large" placeholder="账号" :prefix-icon="User" /></el-form-item><el-form-item><el-input v-model="form.password" size="large" type="password" show-password placeholder="密码" :prefix-icon="Lock" @keyup.enter="submit" /></el-form-item><el-button type="primary" size="large" :loading="loading" class="full" @click="submit">登录平台</el-button></el-form><small>专业、标准化的 C 代码 AI 审计能力</small></div></div></template>
