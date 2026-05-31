<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { authApi, errorMessage } from '../api/client'
import { useAuthStore } from '../stores/auth'
const form = reactive({ current: '', next: '', confirm: '' }), loading = ref(false), auth = useAuthStore(), router = useRouter()
async function submit() { if (form.next.length < 12) return ElMessage.warning('新密码至少 12 位'); if (form.next !== form.confirm) return ElMessage.warning('两次输入的新密码不一致'); loading.value = true; try { await authApi.password(form.current, form.next); auth.logout(); ElMessage.success('密码已修改，请重新登录'); router.push('/login') } catch (e) { ElMessage.error(errorMessage(e)) } finally { loading.value = false } }
</script>
<template><section><header class="page-header"><div><h1>个人中心</h1><p>查看账号信息并维护登录密码。</p></div></header><div class="profile-grid"><article class="panel glass"><h2>账号信息</h2><dl class="profile-list"><div><dt>用户名</dt><dd>{{ auth.user?.username }}</dd></div><div><dt>账号角色</dt><dd>{{ auth.user?.role === 'admin' ? '管理员' : '普通用户' }}</dd></div><div><dt>账号状态</dt><dd><el-tag type="success">正常</el-tag></dd></div></dl></article><article class="panel glass"><h2>修改密码</h2><el-form label-position="top"><el-form-item label="当前密码"><el-input v-model="form.current" type="password" show-password /></el-form-item><el-form-item label="新密码"><el-input v-model="form.next" type="password" show-password placeholder="至少 12 位" /></el-form-item><el-form-item label="确认新密码"><el-input v-model="form.confirm" type="password" show-password /></el-form-item><el-button type="primary" :loading="loading" @click="submit">更新密码</el-button></el-form></article></div></section></template>
