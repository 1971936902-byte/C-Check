<script setup lang="ts">
import type { Severity } from '../types'
defineProps<{ severity: Severity }>()
const labels: Record<Severity, string> = { high: '高危', medium: '中危', low: '低危', suggestion: '建议' }
</script>
<template><span class="badge severity" :class="`severity-${severity}`">{{ labels[severity] }}</span></template>
