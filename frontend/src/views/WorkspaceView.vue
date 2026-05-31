<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Document, FolderOpened, Promotion, UploadFilled } from '@element-plus/icons-vue'
import { errorMessage, reviewApi } from '../api/client'
import StatusBadge from '../components/StatusBadge.vue'
import type { ModelNode, ReviewTask } from '../types'
const models = ref<ModelNode[]>([]), selectedModel = ref(''), mode = ref<'text' | 'file' | 'archive'>('text')
const source = ref(''), upload = ref<File>(), task = ref<ReviewTask>(), submitting = ref(false), router = useRouter()
let timer: number | undefined
const canSubmit = computed(() => selectedModel.value && (mode.value === 'text' ? source.value.trim() : upload.value))
onMounted(async () => { try { models.value = (await reviewApi.models()).data; selectedModel.value = models.value[0]?.id || '' } catch (e) { ElMessage.error(errorMessage(e)) } })
onUnmounted(() => clearInterval(timer))
function setFile(file: { raw: File }) { upload.value = file.raw }
async function submit() {
  if (!canSubmit.value) return ElMessage.warning('请选择模型并提供待审查代码')
  submitting.value = true; task.value = undefined; clearInterval(timer)
  try {
    task.value = mode.value === 'text' ? (await reviewApi.submitText(selectedModel.value, source.value)).data : (await reviewApi.submitFile(mode.value, selectedModel.value, upload.value!)).data
    timer = window.setInterval(poll, 1800); await poll()
  } catch (e) { ElMessage.error(errorMessage(e)) } finally { submitting.value = false }
}
async function poll() { if (!task.value) return; try { task.value = (await reviewApi.get(task.value.id)).data; if (['completed', 'failed'].includes(task.value.status)) clearInterval(timer) } catch (e) { clearInterval(timer); ElMessage.error(errorMessage(e)) } }
function openReport() { if (task.value?.report_id) router.push(`/reports/${task.value.report_id}`); else ElMessage.warning('报告暂不可用，请稍后刷新任务状态') }
</script>
<template><section><header class="page-header"><div><h1>发起代码审查</h1><p>选择输入方式和专业模型，创建一次新的 C 语言代码审计。</p></div></header>
<div class="workspace-grid"><div class="panel glass"><div class="section-heading"><div><h2>提交代码</h2><p>仅支持 C 语言源文件与头文件</p></div><el-select v-model="selectedModel" placeholder="请选择模型" class="model-select"><el-option v-for="model in models" :key="model.id" :value="model.id" :label="model.display_name"><span>{{ model.display_name }}</span><small>{{ model.model_identifier }}</small></el-option></el-select></div>
<el-tabs v-model="mode" class="input-tabs"><el-tab-pane name="text"><template #label><el-icon><Document /></el-icon> 文本粘贴</template><el-input v-model="source" type="textarea" :rows="18" placeholder="在此粘贴 .c / .h 代码..." class="code-input" /></el-tab-pane><el-tab-pane name="file"><template #label><el-icon><UploadFilled /></el-icon> 单文件</template><el-upload drag :auto-upload="false" :limit="1" accept=".c,.h" :on-change="setFile"><el-icon class="el-icon--upload"><UploadFilled /></el-icon><div>拖入或点击选择单个 <b>.c / .h</b> 文件</div></el-upload></el-tab-pane><el-tab-pane name="archive"><template #label><el-icon><FolderOpened /></el-icon> 项目压缩包</template><el-upload drag :auto-upload="false" :limit="1" accept=".zip" :on-change="setFile"><el-icon class="el-icon--upload"><FolderOpened /></el-icon><div>拖入或点击选择 <b>.zip</b> 项目压缩包</div></el-upload></el-tab-pane></el-tabs><div class="submit-row"><span>提交后系统将自动排队并实时更新进度</span><el-button type="primary" size="large" :icon="Promotion" :disabled="!canSubmit" :loading="submitting" @click="submit">开始智能审查</el-button></div></div>
<aside class="panel glass task-panel"><h2>任务状态</h2><div v-if="!task" class="empty-state">提交代码后，可在此查看审查进度与结果。</div><template v-else><div class="task-title"><div><strong>{{ task.display_name }}</strong><small>{{ task.file_count }} 个文件</small></div><StatusBadge :status="task.status" /></div><el-progress :percentage="task.progress" :status="task.status === 'failed' ? 'exception' : task.status === 'completed' ? 'success' : undefined" /><dl><div><dt>发现问题</dt><dd>{{ task.finding_count }}</dd></div><div><dt>耗时</dt><dd>{{ task.duration_ms ? `${(task.duration_ms / 1000).toFixed(1)}s` : '--' }}</dd></div></dl><el-alert v-if="task.error_message" :title="task.error_message" type="error" :closable="false" show-icon /><el-button v-if="task.status === 'completed'" type="primary" class="full" @click="openReport">查看审查报告</el-button></template></aside></div></section></template>
