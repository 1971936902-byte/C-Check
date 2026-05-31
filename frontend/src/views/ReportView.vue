<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Download } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { errorMessage, reportApi } from '../api/client'
import ReportChart from '../components/ReportChart.vue'
import SeverityBadge from '../components/SeverityBadge.vue'
import type { Report } from '../types'
const route = useRoute(), report = ref<Report>(), loading = ref(true)
const severityCounts = computed<Record<string, number>>(() => report.value ? { high: report.value.high_count, medium: report.value.medium_count, low: report.value.low_count, suggestion: report.value.suggestion_count } : { high: 0, medium: 0, low: 0, suggestion: 0 })
onMounted(async () => { try { report.value = (await reportApi.get(String(route.params.id))).data } catch (e) { ElMessage.error(errorMessage(e)) } finally { loading.value = false } })
async function download(format: 'markdown' | 'pdf') { if (!report.value) return; try { const { data } = await reportApi.download(report.value.id, format); const url = URL.createObjectURL(data); const a = document.createElement('a'); a.href = url; a.download = `report-${report.value.id}.${format === 'markdown' ? 'md' : 'pdf'}`; a.click(); URL.revokeObjectURL(url) } catch (e) { ElMessage.error(errorMessage(e)) } }
</script>
<template><section v-loading="loading"><header class="page-header"><div><h1>审查报告</h1><p>结构化呈现代码风险、问题定位与修复建议。</p></div><div><el-button :icon="Download" @click="download('markdown')">下载 Markdown</el-button><el-button type="primary" :icon="Download" @click="download('pdf')">下载 PDF</el-button></div></header>
<template v-if="report"><div class="metric-row"><div class="metric-card glass score"><span>综合评分</span><strong>{{ report.score }}</strong><small>/ 100</small></div><div class="metric-card glass"><span>高危问题</span><strong>{{ report.high_count }}</strong></div><div class="metric-card glass"><span>中危问题</span><strong>{{ report.medium_count }}</strong></div><div class="metric-card glass"><span>全部发现</span><strong>{{ report.result_json.findings.length }}</strong></div></div>
<div class="report-grid"><div><article class="panel glass summary"><h2>审查概览</h2><p>{{ report.summary }}</p></article><article class="panel glass findings"><div class="section-heading"><div><h2>问题明细</h2><p>按风险等级定位并逐项处理</p></div></div><el-collapse><el-collapse-item v-for="(finding, index) in report.result_json.findings" :key="`${finding.file_path}-${index}`"><template #title><div class="finding-title"><SeverityBadge :severity="finding.severity" /><strong>{{ finding.title }}</strong><code>{{ finding.file_path }}{{ finding.line ? `:${finding.line}` : '' }}</code></div></template><div class="finding-body"><p>{{ finding.description }}</p><h4>修复建议</h4><p>{{ finding.remediation }}</p><small>分类：{{ finding.category }}</small></div></el-collapse-item></el-collapse><el-empty v-if="!report.result_json.findings.length" description="未发现需要处理的问题" /></article></div>
<aside><article class="panel glass"><h2>风险分布</h2><ReportChart :counts="severityCounts" /></article><article class="panel glass"><h2>问题分类</h2><ReportChart :counts="report.category_counts" mode="category" /></article></aside></div></template></section></template>
