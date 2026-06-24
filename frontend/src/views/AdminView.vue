<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CirclePlus, Connection, Download, Refresh } from '@element-plus/icons-vue'
import { adminApi, codeIndexApi, errorMessage } from '../api/client'
import StatusBadge from '../components/StatusBadge.vue'
import type { AdminTask, AdminUser, CodeIndexEdge, CodeIndexObservability, CodeIndexSymbol, Dashboard, ModelCatalogItem, ModelDeployment, ModelNode, Prompt, ResourceSnapshot, TaskStatus } from '../types'
import { validateModel, validateNewUser, validatePrompt } from './form-validation'

const active = ref('dashboard')
const dashboard = ref<Dashboard>()
const resources = ref<ResourceSnapshot>()
const users = ref<AdminUser[]>([])
const models = ref<ModelNode[]>([])
const modelCatalog = ref<ModelCatalogItem[]>([])
const modelDeployments = ref<ModelDeployment[]>([])
const prompts = ref<Prompt[]>([])
const tasks = ref<AdminTask[]>([])
const ragTaskId = ref('')
const ragSymbols = ref<CodeIndexSymbol[]>([])
const ragEdges = ref<CodeIndexEdge[]>([])
const ragObservability = ref<CodeIndexObservability>()
const ragLoading = ref(false)
const selectedGraphNodeId = ref('')
const resourceSamples = ref<ResourceSnapshot[]>([])
const taskStatus = ref<TaskStatus | ''>('')
const loading = ref(false)
const resourceLoading = ref(false)
const autoRefresh = ref(true)
const modelTableKey = ref(0)
const userDialog = ref(false)
const modelDialog = ref(false)
const deploymentDialog = ref(false)
const deploymentLogDialog = ref(false)
const deploymentSubmitting = ref(false)
const selectedDeployment = ref<ModelDeployment>()
const promptDialog = ref(false)
const editingModel = ref<string>()
const editingPrompt = ref<string>()
const userForm = reactive({ username: '', password: '', role: 'user' })
const modelForm = reactive({ display_name: '', model_identifier: '', base_url: '', api_key: '', timeout_seconds: 120, is_enabled: true, description: '' })
const deploymentForm = reactive({ catalog_key: '', source: 'modelscope', base_url: '', auto_base_url: true, served_model_name: '', api_key: '', port: 8101, timeout_seconds: 180, auto_register: true })
const promptBody = ref('')
let resourceTimer: number | undefined
let deploymentTimer: number | undefined

const date = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false })
const percent = (value?: number | null) => Math.max(0, Math.min(100, Number(value ?? 0)))
const metric = (value?: number | null, digits = 1) => value == null ? '--' : value.toFixed(digits)
const gpuLabel = (indices?: number[] | null) => indices?.length ? indices.map((index) => `GPU ${index}`).join(', ') : '--'
const tpLabel = (size?: number | null) => `TP ${size || 1}`
const bytes = (value?: number | null) => {
  if (value == null) return '--'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = value
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1 }
  return `${size.toFixed(unit ? 1 : 0)} ${units[unit]}`
}
const mb = (value?: number | null) => value == null ? '--' : `${value.toFixed(0)} MB`
const peakLabel = (value?: number | null, suffix = '%', digits = 1) => value == null ? '峰值 --' : `峰值 ${value.toFixed(digits)}${suffix}`
const progressStatus = (value?: number | null) => {
  const n = percent(value)
  if (n >= 90) return 'exception'
  if (n >= 70) return 'warning'
  return undefined
}
const latestResourceTime = computed(() => resources.value ? date(resources.value.captured_at) : '--')
const taskRunningPercent = computed(() => {
  const total = resources.value?.tasks.tasks || 0
  if (!total) return 0
  return Number((resources.value!.tasks.running_tasks / total * 100).toFixed(1))
})
const sampleWindowMinutes = computed(() => {
  const first = resourceSamples.value[0]?.captured_at
  const last = resourceSamples.value[resourceSamples.value.length - 1]?.captured_at
  if (!first || !last) return 0
  return Math.max(0, Math.round((new Date(last).getTime() - new Date(first).getTime()) / 60000))
})
const maxNumber = (values: Array<number | null | undefined>) => {
  const available = values.filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
  return available.length ? Math.max(...available) : undefined
}
const resourcePeaks = computed(() => ({
  cpu_percent: maxNumber(resourceSamples.value.map((item) => item.system.cpu_percent)),
  memory_percent: maxNumber(resourceSamples.value.map((item) => item.system.memory_percent)),
  disk_percent: maxNumber(resourceSamples.value.map((item) => item.system.disk_percent)),
  running_tasks: maxNumber(resourceSamples.value.map((item) => item.tasks.running_tasks)),
  gpu_count: maxNumber(resourceSamples.value.map((item) => item.gpus.length)),
}))
const modelRuntimePeaks = (nodeId: string) => {
  const samples = resourceSamples.value.map((item) => item.models.find((model) => model.node_id === nodeId)).filter(Boolean)
  return {
    prompt_throughput_tps: maxNumber(samples.map((model) => model?.prompt_throughput_tps)),
    generation_throughput_tps: maxNumber(samples.map((model) => model?.generation_throughput_tps)),
    running_requests: maxNumber(samples.map((model) => model?.running_requests)),
    pending_requests: maxNumber(samples.map((model) => model?.pending_requests)),
    gpu_kv_cache_usage_percent: maxNumber(samples.map((model) => model?.gpu_kv_cache_usage_percent)),
  }
}
const selectedCatalog = computed(() => modelCatalog.value.find((item) => item.key === deploymentForm.catalog_key))
const generatedDeploymentBaseUrl = computed(() => `http://127.0.0.1:${deploymentForm.port || selectedCatalog.value?.default_port || 8101}`)
const deploymentSourceOptions = [
  { label: 'HuggingFace', value: 'huggingface' },
  { label: 'ModelScope', value: 'modelscope' },
]
const defaultModel = computed(() => models.value.find((model) => model.is_default))
const failedDeployments = computed(() => modelDeployments.value.filter((item) => item.status === 'failed'))
const activeDeployments = computed(() => modelDeployments.value.filter((item) => item.status === 'queued' || item.status === 'running'))
const isDeploymentLocked = computed(() => deploymentSubmitting.value || activeDeployments.value.length > 0)
const succeededDeploymentNodeIds = computed(() => new Set(
  modelDeployments.value
    .filter((item) => item.status === 'succeeded' && item.model_node_id)
    .map((item) => item.model_node_id as string),
))
const deployedModelNodes = computed(() => models.value
  .filter((model) => model.is_enabled && (model.is_default || succeededDeploymentNodeIds.value.has(model.id)))
  .sort((a, b) => Number(b.is_default) - Number(a.is_default) || a.display_name.localeCompare(b.display_name)))
const indexedTasks = computed(() => tasks.value.filter((task) => ['completed', 'running', 'failed'].includes(task.status)))
const selectedRagTask = computed(() => tasks.value.find((task) => task.id === ragTaskId.value))
const selectedGraphNode = computed(() => graphNodes.value.find((node) => node.id === selectedGraphNodeId.value))
const ragStats = computed(() => {
  const stats = ragObservability.value?.stats || {}
  return {
    files: Number(stats.files || 0),
    symbols: Number(stats.symbols || ragSymbols.value.length),
    edges: Number(stats.edges || ragEdges.value.length),
    chunks: Number(stats.chunks || 0),
    embeddings: Number(stats.embeddings || 0),
    qdrant_points: Number(stats.qdrant_points || 0),
  }
})
const graphSymbolCounts = computed(() => countBy(ragSymbols.value.map((symbol) => symbol.kind)))
const graphEdgeCounts = computed(() => countBy(ragEdges.value.map((edge) => edge.edge_type)))
const graphToolCounts = computed(() => countBy(ragSymbols.value.map((symbol) => symbol.source_tool)))
const graphSymbolCountEntries = computed(() => Object.entries(graphSymbolCounts.value).sort((a, b) => b[1] - a[1]))
const graphEdgeCountEntries = computed(() => Object.entries(graphEdgeCounts.value).sort((a, b) => b[1] - a[1]))
const graphToolCountEntries = computed(() => Object.entries(graphToolCounts.value).sort((a, b) => b[1] - a[1]))
const graphBucketEntries = computed(() => Object.entries(ragObservability.value?.bucket_counts || {}).sort((a, b) => b[1] - a[1]))
const symbolById = computed(() => new Map(ragSymbols.value.map((symbol) => [symbol.id, symbol])))
const visibleSymbols = computed(() => {
  const rank: Record<string, number> = { function: 0, declaration: 1, macro: 2, typedef: 3, struct: 4, enum: 5, global_variable: 6 }
  return [...ragSymbols.value]
    .sort((a, b) => (rank[a.kind] ?? 20) - (rank[b.kind] ?? 20) || a.file_path.localeCompare(b.file_path) || a.start_line - b.start_line)
    .slice(0, 80)
})
const graphNodes = computed(() => {
  const symbols = visibleSymbols.value
  const filePaths = [...new Set(symbols.map((symbol) => symbol.file_path))].slice(0, 24)
  const nodes: Array<{ id: string; label: string; kind: string; file_path?: string; line?: number; source_tool?: string; x: number; y: number }> = []
  const width = 920
  const height = 390
  filePaths.forEach((file, index) => {
    const y = 58 + index * Math.max(26, Math.min(54, 280 / Math.max(1, filePaths.length - 1)))
    nodes.push({ id: `file:${file}`, label: compactPath(file), kind: 'file', file_path: file, x: 110, y })
  })
  symbols.forEach((symbol, index) => {
    const angle = (index / Math.max(1, symbols.length)) * Math.PI * 2
    const ring = 1 + (index % 3) * 0.18
    const x = width * 0.62 + Math.cos(angle) * 230 * ring
    const y = height * 0.5 + Math.sin(angle) * 140 * ring
    nodes.push({ id: symbol.id, label: symbol.name, kind: symbol.kind, file_path: symbol.file_path, line: symbol.start_line, source_tool: symbol.source_tool, x, y })
  })
  return nodes
})
const graphNodeIds = computed(() => new Set(graphNodes.value.map((node) => node.id)))
const graphLinks = computed(() => {
  const links: Array<{ id: string; source: string; target: string; edge_type: string; line?: number | null }> = []
  for (const symbol of visibleSymbols.value) {
    const fileId = `file:${symbol.file_path}`
    if (graphNodeIds.value.has(fileId)) links.push({ id: `file-edge:${symbol.id}`, source: fileId, target: symbol.id, edge_type: 'FILE_CONTAINS_SYMBOL' })
  }
  for (const edge of ragEdges.value) {
    if (edge.target_id && graphNodeIds.value.has(edge.source_id) && graphNodeIds.value.has(edge.target_id)) {
      links.push({ id: edge.id, source: edge.source_id, target: edge.target_id, edge_type: edge.edge_type, line: edge.line })
    }
  }
  return links.slice(0, 160)
})
const graphNodePosition = computed(() => new Map(graphNodes.value.map((node) => [node.id, node])))

const isDefaultCatalogModel = (item: ModelCatalogItem) => {
  const model = defaultModel.value
  if (!model) return false
  return model.model_identifier === item.model_identifier || model.model_identifier === item.default_served_model_name
}

const countBy = (values: string[]) => values.reduce<Record<string, number>>((acc, value) => {
  const key = value || 'unknown'
  acc[key] = (acc[key] || 0) + 1
  return acc
}, {})

const compactPath = (path: string) => {
  const parts = path.split(/[\\/]/).filter(Boolean)
  if (parts.length <= 2) return path
  return `${parts[0]}/.../${parts[parts.length - 1]}`
}

const nodeClass = (kind: string) => {
  if (kind === 'file') return 'graph-node-file'
  if (kind === 'function') return 'graph-node-function'
  if (kind === 'declaration') return 'graph-node-declaration'
  if (kind === 'macro') return 'graph-node-macro'
  return 'graph-node-symbol'
}

const edgeColor = (type: string) => {
  if (type === 'FUNCTION_CALLS_FUNCTION') return '#3d82c4'
  if (type === 'FILE_CONTAINS_SYMBOL') return '#9fb6c9'
  if (type.includes('INCLUDES')) return '#75a66b'
  if (type.includes('USES')) return '#d19a45'
  return '#8d9fb1'
}

const isDeployedCatalogModel = (item: ModelCatalogItem) => {
  const identifiers = new Set([
    item.key,
    item.model_identifier,
    item.default_served_model_name,
    item.huggingface_repo,
    item.modelscope_repo,
  ].filter(Boolean))
  return models.value.some((model) => (
    (model.is_default || succeededDeploymentNodeIds.value.has(model.id))
    && model.is_enabled
    && (
    identifiers.has(model.model_identifier)
    || identifiers.has(model.display_name)
    )
  ))
}

const isDefaultDeployment = (row: ModelDeployment) => {
  const model = defaultModel.value
  if (!model) return false
  return row.model_node_id === model.id || row.served_model_name === model.model_identifier || row.model_identifier === model.model_identifier
}

const deploymentRowClassName = ({ row }: { row: ModelDeployment }) => isDefaultDeployment(row) ? 'is-default-deployment' : ''
const deploymentStatusLabel = (status: ModelDeployment['status']) => ({
  queued: '排队中',
  running: '部署中',
  succeeded: '成功',
  failed: '失败',
  manual_required: '需手动执行',
}[status] || status)
const deploymentStatusType = (status: ModelDeployment['status']) => {
  if (status === 'failed') return 'danger'
  if (status === 'succeeded') return 'success'
  if (status === 'manual_required') return 'info'
  return 'warning'
}
const deploymentProgressStatus = (row: ModelDeployment) => {
  if (row.status === 'failed') return 'exception'
  if (row.status === 'succeeded') return 'success'
  return undefined
}
const deploymentFailureText = (row: ModelDeployment) => {
  if (row.error_message) return row.error_message
  const log = row.log || ''
  const important = log
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => /error|failed|exception|traceback|docker:/i.test(line))
  if (important.length) return important.slice(-3).join(' / ')
  if (row.status === 'failed') return '部署脚本执行失败，请查看完整日志。'
  if (row.status === 'manual_required') return '自动部署未执行，请查看日志中的手动命令。'
  return ''
}
const deploymentLogPreview = (row: ModelDeployment) => {
  const failure = deploymentFailureText(row)
  if (failure) return failure
  const log = (row.log || '').trim().split('\n').filter(Boolean)
  return log[log.length - 1] || '暂无日志'
}

const withSingleDefault = (items: ModelNode[], defaultId?: string) => {
  const fallback = items.find((model) => model.is_default)?.id
  const activeDefault = defaultId || fallback
  return items.map((model) => ({ ...model, is_default: Boolean(activeDefault && model.id === activeDefault) }))
}

async function load() {
  loading.value = true
  try {
    const [d, u, m, c, md, p, t] = await Promise.all([
      adminApi.dashboard(),
      adminApi.users(),
      adminApi.models(),
      adminApi.modelCatalog(),
      adminApi.modelDeployments(),
      adminApi.prompts(),
      adminApi.tasks(taskStatus.value),
    ])
    dashboard.value = d.data
    users.value = u.data
    models.value = withSingleDefault(m.data)
    modelCatalog.value = c.data
    modelDeployments.value = md.data
    modelTableKey.value += 1
    prompts.value = p.data
    tasks.value = t.data
    if (!ragTaskId.value && t.data.length) ragTaskId.value = t.data.find((task) => task.status === 'completed')?.id || t.data[0].id
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    loading.value = false
  }
}

async function loadResources(silent = false) {
  if (!silent) resourceLoading.value = true
  try {
    const snapshot = (await adminApi.resources()).data
    resources.value = snapshot
    const cutoff = Date.now() - 30 * 60 * 1000
    resourceSamples.value = [...resourceSamples.value, snapshot]
      .filter((item) => new Date(item.captured_at).getTime() >= cutoff)
      .slice(-360)
  } catch (e) {
    if (!silent) ElMessage.error(errorMessage(e))
  } finally {
    resourceLoading.value = false
  }
}

async function loadRagGraph() {
  if (!ragTaskId.value) {
    ragSymbols.value = []
    ragEdges.value = []
    ragObservability.value = undefined
    selectedGraphNodeId.value = ''
    return
  }
  ragLoading.value = true
  try {
    const [symbols, graph, observability] = await Promise.all([
      codeIndexApi.symbols(ragTaskId.value),
      codeIndexApi.graph(ragTaskId.value),
      codeIndexApi.observability(ragTaskId.value),
    ])
    ragSymbols.value = symbols.data
    ragEdges.value = graph.data
    ragObservability.value = observability.data
    selectedGraphNodeId.value = graphNodes.value[0]?.id || ''
  } catch (e) {
    ragSymbols.value = []
    ragEdges.value = []
    ragObservability.value = undefined
    selectedGraphNodeId.value = ''
    ElMessage.error(errorMessage(e))
  } finally {
    ragLoading.value = false
  }
}

async function loadDeployments(silent = false) {
  try {
    modelDeployments.value = (await adminApi.modelDeployments()).data
  } catch (e) {
    if (!silent) ElMessage.error(errorMessage(e))
  }
}

function startResourceTimer() {
  if (resourceTimer) window.clearInterval(resourceTimer)
  if (autoRefresh.value) resourceTimer = window.setInterval(() => loadResources(true), 5000)
}

function startDeploymentTimer() {
  if (deploymentTimer) window.clearInterval(deploymentTimer)
  if (active.value === 'deployments') deploymentTimer = window.setInterval(() => loadDeployments(true), 5000)
}

async function createUser() {
  const message = validateNewUser(userForm)
  if (message) return ElMessage.warning(message)
  try {
    await adminApi.createUser(userForm)
    userDialog.value = false
    Object.assign(userForm, { username: '', password: '', role: 'user' })
    await load()
    ElMessage.success('用户已创建')
  } catch (e) { ElMessage.error(errorMessage(e)) }
}

async function toggleUser(row: AdminUser) {
  try { await adminApi.enableUser(row.id, !row.is_enabled); await load() } catch (e) { ElMessage.error(errorMessage(e)) }
}

async function resetPassword(row: AdminUser) {
  try {
    const { value } = await ElMessageBox.prompt('请输入至少 12 位的新密码', `重置 ${row.username} 的密码`, { inputType: 'password', inputValidator: (v) => v.length >= 12 || '密码至少 12 位' })
    await adminApi.resetPassword(row.id, value)
    ElMessage.success('密码已重置')
  } catch (e) { if (e !== 'cancel') ElMessage.error(errorMessage(e)) }
}

function openModel(row?: ModelNode) {
  editingModel.value = row?.id
  Object.assign(modelForm, row ? { ...row, api_key: row.api_key || '' } : { display_name: '', model_identifier: '', base_url: '', api_key: '', timeout_seconds: 120, is_enabled: true, description: '' })
  modelDialog.value = true
}

async function saveModel() {
  const message = validateModel(modelForm)
  if (message) return ElMessage.warning(message)
  try {
    await adminApi.saveModel({ ...modelForm, api_key: modelForm.api_key || undefined }, editingModel.value)
    modelDialog.value = false
    await load()
    ElMessage.success('模型配置已保存')
  } catch (e) { ElMessage.error(errorMessage(e)) }
}

async function toggleModel(row: ModelNode) {
  try { await adminApi.enableModel(row.id, !row.is_enabled); await load(); await loadResources(true) } catch (e) { ElMessage.error(errorMessage(e)) }
}

async function setDefaultModel(row: ModelNode) {
  try {
    await adminApi.defaultModel(row.id)
    await load()
    models.value = withSingleDefault(models.value, row.id)
    ElMessage.success('默认模型已更新')
  } catch (e) { ElMessage.error(errorMessage(e)) }
}

async function removeModel(id: string) {
  try { await ElMessageBox.confirm('确认删除此模型节点？', '删除模型', { type: 'warning' }); await adminApi.deleteModel(id); await load(); await loadResources(true) } catch (e) { if (e !== 'cancel') ElMessage.error(errorMessage(e)) }
}

async function removeTask(row: AdminTask) {
  const action = row.status === 'queued' || row.status === 'running' ? '停止并删除' : '删除'
  try {
    await ElMessageBox.confirm(`确认${action}任务“${row.display_name}”？`, action, { type: 'warning' })
    await adminApi.removeTask(row.id)
    await load()
    await loadResources(true)
    ElMessage.success('任务已删除')
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(errorMessage(e))
  }
}

async function pinTask(row: AdminTask) {
  try {
    await adminApi.pinTask(row.id)
    await load()
    await loadResources(true)
    ElMessage.success('任务已置顶')
  } catch (e) {
    ElMessage.error(errorMessage(e))
  }
}

async function health(id: string) {
  try { await adminApi.modelHealth(id); ElMessage.success('模型服务健康检查通过') } catch (e) { ElMessage.error(errorMessage(e)) }
}

function openDeployment(item?: ModelCatalogItem) {
  if (isDeploymentLocked.value) return ElMessage.warning('已有模型正在部署，请等待本次部署完成或失败后再创建新任务')
  const target = item || modelCatalog.value[0]
  if (!target) return ElMessage.warning('请先配置模型目录')
  Object.assign(deploymentForm, {
    catalog_key: target.key,
    source: target.recommended_source || 'huggingface',
    base_url: `http://127.0.0.1:${target.default_port || 8101}`,
    auto_base_url: true,
    served_model_name: target.default_served_model_name || target.key,
    api_key: '',
    port: target.default_port || 8101,
    timeout_seconds: 180,
    auto_register: true,
  })
  deploymentDialog.value = true
}

watch(() => deploymentForm.catalog_key, (key) => {
  const target = modelCatalog.value.find((item) => item.key === key)
  if (!target) return
  deploymentForm.source = target.recommended_source || deploymentForm.source
  deploymentForm.port = target.default_port || deploymentForm.port
  deploymentForm.served_model_name = target.default_served_model_name || target.key
  if (deploymentForm.auto_base_url) deploymentForm.base_url = generatedDeploymentBaseUrl.value
})

watch(() => deploymentForm.port, () => {
  if (deploymentForm.auto_base_url) deploymentForm.base_url = generatedDeploymentBaseUrl.value
})

watch(() => deploymentForm.auto_base_url, (enabled) => {
  if (enabled) deploymentForm.base_url = generatedDeploymentBaseUrl.value
})

async function createDeployment() {
  if (isDeploymentLocked.value) return ElMessage.warning('已有模型正在部署，请等待本次部署完成或失败后再创建新任务')
  if (!deploymentForm.catalog_key) return ElMessage.warning('请选择模型')
  if (!deploymentForm.auto_base_url && !deploymentForm.base_url.trim()) return ElMessage.warning('请输入 VLLM 服务地址')
  deploymentSubmitting.value = true
  try {
    const { auto_base_url, ...payload } = deploymentForm
    await adminApi.createModelDeployment({
      ...payload,
      base_url: auto_base_url ? undefined : payload.base_url,
      api_key: payload.api_key || undefined,
    })
    deploymentDialog.value = false
    await load()
    startDeploymentTimer()
    ElMessage.success('模型部署任务已创建')
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    deploymentSubmitting.value = false
  }
}

function openDeploymentLog(row: ModelDeployment) {
  selectedDeployment.value = row
  deploymentLogDialog.value = true
}

function openPrompt(row?: Prompt) {
  editingPrompt.value = row?.id
  promptBody.value = row?.body || ''
  promptDialog.value = true
}

async function savePrompt() {
  const message = validatePrompt(promptBody.value)
  if (message) return ElMessage.warning(message)
  try {
    if (editingPrompt.value) await adminApi.updatePrompt(editingPrompt.value, promptBody.value)
    else await adminApi.createPrompt(promptBody.value)
    promptDialog.value = false
    promptBody.value = ''
    editingPrompt.value = undefined
    await load()
    ElMessage.success('提示词版本已保存')
  } catch (e) { ElMessage.error(errorMessage(e)) }
}

async function activatePrompt(id: string) {
  try { await adminApi.activatePrompt(id); await load(); ElMessage.success('提示词版本已启用') } catch (e) { ElMessage.error(errorMessage(e)) }
}

async function removePrompt(row: Prompt) {
  try { await ElMessageBox.confirm('删除后无法恢复，确认删除此提示词版本？', '删除提示词', { type: 'warning' }); await adminApi.deletePrompt(row.id); await load(); ElMessage.success('提示词版本已删除') } catch (e) { if (e !== 'cancel') ElMessage.error(errorMessage(e)) }
}

watch(active, (value) => {
  if (value === 'deployments') {
    active.value = 'models'
    return
  }
  if (value === 'resources') loadResources(true)
  if (value === 'rag-graph') loadRagGraph()
  startDeploymentTimer()
})
watch(ragTaskId, () => {
  if (active.value === 'rag-graph') loadRagGraph()
})
watch(autoRefresh, startResourceTimer)
onMounted(async () => { await Promise.all([load(), loadResources(true)]); startResourceTimer(); startDeploymentTimer() })
onUnmounted(() => {
  if (resourceTimer) window.clearInterval(resourceTimer)
  if (deploymentTimer) window.clearInterval(deploymentTimer)
})
</script>

<template>
  <section>
    <header class="page-header">
      <div>
        <h1>后台管理</h1>
        <p>集中管理用户、模型节点、提示词版本、审查任务与服务器运行资源。</p>
      </div>
      <el-button class="refresh-action" :loading="loading || resourceLoading" :icon="Refresh" @click="active === 'resources' ? loadResources() : load()">刷新</el-button>
    </header>

    <div class="panel glass admin-panel">
      <el-tabs v-model="active">
        <el-tab-pane label="运行概览" name="dashboard">
          <div v-if="dashboard" class="admin-metrics">
            <div><span>全部用户</span><b>{{ dashboard.users }}</b><small>{{ dashboard.enabled_users }} 个启用</small></div>
            <div><span>模型节点</span><b>{{ dashboard.models }}</b><small>{{ dashboard.enabled_models }} 个在线配置</small></div>
            <div><span>审查任务</span><b>{{ dashboard.tasks }}</b><small>{{ dashboard.completed_tasks }} 个已完成</small></div>
            <div><span>异常任务</span><b>{{ dashboard.failed_tasks }}</b><small>{{ dashboard.running_tasks }} 个运行中</small></div>
          </div>
        </el-tab-pane>

        <el-tab-pane label="资源监控" name="resources">
          <div class="table-tools resource-tools">
            <span>服务器与模型推理实时状态，最后采样：{{ latestResourceTime }}</span>
            <div>
              <span>自动刷新</span>
              <el-switch v-model="autoRefresh" />
              <el-button :icon="Refresh" :loading="resourceLoading" @click="loadResources()">立即刷新</el-button>
            </div>
          </div>

          <div v-if="resources" class="resource-grid">
            <section class="resource-card">
              <div class="resource-card-head"><span>CPU</span><b>{{ metric(resources.system.cpu_percent) }}%</b></div>
              <el-progress :percentage="percent(resources.system.cpu_percent)" :status="progressStatus(resources.system.cpu_percent)" />
              <small>1 分钟负载：{{ metric(resources.system.load_average_1m, 2) }}</small>
              <small>{{ peakLabel(resourcePeaks.cpu_percent) }}</small>
            </section>
            <section class="resource-card">
              <div class="resource-card-head"><span>内存</span><b>{{ metric(resources.system.memory_percent) }}%</b></div>
              <el-progress :percentage="percent(resources.system.memory_percent)" :status="progressStatus(resources.system.memory_percent)" />
              <small>{{ bytes(resources.system.memory_used_bytes) }} / {{ bytes(resources.system.memory_total_bytes) }}</small>
              <small>{{ peakLabel(resourcePeaks.memory_percent) }}</small>
            </section>
            <section class="resource-card">
              <div class="resource-card-head"><span>磁盘</span><b>{{ metric(resources.system.disk_percent) }}%</b></div>
              <el-progress :percentage="percent(resources.system.disk_percent)" :status="progressStatus(resources.system.disk_percent)" />
              <small>{{ bytes(resources.system.disk_used_bytes) }} / {{ bytes(resources.system.disk_total_bytes) }}</small>
              <small>{{ peakLabel(resourcePeaks.disk_percent) }}</small>
            </section>
            <section class="resource-card">
              <div class="resource-card-head"><span>任务队列</span><b>{{ resources.tasks.running_tasks }}</b></div>
              <el-progress :percentage="taskRunningPercent" />
              <small>{{ resources.tasks.queued_tasks }} 排队 / {{ resources.tasks.failed_tasks }} 失败</small>
              <small>{{ peakLabel(resourcePeaks.running_tasks, ' 个', 0) }}</small>
            </section>
          </div>

          <div class="resource-section">
            <div class="section-heading">
              <div><h2>GPU 与显存</h2><p>当前 {{ resources?.gpus.length || 0 }} 张显卡，展示核心利用率与显存占用。</p></div>
            </div>
            <el-empty v-if="!resources?.gpus.length" description="当前未采集到 GPU 指标" />
            <div v-else class="gpu-grid">
              <article v-for="gpu in resources.gpus" :key="gpu.index" class="resource-card gpu-card">
                <div class="resource-card-head"><span>GPU {{ gpu.index }} · {{ gpu.name }}</span><b>{{ metric(gpu.utilization_percent) }}%</b></div>
                <el-progress :percentage="percent(gpu.utilization_percent)" :status="progressStatus(gpu.utilization_percent)" />
                <div class="gpu-details">
                  <span>显存 {{ mb(gpu.memory_used_mb) }} / {{ mb(gpu.memory_total_mb) }}</span>
                  <span>显存占用 {{ metric(gpu.memory_percent) }}%</span>
                </div>
              </article>
            </div>
          </div>

          <div class="resource-section">
            <div class="section-heading">
              <div><h2>模型吞吐量</h2><p>读取 VLLM Prometheus metrics，展示请求并发、等待队列和 token 吞吐。</p></div>
            </div>
            <el-table :data="resources?.models || []">
              <el-table-column prop="display_name" label="模型节点" min-width="170" />
              <el-table-column prop="base_url" label="地址" min-width="210" />
              <el-table-column label="GPU" width="150"><template #default="{ row }"><div class="metric-cell"><b>{{ gpuLabel(row.gpu_indices) }}</b><small>{{ tpLabel(row.tensor_parallel_size) }}</small></div></template></el-table-column>
              <el-table-column label="状态" width="110">
                <template #default="{ row }"><el-tag :type="row.metrics_available ? 'success' : 'warning'">{{ row.metrics_available ? '可采集' : '不可用' }}</el-tag></template>
              </el-table-column>
              <el-table-column label="Prompt tok/s" width="130"><template #default="{ row }"><div class="metric-cell"><b>{{ metric(row.prompt_throughput_tps) }}</b><small>{{ peakLabel(modelRuntimePeaks(row.node_id).prompt_throughput_tps, '', 1) }}</small></div></template></el-table-column>
              <el-table-column label="生成 tok/s" width="120"><template #default="{ row }"><div class="metric-cell"><b>{{ metric(row.generation_throughput_tps) }}</b><small>{{ peakLabel(modelRuntimePeaks(row.node_id).generation_throughput_tps, '', 1) }}</small></div></template></el-table-column>
              <el-table-column label="运行请求" width="100"><template #default="{ row }"><div class="metric-cell"><b>{{ row.running_requests ?? '--' }}</b><small>{{ peakLabel(modelRuntimePeaks(row.node_id).running_requests, ' 个', 0) }}</small></div></template></el-table-column>
              <el-table-column label="等待请求" width="100"><template #default="{ row }"><div class="metric-cell"><b>{{ row.pending_requests ?? '--' }}</b><small>{{ peakLabel(modelRuntimePeaks(row.node_id).pending_requests, ' 个', 0) }}</small></div></template></el-table-column>
              <el-table-column label="KV Cache" width="110"><template #default="{ row }"><div class="metric-cell"><b>{{ metric(row.gpu_kv_cache_usage_percent) }}%</b><small>{{ peakLabel(modelRuntimePeaks(row.node_id).gpu_kv_cache_usage_percent) }}</small></div></template></el-table-column>
              <el-table-column prop="error" label="采集错误" min-width="180" show-overflow-tooltip />
            </el-table>
          </div>
        </el-tab-pane>

        <el-tab-pane label="用户管理" name="users">
          <div class="table-tools"><span>账号与权限隔离</span><el-button type="primary" :icon="CirclePlus" @click="userDialog = true">新增用户</el-button></div>
          <el-table :data="users">
            <el-table-column prop="username" label="用户名" />
            <el-table-column prop="role" label="角色" width="110" />
            <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="row.is_enabled ? 'success' : 'info'">{{ row.is_enabled ? '启用' : '禁用' }}</el-tag></template></el-table-column>
            <el-table-column label="创建时间" width="180"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column>
            <el-table-column label="操作" width="220"><template #default="{ row }"><el-button link type="primary" @click="toggleUser(row)">{{ row.is_enabled ? '禁用' : '启用' }}</el-button><el-button link type="primary" @click="resetPassword(row)">重置密码</el-button></template></el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="模型节点" name="models">
          <div class="table-tools"><span>分布式 VLLM 推理服务配置</span><el-button type="primary" :icon="CirclePlus" @click="openModel()">新增模型</el-button></div>
          <el-table :key="modelTableKey" :data="models">
            <el-table-column prop="display_name" label="模型名称" />
            <el-table-column prop="model_identifier" label="模型标识" min-width="180" />
            <el-table-column prop="base_url" label="服务地址" min-width="200" />
            <el-table-column label="GPU" width="150"><template #default="{ row }"><div class="metric-cell"><b>{{ gpuLabel(row.gpu_indices) }}</b><small>{{ tpLabel(row.tensor_parallel_size) }}</small></div></template></el-table-column>
            <el-table-column label="状态" width="150"><template #default="{ row }"><el-tag :type="row.is_enabled ? 'success' : 'info'">{{ row.is_enabled ? '启用' : '禁用' }}</el-tag><el-tag v-if="row.is_default" class="model-default-tag">默认</el-tag></template></el-table-column>
            <el-table-column label="操作" width="355"><template #default="{ row }"><el-button link :icon="Connection" @click="health(row.id)">检测</el-button><el-button link type="primary" :disabled="row.is_default || !row.is_enabled" @click="setDefaultModel(row)">设为默认</el-button><el-button link type="primary" @click="toggleModel(row)">{{ row.is_enabled ? '禁用' : '启用' }}</el-button><el-button link type="primary" @click="openModel(row)">编辑</el-button><el-button link type="danger" @click="removeModel(row.id)">删除</el-button></template></el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane v-if="false" label="模型部署" name="deployments">
          <div class="table-tools">
            <span>从可配置目录发起模型下载与 VLLM 部署，成功后自动登记为可切换模型节点</span>
          </div>
          <div v-if="deployedModelNodes.length" class="deployed-model-section">
            <div class="section-heading">
              <div><h2>已部署模型节点</h2><p>这里展示当前已登记并可用于代码审查的真实模型服务。</p></div>
            </div>
            <div class="deployment-grid">
              <section
                v-for="model in deployedModelNodes"
                :key="model.id"
                class="deployment-card is-deployed-model"
                :class="{ 'is-default-model': model.is_default }"
              >
                <div class="deployment-card-head">
                  <div><h3>{{ model.display_name }}</h3><p>{{ model.model_identifier }}</p></div>
                  <div class="deployment-card-tags">
                    <el-tag v-if="model.is_default" type="success">默认</el-tag>
                    <el-tag type="success">已部署</el-tag>
                  </div>
                </div>
                <p>{{ model.description || '已登记为可切换模型节点，可直接用于代码审查。' }}</p>
                <div class="deployed-model-meta">
                  <span>服务地址</span>
                  <code>{{ model.base_url }}</code>
                </div>
                <div class="deployment-tags">
                  <el-tag effect="plain">{{ model.base_url.startsWith('mock://') ? 'mock' : 'vllm' }}</el-tag>
                  <el-tag effect="plain">{{ model.timeout_seconds }}s</el-tag>
                </div>
                <el-button :icon="Connection" @click="health(model.id)">健康检查</el-button>
              </section>
            </div>
          </div>
          <div class="deployment-grid">
            <section v-for="item in modelCatalog" :key="item.key" class="deployment-card" :class="{ 'is-default-model': isDefaultCatalogModel(item), 'is-deployed-model': isDeployedCatalogModel(item) }">
              <div class="deployment-card-head">
                <div><h3>{{ item.display_name }}</h3><p>{{ item.model_identifier }}</p></div>
                <div class="deployment-card-tags">
                  <el-tag v-if="isDefaultCatalogModel(item)" type="success">默认</el-tag>
                  <el-tag v-else-if="isDeployedCatalogModel(item)" type="success">已部署</el-tag>
                  <el-tag>{{ item.estimated_vram_gb || '--' }} GB</el-tag>
                </div>
              </div>
              <p>{{ item.description }}</p>
              <div class="deployment-tags">
                <el-tag v-for="tag in item.tags" :key="tag" effect="plain">{{ tag }}</el-tag>
              </div>
              <el-button v-if="isDeployedCatalogModel(item)" class="deployment-action-button" disabled>已部署</el-button>
              <el-tooltip
                v-else
                :disabled="!isDeploymentLocked"
                content="已有模型正在部署，请等待本次部署完成或失败后再操作"
                placement="top"
              >
                <span class="deployment-action-wrap">
                  <el-button :icon="Download" :disabled="isDeploymentLocked" @click="openDeployment(item)">下载部署</el-button>
                </span>
              </el-tooltip>
            </section>
          </div>
          <div class="resource-section">
            <div class="section-heading">
              <div><h2>部署记录</h2><p>部署页会自动刷新；失败时直接显示原因，完整输出可打开日志查看。</p></div>
              <el-tag v-if="activeDeployments.length" type="warning">{{ activeDeployments.length }} 个任务进行中</el-tag>
            </div>
            <el-alert
              v-if="failedDeployments.length"
              class="deployment-alert"
              type="error"
              show-icon
              :closable="false"
              :title="`${failedDeployments[0].display_name} 部署失败`"
              :description="deploymentFailureText(failedDeployments[0])"
            />
            <el-table :data="modelDeployments" :row-class-name="deploymentRowClassName">
              <el-table-column label="模型" min-width="180">
                <template #default="{ row }">
                  <span>{{ row.display_name }}</span>
                  <el-tag v-if="isDefaultDeployment(row)" class="model-default-tag" type="success">默认</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="source" label="来源" width="110" />
              <el-table-column prop="base_url" label="服务地址" min-width="180" />
              <el-table-column label="状态" width="130">
                <template #default="{ row }"><el-tag :type="deploymentStatusType(row.status)">{{ deploymentStatusLabel(row.status) }}</el-tag></template>
              </el-table-column>
              <el-table-column label="进度" width="150"><template #default="{ row }"><el-progress :percentage="percent(row.progress)" :status="deploymentProgressStatus(row)" /></template></el-table-column>
              <el-table-column label="失败原因 / 最新日志" min-width="300" show-overflow-tooltip>
                <template #default="{ row }">
                  <span :class="{ 'deployment-log-error': row.status === 'failed' }">{{ deploymentLogPreview(row) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="日志" width="90">
                <template #default="{ row }"><el-button link type="primary" @click="openDeploymentLog(row)">查看</el-button></template>
              </el-table-column>
              <el-table-column label="创建时间" width="180"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column>
            </el-table>
          </div>
        </el-tab-pane>

        <el-tab-pane label="提示词" name="prompts">
          <div class="table-tools"><span>C 语言专属审查规则版本</span><el-button type="primary" :icon="CirclePlus" @click="openPrompt()">新增版本</el-button></div>
          <el-table :data="prompts">
            <el-table-column prop="version" label="版本" width="90" />
            <el-table-column prop="body" label="提示词内容" show-overflow-tooltip />
            <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="row.is_active ? 'success' : 'info'">{{ row.is_active ? '当前启用' : '历史版本' }}</el-tag></template></el-table-column>
            <el-table-column label="创建时间" width="180"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column>
            <el-table-column label="操作" width="210"><template #default="{ row }"><el-button link type="primary" :disabled="row.is_active" @click="activatePrompt(row.id)">启用</el-button><el-button link type="primary" @click="openPrompt(row)">修改</el-button><el-button link type="danger" :disabled="row.is_active || prompts.length <= 1" @click="removePrompt(row)">删除</el-button></template></el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="RAG 图谱" name="rag-graph">
          <div class="table-tools rag-graph-tools">
            <div>
              <span>选择已创建索引的审查任务</span>
              <el-select v-model="ragTaskId" filterable placeholder="请选择任务">
                <el-option v-for="task in indexedTasks" :key="task.id" :label="`${task.display_name} · ${task.status}`" :value="task.id" />
              </el-select>
            </div>
            <el-button :icon="Refresh" :loading="ragLoading" :disabled="!ragTaskId" @click="loadRagGraph">刷新图谱</el-button>
          </div>

          <el-empty v-if="!ragTaskId" description="暂无可查看的审查任务" />
          <el-alert v-else-if="ragObservability && !ragObservability.enabled" title="RAG 可观测性未启用" type="warning" :closable="false" />
          <div v-else v-loading="ragLoading" class="rag-graph-view">
            <section class="rag-summary-grid">
              <article><span>文件</span><b>{{ ragStats.files }}</b></article>
              <article><span>符号</span><b>{{ ragStats.symbols }}</b></article>
              <article><span>关系边</span><b>{{ ragStats.edges }}</b></article>
              <article><span>切片</span><b>{{ ragStats.chunks }}</b></article>
              <article><span>向量</span><b>{{ ragStats.embeddings }}</b></article>
              <article><span>Qdrant 点</span><b>{{ ragStats.qdrant_points }}</b></article>
            </section>

            <section class="rag-graph-layout">
              <article class="rag-canvas-card">
                <div class="section-heading">
                  <div><h2>索引图结构</h2><p>{{ selectedRagTask?.display_name || '未选择任务' }}，展示最多 80 个高价值节点和 160 条关系边。</p></div>
                </div>
                <svg class="rag-graph-canvas" viewBox="0 0 920 390" role="img" aria-label="RAG 索引图结构">
                  <defs>
                    <marker id="rag-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
                      <path d="M 0 0 L 10 5 L 0 10 z" fill="#8d9fb1" />
                    </marker>
                  </defs>
                  <g>
                    <line
                      v-for="link in graphLinks"
                      :key="link.id"
                      :x1="graphNodePosition.get(link.source)?.x"
                      :y1="graphNodePosition.get(link.source)?.y"
                      :x2="graphNodePosition.get(link.target)?.x"
                      :y2="graphNodePosition.get(link.target)?.y"
                      :stroke="edgeColor(link.edge_type)"
                      stroke-width="1.4"
                      stroke-opacity="0.58"
                      marker-end="url(#rag-arrow)"
                    />
                  </g>
                  <g>
                    <g
                      v-for="node in graphNodes"
                      :key="node.id"
                      class="rag-graph-node"
                      :class="[nodeClass(node.kind), { 'is-selected': selectedGraphNodeId === node.id }]"
                      :transform="`translate(${node.x}, ${node.y})`"
                      @click="selectedGraphNodeId = node.id"
                    >
                      <circle :r="node.kind === 'file' ? 15 : 10" />
                      <text :x="node.kind === 'file' ? 20 : 14" y="4">{{ node.label }}</text>
                    </g>
                  </g>
                </svg>
              </article>

              <aside class="rag-side-panel">
                <article class="rag-detail-card">
                  <h3>节点详情</h3>
                  <template v-if="selectedGraphNode">
                    <b>{{ selectedGraphNode.label }}</b>
                    <small>{{ selectedGraphNode.kind }}</small>
                    <p>{{ selectedGraphNode.file_path || '--' }}<span v-if="selectedGraphNode.line">:{{ selectedGraphNode.line }}</span></p>
                    <p v-if="selectedGraphNode.source_tool">来源：{{ selectedGraphNode.source_tool }}</p>
                  </template>
                  <el-empty v-else description="点击图中节点查看详情" />
                </article>
                <article class="rag-detail-card">
                  <h3>符号来源</h3>
                  <div v-for="[name, count] in graphToolCountEntries" :key="name" class="rag-stat-row"><span>{{ name }}</span><b>{{ count }}</b></div>
                </article>
                <article class="rag-detail-card">
                  <h3>符号类型</h3>
                  <div v-for="[name, count] in graphSymbolCountEntries.slice(0, 8)" :key="name" class="rag-stat-row"><span>{{ name }}</span><b>{{ count }}</b></div>
                </article>
              </aside>
            </section>

            <section class="rag-lower-grid">
              <article class="rag-detail-card">
                <h3>关系类型</h3>
                <div v-for="[name, count] in graphEdgeCountEntries.slice(0, 10)" :key="name" class="rag-stat-row"><span>{{ name }}</span><b>{{ count }}</b></div>
              </article>
              <article class="rag-detail-card">
                <h3>检索候选</h3>
                <div class="rag-stat-row"><span>原始候选</span><b>{{ ragObservability?.raw_candidate_count || 0 }}</b></div>
                <div class="rag-stat-row"><span>选中证据</span><b>{{ ragObservability?.selected_count || 0 }}</b></div>
                <div class="rag-stat-row"><span>剪掉证据</span><b>{{ ragObservability?.rejected_count || 0 }}</b></div>
                <div v-for="[name, count] in graphBucketEntries.slice(0, 6)" :key="name" class="rag-stat-row"><span>{{ name }}</span><b>{{ count }}</b></div>
              </article>
            </section>
          </div>
        </el-tab-pane>

        <el-tab-pane label="任务监控" name="tasks">
          <div class="table-tools"><span>全局审查任务状态</span><el-select v-model="taskStatus" clearable placeholder="全部状态" @change="load"><el-option label="排队中" value="queued" /><el-option label="审查中" value="running" /><el-option label="已完成" value="completed" /><el-option label="失败" value="failed" /></el-select></div>
          <el-table :data="tasks">
            <el-table-column prop="display_name" label="任务" />
            <el-table-column prop="owner_id" label="用户 ID" min-width="170" />
            <el-table-column label="状态" width="110"><template #default="{ row }"><StatusBadge :status="row.status" /></template></el-table-column>
            <el-table-column prop="progress" label="进度" width="80" />
            <el-table-column label="排队" width="130">
              <template #default="{ row }">
                <span v-if="row.status === 'queued'">
                  前方 {{ row.queued_ahead_count ?? 0 }} 个
                  <small v-if="row.queue_priority">置顶</small>
                </span>
                <span v-else>--</span>
              </template>
            </el-table-column>
            <el-table-column prop="finding_count" label="问题" width="80" />
            <el-table-column label="创建时间" width="180"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column>
            <el-table-column label="操作" width="190">
              <template #default="{ row }">
                <el-button link type="primary" :disabled="row.status !== 'queued'" @click="pinTask(row)">置顶</el-button>
                <el-button link type="danger" @click="removeTask(row)">
                  {{ row.status === 'queued' || row.status === 'running' ? '停止/删除' : '删除' }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </div>

    <el-dialog v-model="userDialog" title="新增用户" width="460">
      <el-form label-position="top"><el-form-item label="用户名"><el-input v-model="userForm.username" /></el-form-item><el-form-item label="初始密码"><el-input v-model="userForm.password" type="password" show-password placeholder="至少 12 位" /></el-form-item><el-form-item label="角色"><el-select v-model="userForm.role"><el-option label="普通用户" value="user" /><el-option label="管理员" value="admin" /></el-select></el-form-item></el-form>
      <template #footer><el-button @click="userDialog = false">取消</el-button><el-button type="primary" @click="createUser">创建</el-button></template>
    </el-dialog>
    <el-dialog v-model="modelDialog" :title="editingModel ? '编辑模型' : '新增模型'" width="580">
      <el-form label-position="top"><el-form-item label="展示名称"><el-input v-model="modelForm.display_name" /></el-form-item><el-form-item label="模型标识"><el-input v-model="modelForm.model_identifier" /></el-form-item><el-form-item label="VLLM 服务地址"><el-input v-model="modelForm.base_url" /></el-form-item><el-form-item label="API Key（可选）"><el-input v-model="modelForm.api_key" type="password" show-password /></el-form-item><el-form-item label="超时时间（秒）"><el-input-number v-model="modelForm.timeout_seconds" :min="1" :max="3600" /></el-form-item><el-form-item label="说明"><el-input v-model="modelForm.description" type="textarea" /></el-form-item></el-form>
      <template #footer><el-button @click="modelDialog = false">取消</el-button><el-button type="primary" @click="saveModel">保存</el-button></template>
    </el-dialog>
    <el-dialog v-model="deploymentDialog" title="部署模型" width="640">
      <el-form label-position="top">
        <el-form-item label="模型">
          <el-select v-model="deploymentForm.catalog_key" filterable>
            <el-option v-for="item in modelCatalog" :key="item.key" :label="item.display_name" :value="item.key" />
          </el-select>
        </el-form-item>
        <el-form-item label="下载来源">
          <div class="source-picker">
            <el-segmented v-model="deploymentForm.source" :options="deploymentSourceOptions" />
            <div class="source-links">
              <el-link href="https://huggingface.co/models" target="_blank" rel="noopener noreferrer" type="primary">HuggingFace 模型库</el-link>
              <el-link href="https://modelscope.cn/models" target="_blank" rel="noopener noreferrer" type="primary">ModelScope 模型库</el-link>
            </div>
          </div>
        </el-form-item>
        <el-form-item label="自动生成地址"><el-switch v-model="deploymentForm.auto_base_url" /></el-form-item>
        <el-form-item label="模型仓库"><el-input :model-value="deploymentForm.source === 'modelscope' ? selectedCatalog?.modelscope_repo : selectedCatalog?.huggingface_repo" disabled /></el-form-item>
        <el-form-item label="端口"><el-input-number v-model="deploymentForm.port" :min="1" :max="65535" /></el-form-item>
        <el-form-item label="VLLM 服务地址"><el-input v-model="deploymentForm.base_url" :disabled="deploymentForm.auto_base_url" /></el-form-item>
        <el-form-item label="模型服务名称"><el-input v-model="deploymentForm.served_model_name" /></el-form-item>
        <el-form-item label="API Key（可选）"><el-input v-model="deploymentForm.api_key" type="password" show-password /></el-form-item>
        <el-form-item label="自动登记为模型节点"><el-switch v-model="deploymentForm.auto_register" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="deploymentDialog = false">取消</el-button><el-button type="primary" :loading="deploymentSubmitting" :disabled="isDeploymentLocked" @click="createDeployment">创建部署任务</el-button></template>
    </el-dialog>
    <el-dialog v-model="deploymentLogDialog" :title="`部署日志 · ${selectedDeployment?.display_name || ''}`" width="860">
      <el-alert
        v-if="selectedDeployment && deploymentFailureText(selectedDeployment)"
        class="deployment-alert"
        :type="selectedDeployment.status === 'failed' ? 'error' : 'warning'"
        show-icon
        :closable="false"
        :title="selectedDeployment.status === 'failed' ? '部署失败' : '部署提示'"
        :description="deploymentFailureText(selectedDeployment)"
      />
      <div class="markdown-preview"><pre>{{ selectedDeployment?.log || '暂无日志' }}</pre></div>
      <template #footer><el-button @click="deploymentLogDialog = false">关闭</el-button></template>
    </el-dialog>
    <el-dialog v-model="promptDialog" :title="editingPrompt ? '修改提示词版本' : '新增提示词版本'" width="680">
      <el-input v-model="promptBody" type="textarea" :rows="14" placeholder="请输入完整的 C 语言审查提示词..." />
      <template #footer><el-button @click="promptDialog = false">取消</el-button><el-button type="primary" @click="savePrompt">{{ editingPrompt ? '保存修改' : '创建版本' }}</el-button></template>
    </el-dialog>
  </section>
</template>
