<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CirclePlus, Connection, Download, Refresh } from '@element-plus/icons-vue'
import { adminApi, errorMessage } from '../api/client'
import StatusBadge from '../components/StatusBadge.vue'
import type { AdminTask, AdminUser, Dashboard, ModelCatalogItem, ModelDeployment, ModelNode, Prompt, ResourceSnapshot, TaskStatus } from '../types'
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
const taskStatus = ref<TaskStatus | ''>('')
const loading = ref(false)
const resourceLoading = ref(false)
const autoRefresh = ref(true)
const modelTableKey = ref(0)
const userDialog = ref(false)
const modelDialog = ref(false)
const deploymentDialog = ref(false)
const promptDialog = ref(false)
const editingModel = ref<string>()
const editingPrompt = ref<string>()
const userForm = reactive({ username: '', password: '', role: 'user' })
const modelForm = reactive({ display_name: '', model_identifier: '', base_url: '', api_key: '', timeout_seconds: 120, is_enabled: true, description: '' })
const deploymentForm = reactive({ catalog_key: '', source: 'modelscope', base_url: '', served_model_name: '', api_key: '', port: 8101, timeout_seconds: 180, auto_register: true })
const promptBody = ref('')
let resourceTimer: number | undefined

const date = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false })
const percent = (value?: number | null) => Math.max(0, Math.min(100, Number(value ?? 0)))
const metric = (value?: number | null, digits = 1) => value == null ? '--' : value.toFixed(digits)
const bytes = (value?: number | null) => {
  if (value == null) return '--'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = value
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1 }
  return `${size.toFixed(unit ? 1 : 0)} ${units[unit]}`
}
const mb = (value?: number | null) => value == null ? '--' : `${value.toFixed(0)} MB`
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
const selectedCatalog = computed(() => modelCatalog.value.find((item) => item.key === deploymentForm.catalog_key))

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
  } catch (e) {
    ElMessage.error(errorMessage(e))
  } finally {
    loading.value = false
  }
}

async function loadResources(silent = false) {
  if (!silent) resourceLoading.value = true
  try {
    resources.value = (await adminApi.resources()).data
  } catch (e) {
    if (!silent) ElMessage.error(errorMessage(e))
  } finally {
    resourceLoading.value = false
  }
}

function startResourceTimer() {
  if (resourceTimer) window.clearInterval(resourceTimer)
  if (autoRefresh.value) resourceTimer = window.setInterval(() => loadResources(true), 5000)
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

async function health(id: string) {
  try { await adminApi.modelHealth(id); ElMessage.success('模型服务健康检查通过') } catch (e) { ElMessage.error(errorMessage(e)) }
}

function openDeployment(item?: ModelCatalogItem) {
  const target = item || modelCatalog.value[0]
  if (!target) return ElMessage.warning('请先配置模型目录')
  Object.assign(deploymentForm, {
    catalog_key: target.key,
    source: target.recommended_source || 'huggingface',
    base_url: `http://127.0.0.1:${target.default_port || 8101}`,
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
  deploymentForm.base_url = `http://127.0.0.1:${target.default_port || deploymentForm.port}`
})

async function createDeployment() {
  if (!deploymentForm.catalog_key) return ElMessage.warning('请选择模型')
  if (!deploymentForm.base_url.trim()) return ElMessage.warning('请输入 VLLM 服务地址')
  try {
    await adminApi.createModelDeployment({ ...deploymentForm, api_key: deploymentForm.api_key || undefined })
    deploymentDialog.value = false
    await load()
    ElMessage.success('模型部署任务已创建')
  } catch (e) { ElMessage.error(errorMessage(e)) }
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

watch(active, (value) => { if (value === 'resources') loadResources(true) })
watch(autoRefresh, startResourceTimer)
onMounted(async () => { await Promise.all([load(), loadResources(true)]); startResourceTimer() })
onUnmounted(() => { if (resourceTimer) window.clearInterval(resourceTimer) })
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
              <el-switch v-model="autoRefresh" active-text="自动刷新" />
              <el-button :icon="Refresh" :loading="resourceLoading" @click="loadResources()">立即刷新</el-button>
            </div>
          </div>

          <div v-if="resources" class="resource-grid">
            <section class="resource-card">
              <div class="resource-card-head"><span>CPU</span><b>{{ metric(resources.system.cpu_percent) }}%</b></div>
              <el-progress :percentage="percent(resources.system.cpu_percent)" :status="progressStatus(resources.system.cpu_percent)" />
              <small>1 分钟负载：{{ metric(resources.system.load_average_1m, 2) }}</small>
            </section>
            <section class="resource-card">
              <div class="resource-card-head"><span>内存</span><b>{{ metric(resources.system.memory_percent) }}%</b></div>
              <el-progress :percentage="percent(resources.system.memory_percent)" :status="progressStatus(resources.system.memory_percent)" />
              <small>{{ bytes(resources.system.memory_used_bytes) }} / {{ bytes(resources.system.memory_total_bytes) }}</small>
            </section>
            <section class="resource-card">
              <div class="resource-card-head"><span>磁盘</span><b>{{ metric(resources.system.disk_percent) }}%</b></div>
              <el-progress :percentage="percent(resources.system.disk_percent)" :status="progressStatus(resources.system.disk_percent)" />
              <small>{{ bytes(resources.system.disk_used_bytes) }} / {{ bytes(resources.system.disk_total_bytes) }}</small>
            </section>
            <section class="resource-card">
              <div class="resource-card-head"><span>任务队列</span><b>{{ resources.tasks.running_tasks }}</b></div>
              <el-progress :percentage="taskRunningPercent" />
              <small>{{ resources.tasks.queued_tasks }} 排队 / {{ resources.tasks.failed_tasks }} 失败</small>
            </section>
          </div>

          <div class="resource-section">
            <div class="section-heading">
              <div><h2>GPU 与显存</h2><p>通过 nvidia-smi 采集，未安装或无 GPU 时会显示为空。</p></div>
            </div>
            <el-empty v-if="!resources?.gpus.length" description="当前未采集到 GPU 指标" />
            <div v-else class="gpu-grid">
              <article v-for="gpu in resources.gpus" :key="gpu.index" class="resource-card gpu-card">
                <div class="resource-card-head"><span>GPU {{ gpu.index }} · {{ gpu.name }}</span><b>{{ metric(gpu.utilization_percent) }}%</b></div>
                <el-progress :percentage="percent(gpu.utilization_percent)" :status="progressStatus(gpu.utilization_percent)" />
                <div class="gpu-details">
                  <span>显存 {{ mb(gpu.memory_used_mb) }} / {{ mb(gpu.memory_total_mb) }}</span>
                  <span>显存占用 {{ metric(gpu.memory_percent) }}%</span>
                  <span>温度 {{ metric(gpu.temperature_c, 0) }}°C</span>
                  <span>功耗 {{ metric(gpu.power_w, 0) }} W</span>
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
              <el-table-column label="状态" width="110">
                <template #default="{ row }"><el-tag :type="row.metrics_available ? 'success' : 'warning'">{{ row.metrics_available ? '可采集' : '不可用' }}</el-tag></template>
              </el-table-column>
              <el-table-column label="Prompt tok/s" width="130"><template #default="{ row }">{{ metric(row.prompt_throughput_tps) }}</template></el-table-column>
              <el-table-column label="生成 tok/s" width="120"><template #default="{ row }">{{ metric(row.generation_throughput_tps) }}</template></el-table-column>
              <el-table-column label="运行请求" width="100"><template #default="{ row }">{{ row.running_requests ?? '--' }}</template></el-table-column>
              <el-table-column label="等待请求" width="100"><template #default="{ row }">{{ row.pending_requests ?? '--' }}</template></el-table-column>
              <el-table-column label="KV Cache" width="110"><template #default="{ row }">{{ metric(row.gpu_kv_cache_usage_percent) }}%</template></el-table-column>
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
            <el-table-column label="状态" width="150"><template #default="{ row }"><el-tag :type="row.is_enabled ? 'success' : 'info'">{{ row.is_enabled ? '启用' : '禁用' }}</el-tag><el-tag v-if="row.is_default" class="model-default-tag">默认</el-tag></template></el-table-column>
            <el-table-column label="操作" width="355"><template #default="{ row }"><el-button link :icon="Connection" @click="health(row.id)">检测</el-button><el-button link type="primary" :disabled="row.is_default || !row.is_enabled" @click="setDefaultModel(row)">设为默认</el-button><el-button link type="primary" @click="toggleModel(row)">{{ row.is_enabled ? '禁用' : '启用' }}</el-button><el-button link type="primary" @click="openModel(row)">编辑</el-button><el-button link type="danger" @click="removeModel(row.id)">删除</el-button></template></el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="模型部署" name="deployments">
          <div class="table-tools">
            <span>从可配置目录发起模型下载与 VLLM 部署，成功后自动登记为可切换模型节点</span>
            <el-button type="primary" :icon="Download" @click="openDeployment()">部署模型</el-button>
          </div>
          <div class="deployment-grid">
            <section v-for="item in modelCatalog" :key="item.key" class="deployment-card">
              <div class="deployment-card-head">
                <div><h3>{{ item.display_name }}</h3><p>{{ item.model_identifier }}</p></div>
                <el-tag>{{ item.estimated_vram_gb || '--' }} GB</el-tag>
              </div>
              <p>{{ item.description }}</p>
              <div class="deployment-tags">
                <el-tag v-for="tag in item.tags" :key="tag" effect="plain">{{ tag }}</el-tag>
              </div>
              <el-button :icon="Download" @click="openDeployment(item)">下载部署</el-button>
            </section>
          </div>
          <div class="resource-section">
            <div class="section-heading">
              <div><h2>部署记录</h2><p>自动部署关闭时会返回手动执行指令；Linux GPU 服务器开启后会执行脚本并回写状态。</p></div>
            </div>
            <el-table :data="modelDeployments">
              <el-table-column prop="display_name" label="模型" min-width="180" />
              <el-table-column prop="source" label="来源" width="110" />
              <el-table-column prop="base_url" label="服务地址" min-width="180" />
              <el-table-column label="状态" width="130">
                <template #default="{ row }"><el-tag :type="row.status === 'failed' ? 'danger' : row.status === 'succeeded' ? 'success' : 'warning'">{{ row.status }}</el-tag></template>
              </el-table-column>
              <el-table-column label="进度" width="150"><template #default="{ row }"><el-progress :percentage="percent(row.progress)" /></template></el-table-column>
              <el-table-column prop="log" label="日志" min-width="260" show-overflow-tooltip />
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

        <el-tab-pane label="任务监控" name="tasks">
          <div class="table-tools"><span>全局审查任务状态</span><el-select v-model="taskStatus" clearable placeholder="全部状态" @change="load"><el-option label="排队中" value="queued" /><el-option label="审查中" value="running" /><el-option label="已完成" value="completed" /><el-option label="失败" value="failed" /></el-select></div>
          <el-table :data="tasks">
            <el-table-column prop="display_name" label="任务" />
            <el-table-column prop="owner_id" label="用户 ID" min-width="170" />
            <el-table-column label="状态" width="110"><template #default="{ row }"><StatusBadge :status="row.status" /></template></el-table-column>
            <el-table-column prop="progress" label="进度" width="80" />
            <el-table-column prop="finding_count" label="问题" width="80" />
            <el-table-column label="创建时间" width="180"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column>
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
          <el-segmented v-model="deploymentForm.source" :options="['huggingface', 'modelscope']" />
        </el-form-item>
        <el-form-item label="模型仓库"><el-input :model-value="deploymentForm.source === 'modelscope' ? selectedCatalog?.modelscope_repo : selectedCatalog?.huggingface_repo" disabled /></el-form-item>
        <el-form-item label="VLLM 服务地址"><el-input v-model="deploymentForm.base_url" /></el-form-item>
        <el-form-item label="端口"><el-input-number v-model="deploymentForm.port" :min="1" :max="65535" /></el-form-item>
        <el-form-item label="Served Model Name"><el-input v-model="deploymentForm.served_model_name" /></el-form-item>
        <el-form-item label="API Key（可选）"><el-input v-model="deploymentForm.api_key" type="password" show-password /></el-form-item>
        <el-form-item label="自动登记为模型节点"><el-switch v-model="deploymentForm.auto_register" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="deploymentDialog = false">取消</el-button><el-button type="primary" @click="createDeployment">创建部署任务</el-button></template>
    </el-dialog>
    <el-dialog v-model="promptDialog" :title="editingPrompt ? '修改提示词版本' : '新增提示词版本'" width="680">
      <el-input v-model="promptBody" type="textarea" :rows="14" placeholder="请输入完整的 C 语言审查提示词..." />
      <template #footer><el-button @click="promptDialog = false">取消</el-button><el-button type="primary" @click="savePrompt">{{ editingPrompt ? '保存修改' : '创建版本' }}</el-button></template>
    </el-dialog>
  </section>
</template>
