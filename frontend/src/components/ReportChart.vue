<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
const props = defineProps<{ counts: Record<string, number>; mode?: 'severity' | 'category' }>()
const el = ref<HTMLDivElement>(); let chart: echarts.ECharts | undefined
const names: Record<string, string> = { high: '高危', medium: '中危', low: '低危', suggestion: '建议', memory_safety: '内存安全', logic: '逻辑', security: '安全', concurrency: '并发', performance: '性能', style: '规范', portability: '可移植性' }
const colors = ['#ec6a72', '#f0a35e', '#e9c25d', '#62a7d4', '#7392ce', '#82b59a', '#9c86c8']
function render() {
  if (!el.value) return
  chart ||= echarts.init(el.value)
  const data = Object.entries(props.counts).map(([key, value]) => ({ name: names[key] || key, value }))
  chart.setOption({ tooltip: { trigger: 'item' }, color: colors, legend: { bottom: 0, textStyle: { color: '#607086' } }, series: [{ type: 'pie', radius: ['48%', '72%'], center: ['50%', '42%'], label: { formatter: '{b}\n{c}', color: '#4c5e73' }, data }] })
}
onMounted(() => { render(); addEventListener('resize', render) })
watch(() => props.counts, render, { deep: true })
onBeforeUnmount(() => { removeEventListener('resize', render); chart?.dispose() })
</script>
<template><div ref="el" class="report-chart" /></template>
