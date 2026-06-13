<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Download } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { errorMessage, reportApi } from '../api/client'
import ReportChart from '../components/ReportChart.vue'
import SeverityBadge from '../components/SeverityBadge.vue'
import type { Finding, Report, Severity } from '../types'
import { scoreTone } from './report-metrics'

const severityTabs: Array<{ key: Severity; label: string; empty: string }> = [
  { key: 'high', label: '高危', empty: '暂无高危问题' },
  { key: 'medium', label: '中危', empty: '暂无中危问题' },
  { key: 'low', label: '低危', empty: '暂无低危问题' },
  { key: 'suggestion', label: '建议', empty: '暂无建议项' },
]

const route = useRoute()
const report = ref<Report>()
const loading = ref(true)
const activeSeverity = ref<Severity>('high')
const pageSize = ref(20)
const pages = reactive<Record<Severity, number>>({
  high: 1,
  medium: 1,
  low: 1,
  suggestion: 1,
})

const allFindings = computed(() => report.value?.result_json.findings ?? [])
const severityCounts = computed<Record<string, number>>(() =>
  report.value
    ? {
        high: report.value.high_count,
        medium: report.value.medium_count,
        low: report.value.low_count,
        suggestion: report.value.suggestion_count,
      }
    : { high: 0, medium: 0, low: 0, suggestion: 0 },
)
const findingsBySeverity = computed<Record<Severity, Finding[]>>(() => ({
  high: allFindings.value.filter((finding) => finding.severity === 'high'),
  medium: allFindings.value.filter((finding) => finding.severity === 'medium'),
  low: allFindings.value.filter((finding) => finding.severity === 'low'),
  suggestion: allFindings.value.filter((finding) => finding.severity === 'suggestion'),
}))
const visibleFindings = computed(() => {
  const current = pages[activeSeverity.value]
  const start = (current - 1) * pageSize.value
  return findingsBySeverity.value[activeSeverity.value].slice(start, start + pageSize.value)
})
const activeTotal = computed(() => findingsBySeverity.value[activeSeverity.value].length)

onMounted(async () => {
  try {
    report.value = (await reportApi.get(String(route.params.id))).data
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    loading.value = false
  }
})

async function download(format: 'markdown' | 'pdf') {
  if (!report.value) return
  try {
    const { data } = await reportApi.download(report.value.id, format)
    const url = URL.createObjectURL(data)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `report-${report.value.id}.${format === 'markdown' ? 'md' : 'pdf'}`
    anchor.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    ElMessage.error(errorMessage(error))
  }
}

function locationText(finding: Finding) {
  return `${finding.file_path}${finding.line ? `:${finding.line}` : ''}`
}
</script>

<template>
  <section v-loading="loading">
    <header class="page-header">
      <div>
        <h1>审查报告</h1>
        <p>结构化呈现代码风险、问题定位与修复建议。</p>
      </div>
      <div>
        <el-button :icon="Download" @click="download('markdown')">下载 Markdown</el-button>
        <el-button type="primary" :icon="Download" @click="download('pdf')">下载 PDF</el-button>
      </div>
    </header>

    <template v-if="report">
      <div class="metric-row">
        <div class="metric-card glass score">
          <span>综合评分</span>
          <strong :class="`metric-${scoreTone(report.score)}`">{{ report.score }}</strong>
          <small>/ 100</small>
        </div>
        <div class="metric-card glass">
          <span>高危问题</span>
          <strong class="metric-danger">{{ report.high_count }}</strong>
        </div>
        <div class="metric-card glass">
          <span>中危问题</span>
          <strong class="metric-warning">{{ report.medium_count }}</strong>
        </div>
        <div class="metric-card glass">
          <span>全部发现</span>
          <strong class="metric-info">{{ allFindings.length }}</strong>
        </div>
      </div>

      <div class="report-grid">
        <div>
          <article class="panel glass findings">
            <div class="section-heading">
              <div>
                <h2>问题明细</h2>
                <p>按风险等级分栏查看，条目较多时分页处理。</p>
              </div>
              <span class="finding-total">共 {{ allFindings.length }} 条</span>
            </div>

            <el-tabs v-model="activeSeverity" class="finding-tabs">
              <el-tab-pane
                v-for="tab in severityTabs"
                :key="tab.key"
                :name="tab.key"
                :label="`${tab.label} (${findingsBySeverity[tab.key].length})`"
              >
                <el-collapse v-if="findingsBySeverity[tab.key].length">
                  <el-collapse-item
                    v-for="(finding, index) in visibleFindings"
                    :key="`${finding.severity}-${finding.file_path}-${finding.line ?? 'unknown'}-${index}`"
                  >
                    <template #title>
                      <div class="finding-title">
                        <SeverityBadge :severity="finding.severity" />
                        <strong>{{ finding.title }}</strong>
                        <code>{{ locationText(finding) }}</code>
                      </div>
                    </template>

                    <div class="finding-body">
                      <p>{{ finding.description }}</p>
                      <h4>修复建议</h4>
                      <p>{{ finding.remediation }}</p>

                      <div v-if="finding.code_snippet?.length || finding.fixed_snippet?.length" class="diff-grid">
                        <section v-if="finding.code_snippet?.length" class="code-panel">
                          <header>
                            <span>问题代码</span>
                            <code>{{ finding.file_path }}</code>
                          </header>
                          <div
                            v-for="line in finding.code_snippet"
                            :key="`source-${line.line}-${line.content}`"
                            :class="['code-line', `line-${line.kind}`]"
                          >
                            <span>{{ line.line }}</span>
                            <i>{{ line.kind === 'removed' ? '-' : ' ' }}</i>
                            <code>{{ line.content }}</code>
                          </div>
                        </section>

                        <section v-if="finding.fixed_snippet?.length" class="code-panel">
                          <header>
                            <span>建议修改</span>
                            <code>{{ finding.file_path }}</code>
                          </header>
                          <div
                            v-for="line in finding.fixed_snippet"
                            :key="`fixed-${line.line}-${line.content}`"
                            :class="['code-line', `line-${line.kind}`]"
                          >
                            <span>{{ line.line }}</span>
                            <i>{{ line.kind === 'added' ? '+' : ' ' }}</i>
                            <code>{{ line.content }}</code>
                          </div>
                        </section>
                      </div>

                      <small>分类：{{ finding.category }}</small>
                    </div>
                  </el-collapse-item>
                </el-collapse>

                <el-empty v-else :description="tab.empty" />
              </el-tab-pane>
            </el-tabs>

            <div v-if="activeTotal > pageSize" class="finding-pagination">
              <el-pagination
                v-model:current-page="pages[activeSeverity]"
                v-model:page-size="pageSize"
                :page-sizes="[10, 20, 50, 100]"
                :total="activeTotal"
                layout="total, sizes, prev, pager, next"
              />
            </div>
          </article>
        </div>

        <aside>
          <article class="panel glass">
            <h2>风险分布</h2>
            <ReportChart :counts="severityCounts" />
          </article>
          <article class="panel glass">
            <h2>问题分类</h2>
            <ReportChart :counts="report.category_counts" mode="category" />
          </article>
        </aside>
      </div>
    </template>
  </section>
</template>

<style scoped>
.finding-total {
  color: var(--text-muted);
  font-size: 13px;
  white-space: nowrap;
}

.finding-tabs {
  margin-top: 12px;
}

.finding-pagination {
  display: flex;
  justify-content: flex-end;
  padding-top: 16px;
}

@media (max-width: 720px) {
  .finding-pagination {
    justify-content: flex-start;
    overflow-x: auto;
  }
}
</style>
