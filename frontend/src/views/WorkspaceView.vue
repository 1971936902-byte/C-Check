<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Document, FolderOpened, Plus, Promotion, UploadFilled, View } from '@element-plus/icons-vue'
import { errorMessage, MOCK_API_ENABLED, reviewApi } from '../api/client'
import StatusBadge from '../components/StatusBadge.vue'
import type { ModelNode, ReviewTask } from '../types'
import { useAuthStore } from '../stores/auth'
import { activeUpload, canSubmitReview, hasReviewInput, type InputMode } from './workspace-input'
import { renderHighlightedCSource } from './c-highlight'
import { displayNameFromFolder, formatBytes, isCSourceFile, modelDescriptionText } from './workspace-files'
import { ALL_CHECK_TYPES, deriveReviewProgressSummary, taskDisplayName, taskSubmissionCountLabel } from './task-progress'

const models = ref<ModelNode[]>([])
const selectedModel = ref('')
const taskDraftStarted = ref(false)
const taskName = ref('')
const mode = ref<InputMode>('text')
const source = ref('')
const singleFile = ref<File>()
const archiveFile = ref<File>()
const folderFiles = ref<File[]>([])
const singleFileInput = ref<HTMLInputElement>()
const archiveFileInput = ref<HTMLInputElement>()
const folderInput = ref<HTMLInputElement>()
const tasks = ref<ReviewTask[]>([])
const selectedTaskId = ref('')
const submitting = ref(false)
const logVisible = ref(false)
const sourcePreviewVisible = ref(false)
const sourcePreviewLoading = ref(false)
const sourcePreviewName = ref('')
const sourcePreviewText = ref('')
const router = useRouter()
const auth = useAuthStore()
const checkTypes = ref<string[]>(ALL_CHECK_TYPES.map((item) => item.value))
let timer: number | undefined
const WORKSPACE_TASK_IDS_KEY = 'c-check-workspace-task-ids'

const upload = computed(() => activeUpload(mode.value, singleFile.value, archiveFile.value))
const folderSourceFiles = computed(() => folderFiles.value.filter(isCSourceFile))
const folderTotalBytes = computed(() => folderSourceFiles.value.reduce((total, file) => total + file.size, 0))
const trimmedTaskName = computed(() => taskName.value.trim())
const highlightedSourcePreview = computed(() => renderHighlightedCSource(sourcePreviewText.value))
const canSubmit = computed(() => taskDraftStarted.value && Boolean(trimmedTaskName.value) && canSubmitReview({
  mode: mode.value,
  selectedModel: selectedModel.value,
  sourceText: source.value,
  singleFile: singleFile.value,
  archiveFile: archiveFile.value,
  folderFiles: folderSourceFiles.value,
  checkTypes: checkTypes.value,
}))
const submitBlockReason = computed(() => {
  if (!taskDraftStarted.value) return '请先创建新任务'
  if (!trimmedTaskName.value) return '请输入任务名称'
  if (!models.value.length) return '暂无可用模型，请先在后台配置并启用模型'
  if (!selectedModel.value) return '请选择模型'
  if (!checkTypes.value.length) return '请至少选择一种检查类型'
  if (!hasReviewInput(mode.value, source.value, singleFile.value, archiveFile.value, folderSourceFiles.value)) {
    if (mode.value === 'text') return '请粘贴待审查代码'
    if (mode.value === 'folder') return '请选择包含 .c / .h 文件的项目文件夹'
    return '请先选择或拖入文件'
  }
  return ''
})
const selectedModelInfo = computed(() => models.value.find((model) => model.id === selectedModel.value))
const allChecksSelected = computed(() => checkTypes.value.length === ALL_CHECK_TYPES.length)
const task = computed(() => tasks.value.find((item) => item.id === selectedTaskId.value) || tasks.value[0])
const progressSummary = computed(() => task.value ? deriveReviewProgressSummary(task.value) : undefined)
const activeTasks = computed(() => tasks.value.filter(isActiveTask))
const checkTypeLabelMap = new Map<string, string>(ALL_CHECK_TYPES.map((item) => [item.value, item.label]))

onMounted(async () => {
  try {
    await Promise.all([loadModels(), loadWorkspaceTasks()])
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
})
onUnmounted(() => clearInterval(timer))

async function loadModels() {
  models.value = (await reviewApi.models()).data
  selectedModel.value = models.value.find((model) => model.is_default)?.id || models.value[0]?.id || ''
}

function isActiveTask(item: ReviewTask) {
  return item.status === 'queued' || item.status === 'running'
}

function readRememberedTaskIds() {
  try {
    const ids = JSON.parse(sessionStorage.getItem(WORKSPACE_TASK_IDS_KEY) || '[]')
    return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === 'string') : []
  } catch {
    return []
  }
}

function rememberTask(id: string) {
  const ids = [id, ...readRememberedTaskIds().filter((item) => item !== id)].slice(0, 20)
  sessionStorage.setItem(WORKSPACE_TASK_IDS_KEY, JSON.stringify(ids))
}

function forgetTask(id: string) {
  const ids = readRememberedTaskIds().filter((item) => item !== id)
  sessionStorage.setItem(WORKSPACE_TASK_IDS_KEY, JSON.stringify(ids))
}

function mergeTasks(items: ReviewTask[]) {
  const merged = new Map(tasks.value.map((item) => [item.id, item]))
  for (const item of items) merged.set(item.id, item)
  tasks.value = Array.from(merged.values()).sort((left, right) => {
    if (left.status === 'running' && right.status !== 'running') return -1
    if (right.status === 'running' && left.status !== 'running') return 1
    if (left.status === 'queued' && right.status === 'queued') {
      return (left.queued_ahead_count ?? 0) - (right.queued_ahead_count ?? 0)
    }
    if (left.status === 'queued' && right.status !== 'queued') return -1
    if (right.status === 'queued' && left.status !== 'queued') return 1
    return new Date(right.created_at).getTime() - new Date(left.created_at).getTime()
  })
  if (!selectedTaskId.value || !tasks.value.some((item) => item.id === selectedTaskId.value)) {
    selectedTaskId.value = tasks.value[0]?.id || ''
  }
}

function upsertTask(item: ReviewTask) {
  rememberTask(item.id)
  mergeTasks([item])
  selectedTaskId.value = item.id
  ensurePolling()
}

function removeLocalTask(id: string) {
  forgetTask(id)
  tasks.value = tasks.value.filter((item) => item.id !== id)
  if (selectedTaskId.value === id) selectedTaskId.value = tasks.value[0]?.id || ''
  ensurePolling()
}

async function loadWorkspaceTasks() {
  const [queued, running, remembered] = await Promise.all([
    reviewApi.list({ status: 'queued', limit: 50 }),
    reviewApi.list({ status: 'running', limit: 50 }),
    Promise.allSettled(readRememberedTaskIds().map((id) => reviewApi.get(id))),
  ])
  const rememberedTasks = remembered
    .filter((result): result is PromiseFulfilledResult<{ data: ReviewTask }> => result.status === 'fulfilled')
    .map((result) => result.value.data)
  mergeTasks([...queued.data.items, ...running.data.items, ...rememberedTasks])
  ensurePolling()
}

function ensurePolling() {
  clearInterval(timer)
  timer = undefined
  if (activeTasks.value.length) timer = window.setInterval(poll, 1400)
}

function startTaskDraft() {
  taskDraftStarted.value = true
}

function resetDraftInputs() {
  taskDraftStarted.value = false
  taskName.value = ''
  source.value = ''
  singleFile.value = undefined
  archiveFile.value = undefined
  folderFiles.value = []
  if (singleFileInput.value) singleFileInput.value.value = ''
  if (archiveFileInput.value) archiveFileInput.value.value = ''
  if (folderInput.value) folderInput.value.value = ''
}

function cancelTaskDraft() {
  resetDraftInputs()
}

function clearFile(target: 'file' | 'archive') {
  if (target === 'file') {
    singleFile.value = undefined
    if (singleFileInput.value) singleFileInput.value.value = ''
  } else {
    archiveFile.value = undefined
    if (archiveFileInput.value) archiveFileInput.value.value = ''
  }
}
function chooseSingleFile() { singleFileInput.value?.click() }
function chooseArchiveFile() { archiveFileInput.value?.click() }
function setNativeFile(target: 'file' | 'archive', event: Event) {
  const input = event.target as HTMLInputElement
  const selected = input.files?.[0]
  if (!selected) return
  setSelectedFile(target, selected)
  input.value = ''
}
function setDroppedFile(target: 'file' | 'archive', event: DragEvent) {
  const selected = event.dataTransfer?.files?.[0]
  if (!selected) return
  setSelectedFile(target, selected)
}
function setSelectedFile(target: 'file' | 'archive', selected: File) {
  const name = selected.name.toLowerCase()
  if (target === 'file' && !name.endsWith('.c') && !name.endsWith('.h')) {
    ElMessage.warning('请选择 .c / .h 文件')
    return
  }
  if (target === 'archive' && !name.endsWith('.zip')) {
    ElMessage.warning('请选择 .zip 压缩包')
    return
  }
  if (target === 'file') {
    mode.value = 'file'
    singleFile.value = selected
    archiveFile.value = undefined
    folderFiles.value = []
    taskName.value = selected.name
  } else {
    mode.value = 'archive'
    archiveFile.value = selected
    singleFile.value = undefined
    folderFiles.value = []
    taskName.value = selected.name
  }
}
function toggleAllChecks(value: boolean) { checkTypes.value = value ? ALL_CHECK_TYPES.map((item) => item.value) : [] }
function taskCheckTypesLabel(target: ReviewTask) {
  return (target.check_types || [])
    .map((item) => checkTypeLabelMap.get(item) || item)
    .join('、')
}
function chooseFolder() { folderInput.value?.click() }
function setFolderFiles(event: Event) {
  const input = event.target as HTMLInputElement
  folderFiles.value = Array.from(input.files || []).filter(isCSourceFile)
  if (!folderFiles.value.length) ElMessage.warning('所选文件夹中未找到 .c / .h 文件')
  if (folderFiles.value.length) {
    mode.value = 'folder'
    singleFile.value = undefined
    archiveFile.value = undefined
    taskName.value = displayNameFromFolder(folderFiles.value)
  }
  input.value = ''
}
function clearFolder() { folderFiles.value = [] }

async function previewSingleFile() {
  if (!singleFile.value) return ElMessage.warning('请先选择单个 .c / .h 文件')
  sourcePreviewLoading.value = true
  sourcePreviewName.value = singleFile.value.name
  sourcePreviewVisible.value = true
  try {
    sourcePreviewText.value = await singleFile.value.text()
  } catch {
    sourcePreviewText.value = ''
    ElMessage.error('文件内容读取失败，请确认文件可访问')
  } finally {
    sourcePreviewLoading.value = false
  }
}

async function submit() {
  if (!taskDraftStarted.value) return ElMessage.warning('请先创建新任务')
  if (!trimmedTaskName.value) return ElMessage.warning('请输入任务名称')
  if (!models.value.length) return ElMessage.warning('暂无可用模型，请先在后台配置并启用模型')
  if (!checkTypes.value.length) return ElMessage.warning('请至少选择一种检查类型')
  if (!canSubmit.value) return ElMessage.warning(submitBlockReason.value || '请选择模型并提供待审查代码')
  submitting.value = true
  let created: ReviewTask
  try {
    if (mode.value === 'text') {
      created = (await reviewApi.submitText(selectedModel.value, source.value, checkTypes.value, trimmedTaskName.value)).data
    } else if (mode.value === 'folder') {
      created = (await reviewApi.submitFolder(selectedModel.value, folderSourceFiles.value, checkTypes.value, trimmedTaskName.value)).data
    } else {
      created = (await reviewApi.submitFile(mode.value, selectedModel.value, upload.value!, checkTypes.value, trimmedTaskName.value)).data
    }
    upsertTask(created)
    resetDraftInputs()
    await poll()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    submitting.value = false
  }
}

async function runDemo() {
  submitting.value = true
  try {
    upsertTask((await reviewApi.submitDemoArchive(checkTypes.value)).data)
    await poll()
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    submitting.value = false
  }
}

async function poll() {
  const pollingTasks = activeTasks.value
  if (!pollingTasks.length) {
    ensurePolling()
    return
  }
  try {
    const updates = await Promise.all(pollingTasks.map((item) => reviewApi.get(item.id)))
    mergeTasks(updates.map((item) => item.data))
    ensurePolling()
  } catch (e) {
    clearInterval(timer)
    timer = undefined
    ElMessage.error(errorMessage(e))
  }
}

function openReport() {
  if (task.value?.report_id) router.push(`/reports/${task.value.report_id}`)
  else ElMessage.warning('报告暂不可用，请稍后刷新任务状态')
}

async function removeTask(target: ReviewTask) {
  const action = target.status === 'queued' || target.status === 'running' ? '停止并删除' : '删除'
  try {
    await ElMessageBox.confirm(`确认${action}任务“${taskDisplayName(target)}”？`, action, { type: 'warning' })
    await reviewApi.remove(target.id)
    removeLocalTask(target.id)
    ElMessage.success('任务已删除')
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(errorMessage(e))
  }
}

async function pinTask(target: ReviewTask) {
  try {
    const { data } = await reviewApi.pin(target.id)
    upsertTask(data)
    await loadWorkspaceTasks()
    ElMessage.success('任务已置顶')
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}
</script>

<template>
  <section>
    <header class="page-header">
      <div>
        <h1>代码审查任务</h1>
        <p>先创建任务并设置名称与检查维度，再导入 C 源码生成审查报告。</p>
      </div>
    </header>

    <div class="workspace-grid">
      <div class="panel glass">
        <div v-if="!taskDraftStarted" class="task-setup-empty">
          <button type="button" class="task-setup-plus" aria-label="创建新任务" @click="startTaskDraft">
            <el-icon><Plus /></el-icon>
          </button>
          <strong>创建新任务</strong>
          <span>创建后填写任务名称、检查类型，再导入 C 源码、压缩包或项目文件夹。</span>
        </div>

        <template v-else>
        <div class="section-heading">
          <div>
            <h2>任务信息</h2>
            <p>填写任务名称并选择本次审查的模型与检查维度。</p>
          </div>
          <el-button plain @click="cancelTaskDraft">取消创建</el-button>
        </div>

        <div class="task-config-list">
          <div class="task-config-row">
            <div>
              <strong>任务名称</strong>
              <small>最多 128 个字符</small>
            </div>
            <el-input v-model="taskName" class="task-name-input" maxlength="128" show-word-limit placeholder="请输入任务名称" />
          </div>

          <div class="task-config-row">
            <div>
              <strong>检查类型</strong>
              <small>按审查目标选择重点维度</small>
            </div>
            <el-checkbox :model-value="allChecksSelected" @change="toggleAllChecks">全选</el-checkbox>
            <el-select v-model="checkTypes" multiple collapse-tags collapse-tags-tooltip placeholder="请至少选择一种检查类型" class="check-type-select">
              <el-option v-for="item in ALL_CHECK_TYPES" :key="item.value" :label="item.label" :value="item.value" />
            </el-select>
          </div>

          <div class="task-config-row">
            <div>
              <strong>模型名称</strong>
              <small>选择本次审查使用的推理模式</small>
            </div>
            <el-select v-model="selectedModel" :disabled="!auth.isAdmin || !models.length" :placeholder="models.length ? '请选择模型' : '暂无可用模型'" class="model-select">
              <el-option v-for="model in models" :key="model.id" :value="model.id" :label="model.display_name">
                <span>{{ model.display_name }}</span>
                <small>{{ model.model_identifier }}</small>
              </el-option>
            </el-select>
          </div>
        </div>

        <el-alert
          v-if="!models.length"
          class="model-alert"
          title="暂无可用模型，请先到后台管理中新增并启用模型。"
          type="warning"
          :closable="false"
          show-icon
        />
        <p v-if="selectedModelInfo?.description" class="model-hint">
          {{ modelDescriptionText(selectedModelInfo.description) }}{{ auth.isAdmin ? '' : ' 当前模型由管理员统一配置。' }}
        </p>

        <el-tabs v-model="mode" class="input-tabs">
          <el-tab-pane name="text">
            <template #label><el-icon><Document /></el-icon> 文本粘贴</template>
            <el-input v-model="source" type="textarea" :rows="18" placeholder="在此粘贴 .c / .h 代码..." class="code-input" />
          </el-tab-pane>
          <el-tab-pane name="file">
            <template #label><el-icon><UploadFilled /></el-icon> 单文件</template>
            <input
              ref="singleFileInput"
              class="folder-native-input"
              type="file"
              accept=".c,.h"
              @change="setNativeFile('file', $event)"
            />
            <button type="button" class="folder-picker" @dragover.prevent @drop.prevent="setDroppedFile('file', $event)" @click="chooseSingleFile">
              <el-icon><UploadFilled /></el-icon>
              <strong>拖入或点击选择单个 .c / .h 文件</strong>
              <span>选择后可继续提交为新的排队任务</span>
            </button>
            <div v-if="singleFile" class="file-preview-row">
              <span>{{ singleFile.name }}</span>
              <div>
                <el-button size="small" plain :icon="View" @click="previewSingleFile">预览代码</el-button>
                <el-button size="small" plain @click="clearFile('file')">清空</el-button>
              </div>
            </div>
          </el-tab-pane>
          <el-tab-pane name="archive">
            <template #label><el-icon><FolderOpened /></el-icon> 项目压缩包</template>
            <input
              ref="archiveFileInput"
              class="folder-native-input"
              type="file"
              accept=".zip"
              @change="setNativeFile('archive', $event)"
            />
            <button type="button" class="folder-picker" @dragover.prevent @drop.prevent="setDroppedFile('archive', $event)" @click="chooseArchiveFile">
              <el-icon><FolderOpened /></el-icon>
              <strong>拖入或点击选择 .zip 项目压缩包</strong>
              <span>选择后可继续提交为新的排队任务</span>
            </button>
            <div v-if="archiveFile" class="file-preview-row">
              <span>{{ archiveFile.name }}</span>
              <el-button size="small" plain @click="clearFile('archive')">清空</el-button>
            </div>
          </el-tab-pane>
          <el-tab-pane name="folder">
            <template #label><el-icon><FolderOpened /></el-icon> 项目文件夹</template>
            <input
              ref="folderInput"
              class="folder-native-input"
              type="file"
              multiple
              webkitdirectory
              directory
              @change="setFolderFiles"
            />
            <button type="button" class="folder-picker" @click="chooseFolder">
              <el-icon><FolderOpened /></el-icon>
              <strong>选择项目文件夹</strong>
              <span>自动递归收集 .c / .h 文件并保留子目录路径</span>
            </button>
            <div v-if="folderSourceFiles.length" class="folder-summary">
              <div>
                <strong>{{ folderSourceFiles.length }}</strong>
                <small>个源文件</small>
              </div>
              <div>
                <strong>{{ formatBytes(folderTotalBytes) }}</strong>
                <small>源码大小</small>
              </div>
              <el-button size="small" plain @click="clearFolder">清空</el-button>
            </div>
            <div v-if="folderSourceFiles.length" class="folder-file-list">
              <code v-for="file in folderSourceFiles.slice(0, 8)" :key="file.webkitRelativePath || file.name">
                {{ file.webkitRelativePath || file.name }}
              </code>
              <small v-if="folderSourceFiles.length > 8">还有 {{ folderSourceFiles.length - 8 }} 个文件</small>
            </div>
          </el-tab-pane>
        </el-tabs>

        <div class="submit-row">
          <span>提交后系统将自动排队并实时更新进度。</span>
          <el-tooltip :disabled="canSubmit" :content="submitBlockReason" placement="top">
            <span class="submit-action-wrap">
              <el-button class="submit-action-button" type="primary" size="large" :icon="Promotion" :disabled="!canSubmit" :loading="submitting" @click="submit">
                开始智能审查
              </el-button>
            </span>
          </el-tooltip>
        </div>
        </template>
      </div>

      <aside class="panel glass task-panel">
        <div class="task-panel-header">
          <div>
            <h2>任务状态</h2>
            <p v-if="!tasks.length">提交代码后，可在此查看审查进度与结果。</p>
          </div>
          <el-button v-if="MOCK_API_ENABLED && !tasks.length" plain @click="runDemo">加载多文件演示</el-button>
        </div>

        <div v-if="tasks.length" class="task-list">
          <button
            v-for="item in tasks"
            :key="item.id"
            type="button"
            class="task-list-item"
            :class="{ 'task-list-item-active': item.id === selectedTaskId }"
            @click="selectedTaskId = item.id"
          >
            <span class="task-list-title">
              <strong>{{ taskDisplayName(item) }}</strong>
              <StatusBadge :status="item.status" />
            </span>
            <small v-if="item.status === 'queued'" class="task-queue-note">
              前方还有 {{ item.queued_ahead_count ?? 0 }} 个任务
              <b v-if="item.queue_priority">已置顶</b>
            </small>
          </button>
        </div>

        <template v-if="task && progressSummary">
          <div class="task-title">
            <div>
              <strong>{{ taskDisplayName(task) }}</strong>
              <small>
                {{ taskSubmissionCountLabel(task) }} · {{ task.check_types?.length || 0 }} 项检查
                <template v-if="taskCheckTypesLabel(task)">（{{ taskCheckTypesLabel(task) }}）</template>
              </small>
            </div>
            <StatusBadge :status="task.status" />
          </div>
          <el-progress v-if="task.status === 'running'" :percentage="task.progress" />
          <div v-if="task.status === 'running'" class="task-focus-row">
            <div class="task-focus-main">
              <i :class="`file-state file-state-${progressSummary.state}`"></i>
              <div>
                <small>{{ progressSummary.stateLabel }}</small>
                <code>{{ progressSummary.currentLabel }}</code>
              </div>
            </div>
            <div class="task-focus-remaining">
              <small>剩余文件</small>
              <strong>{{ progressSummary.remainingCount }}</strong>
            </div>
          </div>
          <el-alert v-if="task.error_message" :title="task.error_message" type="error" :closable="false" show-icon />
          <el-button v-if="task.model_log" plain :icon="View" class="report-button" @click="logVisible = true">查看模型日志</el-button>
          <el-button v-if="task.status === 'completed'" type="primary" class="report-button" @click="openReport">查看审查报告</el-button>
          <el-button v-if="task.status === 'queued'" plain type="primary" class="report-button" @click="pinTask(task)">置顶任务</el-button>
          <el-button plain type="danger" class="report-button" @click="removeTask(task)">
            {{ task.status === 'queued' || task.status === 'running' ? '停止并删除任务' : '删除任务' }}
          </el-button>
        </template>
      </aside>
    </div>

    <el-dialog v-model="logVisible" :title="`模型日志 · ${task?.display_name || ''}`" width="820px">
      <div class="markdown-preview"><pre>{{ task?.model_log || '暂无模型日志' }}</pre></div>
      <template #footer><el-button @click="logVisible = false">关闭</el-button></template>
    </el-dialog>

    <el-dialog v-model="sourcePreviewVisible" :title="`代码预览 · ${sourcePreviewName}`" width="860px">
      <div v-loading="sourcePreviewLoading" class="code-preview" v-html="highlightedSourcePreview"></div>
      <template #footer><el-button @click="sourcePreviewVisible = false">关闭</el-button></template>
    </el-dialog>
  </section>
</template>

