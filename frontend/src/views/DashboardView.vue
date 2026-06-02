<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { errorMessage, reviewApi } from '../api/client'
import StatusBadge from '../components/StatusBadge.vue'
import type { ReviewTask } from '../types'
const tasks = ref<ReviewTask[]>([]), total = ref(0), router = useRouter()
const completed = computed(() => tasks.value.filter(task => task.status === 'completed').length)
const running = computed(() => tasks.value.filter(task => task.status === 'running' || task.status === 'queued').length)
const findings = computed(() => tasks.value.reduce((sum, task) => sum + task.finding_count, 0))
const date = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false })
onMounted(async () => { try { const { data } = await reviewApi.list({ limit: 6 }); tasks.value = data.items; total.value = data.total } catch (e) { ElMessage.error(errorMessage(e)) } })
</script>
<template><section><header class="page-header"><div><h1>工作台概览</h1><p>快速了解近期审查情况，并从常用入口开始工作。</p></div><el-button type="primary" @click="router.push('/workspace')">新建代码审查</el-button></header>
<div class="overview-metrics"><article class="panel glass"><span>审查记录</span><b>{{ total }}</b><small>当前可查看的历史任务</small></article><article class="panel glass"><span>已完成</span><b>{{ completed }}</b><small>近期完成的审查任务</small></article><article class="panel glass"><span>处理中</span><b>{{ running }}</b><small>排队中与审查中的任务</small></article><article class="panel glass"><span>发现问题</span><b>{{ findings }}</b><small>近期任务累计问题数</small></article></div>
<article class="panel glass overview-list"><div class="section-heading"><div><h2>最近审查</h2><p>查看最新提交的代码审查任务。</p></div><el-button link type="primary" @click="router.push('/history')">查看全部</el-button></div><el-table :data="tasks"><el-table-column prop="display_name" label="任务名称" min-width="200" /><el-table-column label="状态" width="110"><template #default="{ row }"><StatusBadge :status="row.status" /></template></el-table-column><el-table-column prop="finding_count" label="问题" width="80" /><el-table-column label="测试时间" width="180"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column></el-table></article></section></template>
