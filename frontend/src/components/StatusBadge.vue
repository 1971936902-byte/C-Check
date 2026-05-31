<script setup lang="ts">
import type { TaskStatus } from '../types'
defineProps<{ status: TaskStatus }>()
const labels: Record<TaskStatus, string> = { queued: '排队中', running: '审查中', completed: '已完成', failed: '失败' }
</script>
<template><span class="badge" :class="`status-${status}`"><i />{{ labels[status] }}</span></template>
