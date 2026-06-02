<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Document, FolderOpened, Promotion, UploadFilled } from '@element-plus/icons-vue'
import { errorMessage, MOCK_API_ENABLED, reviewApi } from '../api/client'
import StatusBadge from '../components/StatusBadge.vue'
import type { ModelNode, ReviewTask } from '../types'
import { useAuthStore } from '../stores/auth'
import { activeUpload, type InputMode } from './workspace-input'
import { ALL_CHECK_TYPES, deriveReviewProgressSummary, taskDisplayName, taskSubmissionCountLabel } from './task-progress'
const models = ref<ModelNode[]>([]), selectedModel = ref(''), mode = ref<InputMode>('text')
const source = ref(''), singleFile = ref<File>(), archiveFile = ref<File>(), task = ref<ReviewTask>(), submitting = ref(false), router = useRouter(), auth = useAuthStore()
const checkTypes = ref<string[]>(ALL_CHECK_TYPES.map((item) => item.value))
let timer: number | undefined
const upload = computed(() => activeUpload(mode.value, singleFile.value, archiveFile.value))
const canSubmit = computed(() => selectedModel.value && checkTypes.value.length && (mode.value === 'text' ? source.value.trim() : upload.value))
const selectedModelInfo = computed(() => models.value.find((model) => model.id === selectedModel.value))
const allChecksSelected = computed(() => checkTypes.value.length === ALL_CHECK_TYPES.length)
const progressSummary = computed(() => task.value ? deriveReviewProgressSummary(task.value) : undefined)
onMounted(async () => { try { models.value = (await reviewApi.models()).data; selectedModel.value = models.value[0]?.id || '' } catch (e) { ElMessage.error(errorMessage(e)) } })
onUnmounted(() => clearInterval(timer))
function setFile(target: 'file' | 'archive', file: { raw: File }) { if (target === 'file') singleFile.value = file.raw; else archiveFile.value = file.raw }
function clearFile(target: 'file' | 'archive') { if (target === 'file') singleFile.value = undefined; else archiveFile.value = undefined }
function setSingleFile(file: { raw: File }) { setFile('file', file) }
function setArchiveFile(file: { raw: File }) { setFile('archive', file) }
function toggleAllChecks(value: boolean) { checkTypes.value = value ? ALL_CHECK_TYPES.map((item) => item.value) : [] }
async function submit() {
  if (!checkTypes.value.length) return ElMessage.warning('请至少选择一种检查类型')
  if (!canSubmit.value) return ElMessage.warning('请选择模型并提供待审查代码')
  submitting.value = true; task.value = undefined; clearInterval(timer)
  try {
    task.value = mode.value === 'text' ? (await reviewApi.submitText(selectedModel.value, source.value, checkTypes.value)).data : (await reviewApi.submitFile(mode.value, selectedModel.value, upload.value!, checkTypes.value)).data
    timer = window.setInterval(poll, 1400); await poll()
  } catch (e) { ElMessage.error(errorMessage(e)) } finally { submitting.value = false }
}
async function runDemo() { submitting.value = true; task.value = undefined; clearInterval(timer); try { task.value = (await reviewApi.submitDemoArchive(checkTypes.value)).data; timer = window.setInterval(poll, 1400); await poll() } catch (e) { ElMessage.error(errorMessage(e)) } finally { submitting.value = false } }
async function poll() { if (!task.value) return; try { task.value = (await reviewApi.get(task.value.id)).data; if (['completed', 'failed'].includes(task.value.status)) clearInterval(timer) } catch (e) { clearInterval(timer); ElMessage.error(errorMessage(e)) } }
function openReport() { if (task.value?.report_id) router.push(`/reports/${task.value.report_id}`); else ElMessage.warning('报告暂不可用，请稍后刷新任务状态') }
</script>
<template><section><header class="page-header"><div><h1>新建代码审查</h1><p>提交 C 语言源码，系统将按所选检查维度识别风险并生成审查报告。</p></div></header>
<div class="workspace-grid"><div class="panel glass"><div class="section-heading"><div><h2>提交代码</h2><p>仅支持 C 语言源文件与头文件</p></div><el-select v-model="selectedModel" :disabled="!auth.isAdmin" placeholder="请选择模型" class="model-select"><el-option v-for="model in models" :key="model.id" :value="model.id" :label="model.display_name"><span>{{ model.display_name }}</span><small>{{ model.model_identifier }}</small></el-option></el-select></div><p v-if="selectedModelInfo?.description" class="model-hint">{{ selectedModelInfo.description }}{{ auth.isAdmin ? '' : ' 当前模型由管理员统一配置。' }}</p>
<div class="check-type-row"><div><strong>检查类型</strong><small>可按审查目标选择重点维度</small></div><el-checkbox :model-value="allChecksSelected" @change="toggleAllChecks">全选</el-checkbox><el-select v-model="checkTypes" multiple collapse-tags collapse-tags-tooltip placeholder="请至少选择一种检查类型" class="check-type-select"><el-option v-for="item in ALL_CHECK_TYPES" :key="item.value" :label="item.label" :value="item.value" /></el-select></div>
<el-tabs v-model="mode" class="input-tabs"><el-tab-pane name="text"><template #label><el-icon><Document /></el-icon> 文本粘贴</template><el-input v-model="source" type="textarea" :rows="18" placeholder="在此粘贴 .c / .h 代码..." class="code-input" /></el-tab-pane><el-tab-pane name="file"><template #label><el-icon><UploadFilled /></el-icon> 单文件</template><el-upload drag :auto-upload="false" :limit="1" accept=".c,.h" :on-change="setSingleFile" :on-remove="() => clearFile('file')"><el-icon class="el-icon--upload"><UploadFilled /></el-icon><div>拖入或点击选择单个 <b>.c / .h</b> 文件</div></el-upload></el-tab-pane><el-tab-pane name="archive"><template #label><el-icon><FolderOpened /></el-icon> 项目压缩包</template><el-upload drag :auto-upload="false" :limit="1" accept=".zip" :on-change="setArchiveFile" :on-remove="() => clearFile('archive')"><el-icon class="el-icon--upload"><FolderOpened /></el-icon><div>拖入或点击选择 <b>.zip</b> 项目压缩包</div></el-upload></el-tab-pane></el-tabs><div class="submit-row"><span>提交后系统将自动排队并实时更新进度</span><el-button type="primary" size="large" :icon="Promotion" :disabled="!canSubmit" :loading="submitting" @click="submit">开始智能审查</el-button></div></div>
<aside class="panel glass task-panel"><div class="task-panel-header"><div><h2>任务状态</h2><p v-if="!task">提交代码后，可在此查看审查进度与结果。</p></div><el-button v-if="MOCK_API_ENABLED && !task" plain @click="runDemo">加载多文件演示</el-button></div><template v-if="task && progressSummary"><div class="task-title"><div><strong>{{ taskDisplayName(task) }}</strong><small>{{ taskSubmissionCountLabel(task) }} · {{ task.check_types?.length || 0 }} 项检查</small></div><StatusBadge :status="task.status" /></div><el-progress :percentage="task.progress" :status="task.status === 'failed' ? 'exception' : task.status === 'completed' ? 'success' : undefined" /><div class="task-focus-row"><div class="task-focus-main"><i :class="`file-state file-state-${progressSummary.state}`"></i><div><small>{{ progressSummary.stateLabel }}</small><code>{{ progressSummary.currentLabel }}</code></div></div><div class="task-focus-remaining"><small>剩余文件</small><strong>{{ progressSummary.remainingCount }}</strong></div></div><el-alert v-if="task.error_message" :title="task.error_message" type="error" :closable="false" show-icon /><el-button v-if="task.status === 'completed'" type="primary" class="report-button" @click="openReport">查看审查报告</el-button></template></aside></div></section></template>
