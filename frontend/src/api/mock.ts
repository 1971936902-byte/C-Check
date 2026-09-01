import type { AdminTask, AdminUser, Dashboard, Finding, ModelCatalogItem, ModelDeployment, ModelNode, Prompt, Report, ResourceSnapshot, ReviewTask, Severity, TaskStatus, User } from '../types'

const STATE_KEY = 'c-check-mock-state'
const SESSION_KEY = 'c-check-mock-session'
const STATE_VERSION = 6

type MockState = {
  version: number
  users: AdminUser[]
  passwords: Record<string, string>
  models: ModelNode[]
  deployments: ModelDeployment[]
  prompts: Prompt[]
  tasks: ReviewTask[]
  reports: Report[]
  polls: Record<string, number>
}

type MockResponse<T> = Promise<{ data: T }>

const now = () => new Date().toISOString()
const id = (prefix: string) => `${prefix}-${Math.random().toString(36).slice(2, 10)}`
const response = async <T>(data: T): MockResponse<T> => ({ data })
const minutesAgo = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString()

const checkTypeLabels: Record<string, string> = {
  memory_safety: '释放后使用/内存破坏',
  buffer_overflow: '缓冲区/数组越界',
  pointer_safety: '野指针/悬空指针',
  resource_leak: '资源泄漏',
  integer_safety: '长度/索引整数风险',
  logic: '严重状态机/协议逻辑',
}

const allCheckTypes = Object.keys(checkTypeLabels)

const sourceFiles = [
  'src/main.c',
  'src/parser.c',
  'src/config.c',
  'src/network.c',
  'src/scheduler.c',
  'src/adc_driver.c',
  'src/dma_ring.c',
  'src/usb_sample.c',
  'src/can_bus.c',
  'src/storage.c',
  'include/config.h',
  'include/protocol.h',
  'include/device_state.h',
  'drivers/stm32f10x_adc.c',
  'drivers/stm32f10x_dma.c',
  'drivers/stm32f10x_usart.c',
]

const demoFiles = (count = 8, prefix = '') => sourceFiles
  .slice(0, Math.min(count, sourceFiles.length))
  .map((relative_path, index) => ({
    id: `file-${prefix || 'demo'}-${index + 1}`,
    relative_path,
    size_bytes: 420 + index * 137,
  }))

const findingTemplates: Finding[] = [
  {
    severity: 'high',
    category: 'buffer_overflow',
    title: 'strcpy越界写',
    description: 'strcpy 直接复制外部输入到固定长度缓冲区，输入过长时会越界写入栈内存。',
    file_path: 'src/parser.c',
    line: 42,
  },
  {
    severity: 'high',
    category: 'memory_safety',
    title: '释放后继续写入',
    description: 'free 释放缓冲区后继续通过同一指针写入，存在 use-after-free 风险。',
    file_path: 'src/network.c',
    line: 118,
  },
  {
    severity: 'medium',
    category: 'resource_leak',
    title: '异常返回泄漏句柄',
    description: '读取配置失败后提前返回，已经打开的 FILE 句柄没有在该路径上关闭。',
    file_path: 'src/config.c',
    line: 87,
  },
  {
    severity: 'medium',
    category: 'integer_safety',
    title: '长度计算截断',
    description: '将 size_t 长度强制转换为 int，超大输入会截断并绕过后续边界判断。',
    file_path: 'src/usb_sample.c',
    line: 206,
  },
  {
    severity: 'low',
    category: 'pointer_safety',
    title: 'malloc结果未检查',
    description: 'malloc 返回值在使用前没有判空，低内存场景下继续解引用会导致崩溃。',
    file_path: 'src/storage.c',
    line: 65,
  },
  {
    severity: 'low',
    category: 'pointer_safety',
    title: '返回指针未判空',
    description: '外部接口返回的指针在使用前没有判空，异常场景下可能触发空指针解引用。',
    file_path: 'src/scheduler.c',
    line: 74,
  },
  {
    severity: 'suggestion',
    category: 'logic',
    title: '状态更新条件错误',
    description: '错误分支仍然更新任务状态为成功，调用方会误判处理结果。',
    file_path: 'src/scheduler.c',
    line: 131,
  },
  {
    severity: 'suggestion',
    category: 'logic',
    title: '错误处理路径不完整',
    description: '初始化失败后继续执行后续步骤，可能让调用方看到部分初始化的对象。',
    file_path: 'src/main.c',
    line: 33,
  },
]

const cloneFindings = (count: number, offset = 0) => Array.from({ length: count }, (_, index) => {
  const base = findingTemplates[(index + offset) % findingTemplates.length]
  return {
    ...base,
    title: index < findingTemplates.length ? base.title : `${base.title} #${index + 1}`,
    line: (base.line || 1) + Math.floor(index / findingTemplates.length) * 9,
  }
})

const summarizeFindings = (items: Finding[]) => ({
  high_count: items.filter((item) => item.severity === 'high').length,
  medium_count: items.filter((item) => item.severity === 'medium').length,
  low_count: items.filter((item) => item.severity === 'low').length,
  suggestion_count: items.filter((item) => item.severity === 'suggestion').length,
  category_counts: items.reduce<Record<string, number>>((counts, item) => {
    counts[item.category] = (counts[item.category] || 0) + 1
    return counts
  }, {}),
})

const makeReport = (reportId: string, taskId: string, findingCount = 8, score = 72): Report => {
  const findings = cloneFindings(findingCount, taskId.length % findingTemplates.length)
  const counts = summarizeFindings(findings)
  return {
    id: reportId,
    task_id: taskId,
    summary: `分片审查完成，共发现 ${findingCount} 个问题，已保存全部问题并按风险等级排序。`,
    score,
    ...counts,
    result_json: {
      summary: `发现 ${findingCount} 个问题，其中高危 ${counts.high_count} 个，中危 ${counts.medium_count} 个。`,
      score,
      findings,
    },
  }
}

const modelCatalog: ModelCatalogItem[] = [
  {
    key: 'qwen2.5-coder-14b-instruct',
    display_name: 'Qwen2.5-Coder 14B Instruct',
    model_identifier: 'Qwen/Qwen2.5-Coder-14B-Instruct',
    description: '适合 C 代码批量审查和日常风险定位。',
    recommended_source: 'modelscope',
    huggingface_repo: 'Qwen/Qwen2.5-Coder-14B-Instruct',
    modelscope_repo: 'Qwen/Qwen2.5-Coder-14B-Instruct',
    default_port: 8101,
    default_served_model_name: 'qwen2.5-coder-14b-instruct',
    estimated_vram_gb: 28,
    tags: ['c', 'security', 'mock-ready'],
  },
  {
    key: 'deepseek-coder-14b-instruct',
    display_name: 'DeepSeek-Coder 14B Instruct',
    model_identifier: 'deepseek-ai/deepseek-coder-14b-instruct',
    description: '适合复杂逻辑与安全审计的 C/C++ 代码模型。',
    recommended_source: 'modelscope',
    huggingface_repo: 'deepseek-ai/deepseek-coder-14b-instruct',
    modelscope_repo: 'deepseek-ai/deepseek-coder-14b-instruct',
    default_port: 8102,
    default_served_model_name: 'deepseek-coder-14b-instruct',
    estimated_vram_gb: 30,
    tags: ['c', 'security', 'instruct'],
  },
  {
    key: 'mock-local-reviewer',
    display_name: 'Mock 审查模型',
    model_identifier: 'mock-local-reviewer',
    description: '无需后端服务，用于前端联调、并发任务和报告展示验证。',
    recommended_source: 'local',
    default_port: 8800,
    default_served_model_name: 'mock-local-reviewer',
    estimated_vram_gb: 0,
    tags: ['mock', 'frontend', 'testing'],
  },
  {
    key: 'starcoder2-15b',
    display_name: 'StarCoder2 15B',
    model_identifier: 'bigcode/starcoder2-15b',
    description: '适合批量代码质量检查和兼容性审查。',
    recommended_source: 'huggingface',
    huggingface_repo: 'bigcode/starcoder2-15b',
    modelscope_repo: 'AI-ModelScope/starcoder2-15b',
    default_port: 8103,
    default_served_model_name: 'starcoder2-15b',
    estimated_vram_gb: 30,
    tags: ['c', 'batch', 'code'],
  },
]

const seedTasks = (created: string): ReviewTask[] => [
  {
    id: 'review-seeded',
    owner_id: 'user-admin',
    model_node_id: 'model-qwen',
    input_mode: 'archive',
    display_name: 'embedded-gateway-demo.zip',
    status: 'completed',
    progress: 100,
    duration_ms: 12840,
    file_count: 10,
    finding_count: 36,
    report_id: 'report-seeded',
    files: demoFiles(10, 'seeded'),
    check_types: allCheckTypes,
    created_at: minutesAgo(110),
    updated_at: minutesAgo(90),
    completed_at: minutesAgo(90),
  },
  {
    id: 'review-demo-completed',
    owner_id: 'user-demo',
    model_node_id: 'model-mock',
    input_mode: 'folder',
    display_name: 'sensor_gateway',
    status: 'completed',
    progress: 100,
    duration_ms: 8420,
    file_count: 14,
    finding_count: 21,
    report_id: 'report-demo-completed',
    files: demoFiles(14, 'demo-completed'),
    check_types: ['memory_safety', 'resource_leak', 'logic'],
    created_at: minutesAgo(76),
    updated_at: minutesAgo(62),
    completed_at: minutesAgo(62),
  },
  {
    id: 'review-running-admin',
    owner_id: 'user-admin',
    model_node_id: 'model-qwen',
    input_mode: 'folder',
    display_name: 'USBSample.c',
    status: 'running',
    progress: 57,
    file_count: 10,
    finding_count: 12,
    files: demoFiles(10, 'running-admin'),
    check_types: allCheckTypes,
    started_at: minutesAgo(9),
    created_at: minutesAgo(14),
    updated_at: minutesAgo(2),
    model_log: '[mock] 正在审查 drivers/stm32f10x_adc.c\n[mock] 已完成 6/10 个文件\n[mock] 当前批次使用 Mock 审查模型',
  },
  {
    id: 'review-running-demo',
    owner_id: 'user-demo',
    model_node_id: 'model-mock',
    input_mode: 'file',
    display_name: 'network_driver.c',
    status: 'running',
    progress: 41,
    file_count: 1,
    finding_count: 3,
    files: [{ id: 'file-running-demo', relative_path: 'network_driver.c', size_bytes: 4096 }],
    check_types: ['memory_safety', 'pointer_safety', 'logic'],
    started_at: minutesAgo(4),
    created_at: minutesAgo(5),
    updated_at: minutesAgo(1),
    model_log: '[mock] 单文件审查中，已识别输入校验和指针安全风险。',
  },
  {
    id: 'review-queued-1',
    owner_id: 'user-alice',
    model_node_id: 'model-mock',
    input_mode: 'archive',
    display_name: 'motor_control.zip',
    status: 'queued',
    progress: 0,
    queue_priority: 1,
    queued_ahead_count: 0,
    file_count: 18,
    finding_count: 0,
    files: demoFiles(12, 'queue-1'),
    check_types: ['integer_safety', 'logic'],
    created_at: minutesAgo(3),
    updated_at: minutesAgo(3),
  },
  {
    id: 'review-queued-2',
    owner_id: 'user-demo',
    model_node_id: 'model-mock',
    input_mode: 'folder',
    display_name: 'rtos_porting',
    status: 'queued',
    progress: 0,
    queued_ahead_count: 1,
    file_count: 22,
    finding_count: 0,
    files: demoFiles(12, 'queue-2'),
    check_types: ['memory_safety', 'buffer_overflow', 'integer_safety'],
    created_at: minutesAgo(2),
    updated_at: minutesAgo(2),
  },
  {
    id: 'review-failed',
    owner_id: 'user-bob',
    model_node_id: 'model-deepseek',
    input_mode: 'archive',
    display_name: 'legacy-module.zip',
    status: 'failed',
    progress: 100,
    error_message: 'Mock 模型模拟：结构化响应校验失败，已保留模型日志供排查。',
    model_log: '[mock] invalid structured response: fixed_snippet.kind should be context/removed/added',
    duration_ms: 3100,
    file_count: 8,
    finding_count: 0,
    files: demoFiles(8, 'failed'),
    check_types: ['memory_safety', 'logic'],
    created_at: minutesAgo(42),
    updated_at: minutesAgo(39),
    completed_at: minutesAgo(39),
  },
]

const seedState = (): MockState => {
  const created = now()
  const tasks = seedTasks(created)
  return {
    version: STATE_VERSION,
    users: [
      { id: 'user-admin', username: 'admin', role: 'admin', is_enabled: true, created_at: created },
      { id: 'user-demo', username: 'demo', role: 'user', is_enabled: true, created_at: created },
      { id: 'user-alice', username: 'alice', role: 'user', is_enabled: true, created_at: created },
      { id: 'user-bob', username: 'bob', role: 'user', is_enabled: true, created_at: created },
      { id: 'user-disabled', username: 'disabled_user', role: 'user', is_enabled: false, created_at: created },
    ],
    passwords: {
      admin: 'admin12345678',
      demo: 'demo12345678',
      alice: 'alice12345678',
      bob: 'bob12345678',
      disabled_user: 'disabled12345678',
    },
    models: [
      { id: 'model-qwen', display_name: 'Qwen2.5-Coder 14B Instruct', model_identifier: 'qwen2.5-coder-14b-instruct', base_url: 'http://gpu-node-01:8001', timeout_seconds: 120, is_enabled: true, is_default: true, gpu_indices: [0, 1], tensor_parallel_size: 2, description: '由手动部署验证登记，可直接用于代码审查。', created_at: created },
      { id: 'model-mock', display_name: 'Mock 审查模式', model_identifier: 'mock-local-reviewer', base_url: 'mock://frontend', timeout_seconds: 5, is_enabled: true, is_default: false, gpu_indices: [], tensor_parallel_size: 1, description: '前端本地假数据模式，覆盖登录、上传、下载、报告和后台管理流程。', created_at: created },
      { id: 'model-deepseek', display_name: 'DeepSeek-Coder 33B', model_identifier: 'deepseek-coder-33b-instruct', base_url: 'http://gpu-node-02:8001', timeout_seconds: 180, is_enabled: true, is_default: false, gpu_indices: [0], tensor_parallel_size: 1, description: '适合复杂逻辑与安全漏洞审计。', created_at: created },
    ],
    deployments: [
      {
        id: 'deploy-qwen',
        catalog_key: 'qwen2.5-coder-14b-instruct',
        display_name: 'Qwen2.5-Coder 14B Instruct',
        model_identifier: 'Qwen/Qwen2.5-Coder-14B-Instruct',
        source: 'modelscope',
        source_repository: 'Qwen/Qwen2.5-Coder-14B-Instruct',
        served_model_name: 'qwen2.5-coder-14b-instruct',
        base_url: 'http://127.0.0.1:8001',
        port: 8001,
        service_name: 'c-check-vllm-qwen25-coder-14b',
        gpu_indices: [0, 1],
        tensor_parallel_size: 2,
        status: 'succeeded',
        progress: 100,
        log: 'Mock 数据：模型节点已登记，服务健康检查通过。',
        model_node_id: 'model-qwen',
        created_at: minutesAgo(180),
        updated_at: minutesAgo(120),
      },
      {
        id: 'deploy-mock',
        catalog_key: 'mock-local-reviewer',
        display_name: 'Mock 审查模型',
        model_identifier: 'mock-local-reviewer',
        source: 'local',
        source_repository: 'frontend/mock',
        served_model_name: 'mock-local-reviewer',
        base_url: 'mock://frontend',
        port: 8800,
        service_name: 'c-check-frontend-mock',
        gpu_indices: [],
        tensor_parallel_size: 1,
        status: 'running',
        progress: 88,
        log: 'Mock 数据：用于多用户并发和页面联调。',
        model_node_id: 'model-mock',
        created_at: minutesAgo(30),
        updated_at: minutesAgo(1),
      },
    ],
    prompts: [
      { id: 'prompt-2', version: 2, body: 'C 语言企业级审查提示词：检查内存安全、逻辑漏洞、资源释放、并发安全、性能和可移植性。输出结构化 JSON，并保留代码片段。', is_active: true, created_at: created },
      { id: 'prompt-1', version: 1, body: 'C 语言基础审查提示词。', is_active: false, created_at: minutesAgo(240) },
    ],
    tasks,
    reports: [
      makeReport('report-seeded', 'review-seeded', 36, 78),
      makeReport('report-demo-completed', 'review-demo-completed', 21, 84),
    ],
    polls: {},
  }
}

const load = (): MockState => {
  const stored = localStorage.getItem(STATE_KEY)
  if (stored) {
    const state = JSON.parse(stored) as MockState
    if (state.version === STATE_VERSION) return state
  }
  const state = seedState()
  save(state)
  return state
}
const save = (state: MockState) => localStorage.setItem(STATE_KEY, JSON.stringify(state))
const currentUsername = () => localStorage.getItem(SESSION_KEY)
const currentUser = (state: MockState) => state.users.find((user) => user.username === currentUsername())
const requireUser = (state: MockState) => {
  const user = currentUser(state)
  if (!user) throw new Error('登录状态已失效，请重新登录')
  return user
}
const visibleTasks = (state: MockState) => {
  const user = requireUser(state)
  return user.role === 'admin' ? state.tasks : state.tasks.filter((task) => task.owner_id === user.id)
}
const taskToAdmin = (task: ReviewTask): AdminTask => ({
  id: task.id,
  owner_id: task.owner_id,
  model_node_id: task.model_node_id,
  display_name: task.display_name,
  status: task.status,
  progress: task.progress,
  queue_priority: task.queue_priority,
  queued_ahead_count: task.queued_ahead_count,
  finding_count: task.finding_count,
  error_message: task.error_message,
  created_at: task.created_at,
})

export const resetMockState = () => {
  localStorage.removeItem(STATE_KEY)
  localStorage.removeItem(SESSION_KEY)
}

function dashboardFromState(state: MockState): Dashboard {
  return {
    users: state.users.length,
    enabled_users: state.users.filter((user) => user.is_enabled).length,
    models: state.models.length,
    enabled_models: state.models.filter((model) => model.is_enabled).length,
    tasks: state.tasks.length,
    queued_tasks: state.tasks.filter((task) => task.status === 'queued').length,
    running_tasks: state.tasks.filter((task) => task.status === 'running').length,
    completed_tasks: state.tasks.filter((task) => task.status === 'completed').length,
    failed_tasks: state.tasks.filter((task) => task.status === 'failed').length,
  }
}

function ensureReportForTask(state: MockState, task: ReviewTask) {
  if (!task.report_id) task.report_id = `report-${task.id}`
  if (!state.reports.some((report) => report.id === task.report_id)) {
    const count = Math.max(4, task.finding_count || Math.min(24, task.file_count * 2))
    state.reports.push(makeReport(task.report_id, task.id, count, Math.max(45, 92 - count)))
  }
}

function progressTask(state: MockState, task: ReviewTask) {
  if (task.status !== 'queued' && task.status !== 'running') return
  const polls = (state.polls[task.id] || 0) + 1
  state.polls[task.id] = polls
  const hasRunningTask = state.tasks.some((item) => item.id !== task.id && item.status === 'running')

  if (task.status === 'queued' && hasRunningTask && polls < 2) {
    task.queued_ahead_count = Math.max(0, task.queued_ahead_count ?? 1)
    task.updated_at = now()
    return
  }

  if (polls >= 4) {
    task.status = 'completed'
    task.progress = 100
    task.duration_ms = 2800 + task.file_count * 640
    task.finding_count = Math.max(task.finding_count, Math.min(48, task.file_count * 3))
    task.completed_at = now()
    ensureReportForTask(state, task)
  } else {
    task.status = 'running'
    task.progress = Math.min(88, Math.max(task.progress, 25 + polls * 18))
    task.started_at ||= now()
    task.model_log = `[mock] ${task.display_name} 审查中\n[mock] 当前进度 ${task.progress}%\n[mock] 已按串行队列处理文件`
  }
  task.updated_at = now()
}

export const mockApi = {
  auth: {
    login: async (username: string, password: string) => {
      const state = load()
      const user = state.users.find((item) => item.username === username)
      if (!user || !user.is_enabled || state.passwords[username] !== password) throw new Error('账号或密码错误')
      localStorage.setItem(SESSION_KEY, username)
      return response({ access_token: `mock-token-${username}` })
    },
    me: async () => response(requireUser(load()) as User),
    password: async (currentPassword?: string, newPassword?: string) => {
      const state = load()
      const user = requireUser(state)
      if (currentPassword && state.passwords[user.username] !== currentPassword) throw new Error('当前密码不正确')
      if (newPassword) state.passwords[user.username] = newPassword
      save(state)
      return response({ ok: true })
    },
  },
  models: async () => {
    const state = load()
    const models = state.models.filter((model) => model.is_enabled).sort((a, b) => Number(b.is_default) - Number(a.is_default))
    return response(requireUser(state).role === 'admin' ? models : models.filter((model) => model.is_default || model.base_url.startsWith('mock://')))
  },
  reviews: {
    submitText: async (model_node_id: string, source_text: string, check_types: string[], display_name?: string) => createReview(model_node_id, 'text', display_name || 'snippet.c', source_text ? 1 : 0, check_types),
    submitFile: async (mode: 'file' | 'archive', model_node_id: string, file: File, check_types: string[], display_name?: string) => createReview(model_node_id, mode, display_name || file.name, mode === 'archive' ? 12 : 1, check_types),
    submitFolder: async (model_node_id: string, files: File[], check_types: string[], display_name?: string) => createReview(model_node_id, 'folder', display_name || 'selected-folder', files.length, check_types),
    submitDemoArchive: async (check_types: string[]) => createReview('model-mock', 'archive', 'embedded-gateway-live-demo.zip', 12, check_types),
    list: async (params?: Record<string, unknown>) => {
      const state = load()
      let tasks = visibleTasks(state)
      if (params?.keyword) tasks = tasks.filter((task) => task.display_name.includes(String(params.keyword)))
      if (params?.tester_name) tasks = tasks.filter((task) => state.users.find((user) => user.id === task.owner_id)?.username.includes(String(params.tester_name)))
      if (params?.status) tasks = tasks.filter((task) => task.status === params.status)
      if (params?.model_node_id) tasks = tasks.filter((task) => task.model_node_id === params.model_node_id)
      if (params?.start_time) tasks = tasks.filter((task) => new Date(task.created_at) >= new Date(String(params.start_time)))
      if (params?.end_time) tasks = tasks.filter((task) => new Date(task.created_at) <= new Date(String(params.end_time)))
      if (params?.severity) {
        const countKey = `${String(params.severity)}_count` as `${Severity}_count`
        tasks = tasks.filter((task) => {
          const report = state.reports.find((item) => item.id === task.report_id)
          return Boolean(report?.[countKey as keyof Report])
        })
      }
      const sortBy = String(params?.sort_by || 'created_at')
      const sortDir = params?.sort_dir === 'asc' ? 1 : -1
      const modelName = (task: ReviewTask) => state.models.find((model) => model.id === task.model_node_id)?.display_name || task.model_node_id
      const testerName = (task: ReviewTask) => state.users.find((user) => user.id === task.owner_id)?.username || task.owner_id
      const value = (task: ReviewTask) => sortBy === 'tester_name' ? testerName(task) : sortBy === 'model' ? modelName(task) : task[sortBy as keyof ReviewTask] ?? ''
      tasks = [...tasks].sort((left, right) => String(value(left)).localeCompare(String(value(right)), 'zh-CN', { numeric: true }) * sortDir)
      const total = tasks.length
      const offset = Number(params?.offset || 0), limit = Number(params?.limit || 20)
      return response({ items: tasks.slice(offset, offset + limit).map((task) => ({ ...task, tester_name: testerName(task) })), total })
    },
    get: async (taskId: string) => {
      const state = load()
      const task = visibleTasks(state).find((item) => item.id === taskId)
      if (!task) throw new Error('审查任务不存在')
      progressTask(state, task)
      save(state)
      return response(task)
    },
    remove: async (taskId: string) => {
      const state = load()
      const task = visibleTasks(state).find((item) => item.id === taskId)
      if (!task) throw new Error('审查任务不存在')
      state.tasks = state.tasks.filter((item) => item.id !== taskId)
      state.reports = state.reports.filter((report) => report.task_id !== taskId)
      save(state)
      return response({ ok: true })
    },
    pin: async (taskId: string) => {
      const state = load()
      const task = visibleTasks(state).find((item) => item.id === taskId)
      if (!task) throw new Error('审查任务不存在')
      if (task.status !== 'queued') throw new Error('只有排队中的任务可以置顶')
      state.tasks.forEach((item) => {
        if (item.id !== task.id && item.status === 'queued') item.queue_priority = undefined
      })
      task.queue_priority = 1
      task.queued_ahead_count = 0
      save(state)
      return response(task)
    },
  },
  reports: {
    get: async (reportId: string) => {
      const state = load()
      const report = state.reports.find((item) => item.id === reportId)
      if (!report) throw new Error('审查报告不存在')
      return response(report)
    },
    download: async (reportId: string, format: 'markdown' | 'pdf') => {
      const state = load()
      const report = state.reports.find((item) => item.id === reportId)
      if (!report) throw new Error('审查报告不存在')
      const task = state.tasks.find((item) => item.id === report.task_id)
      const taskLine = task ? `任务：${task.display_name}\n输入方式：${task.input_mode}\n` : ''
      const content = format === 'markdown'
        ? `# C-Check 审查报告\n\n${taskLine}\n${report.summary}\n\n发现问题：${report.result_json.findings.length} 个`
        : `C-Check mock PDF report\n${taskLine}${report.summary}`
      return response(new Blob([content], { type: format === 'markdown' ? 'text/markdown' : 'application/pdf' }))
    },
  },
  admin: {
    dashboard: async () => response(dashboardFromState(load())),
    resources: async () => {
      const state = load()
      const tasks = dashboardFromState(state)
      const jitter = Math.round(Date.now() / 1000) % 8
      return response({
        captured_at: now(),
        system: {
          cpu_percent: 32 + jitter,
          load_average_1m: 1.35 + jitter / 10,
          memory_total_bytes: 128 * 1024 ** 3,
          memory_used_bytes: (52 + jitter) * 1024 ** 3,
          memory_percent: 41 + jitter,
          disk_total_bytes: 1024 * 1024 ** 3,
          disk_used_bytes: 286 * 1024 ** 3,
          disk_percent: 28,
        },
        gpus: [
          { index: 0, name: 'NVIDIA RTX 4090', utilization_percent: 71 + jitter, memory_used_mb: 18_432, memory_total_mb: 24_576, memory_percent: 75, temperature_c: 62, power_w: 328 },
          { index: 1, name: 'NVIDIA RTX 4090', utilization_percent: 58 + jitter, memory_used_mb: 16_896, memory_total_mb: 24_576, memory_percent: 69, temperature_c: 59, power_w: 301 },
        ],
        models: state.models.map((model, index) => ({
          node_id: model.id,
          display_name: model.display_name,
          base_url: model.base_url,
          gpu_indices: model.gpu_indices,
          tensor_parallel_size: model.tensor_parallel_size,
          metrics_available: model.is_enabled,
          prompt_throughput_tps: model.is_enabled ? 980 - index * 120 + jitter * 12 : null,
          generation_throughput_tps: model.is_enabled ? 86 - index * 9 + jitter : null,
          running_requests: model.is_enabled ? Math.max(0, tasks.running_tasks - index) : 0,
          pending_requests: index === 0 ? tasks.queued_tasks : 0,
          gpu_kv_cache_usage_percent: model.is_enabled ? 34 + jitter + index * 5 : null,
        })),
        tasks,
      } satisfies ResourceSnapshot)
    },
    users: async () => response(load().users),
    createUser: async (payload: { username: string; password: string; role: string }) => {
      const state = load()
      if (state.users.some((user) => user.username === payload.username)) throw new Error('用户名已存在')
      state.users.push({ id: id('user'), username: payload.username, role: payload.role === 'admin' ? 'admin' : 'user', is_enabled: true, created_at: now() })
      state.passwords[payload.username] = payload.password
      save(state)
      return response({ ok: true })
    },
    enableUser: async (userId: string, is_enabled: boolean) => update(state => state.users.find(user => user.id === userId)!.is_enabled = is_enabled),
    resetPassword: async (userId?: string, password?: string) => update(state => {
      const user = state.users.find((item) => item.id === userId)
      if (user && password) state.passwords[user.username] = password
    }),
    models: async () => response(load().models),
    saveModel: async (payload: Partial<ModelNode> & { display_name: string; model_identifier: string; base_url: string }, modelId?: string) => {
      const state = load()
      if (modelId) Object.assign(state.models.find((model) => model.id === modelId)!, payload)
      else state.models.push({ id: id('model'), timeout_seconds: 120, is_enabled: true, is_default: !state.models.some(model => model.is_default), created_at: now(), ...payload, gpu_indices: payload.gpu_indices || [], tensor_parallel_size: payload.tensor_parallel_size || 1 })
      save(state)
      return response({ ok: true })
    },
    enableModel: async (modelId: string, is_enabled: boolean) => update(state => state.models.find(model => model.id === modelId)!.is_enabled = is_enabled),
    defaultModel: async (modelId: string) => update(state => state.models.forEach(model => { model.is_default = model.id === modelId })),
    deleteModel: async (modelId: string) => update(state => { state.models = state.models.filter(model => model.id !== modelId) }),
    modelHealth: async (_id?: string) => response({ ok: true }),
    modelCatalog: async () => response(modelCatalog),
    modelDeployments: async () => response(load().deployments),
    createModelDeployment: async (payload: Record<string, unknown>) => {
      const state = load()
      const catalog = modelCatalog.find((item) => item.key === payload.catalog_key) || modelCatalog[0]
      const deployment: ModelDeployment = {
        id: id('deploy'),
        catalog_key: String(payload.catalog_key || catalog.key),
        display_name: String(payload.display_name || catalog.display_name),
        model_identifier: String(payload.model_identifier || catalog.model_identifier),
        source: String(payload.source || catalog.recommended_source),
        source_repository: String(payload.source_repository || catalog.modelscope_repo || catalog.huggingface_repo || catalog.model_identifier),
        served_model_name: String(payload.served_model_name || catalog.default_served_model_name || catalog.key),
        base_url: String(payload.base_url || `http://127.0.0.1:${catalog.default_port || 8101}`),
        port: Number(payload.port || catalog.default_port || 8101),
        service_name: String(payload.service_name || `c-check-vllm-${catalog.key}`),
        gpu_indices: Array.isArray(payload.gpu_indices) ? payload.gpu_indices as number[] : [0, 1],
        tensor_parallel_size: Number(payload.tensor_parallel_size || 2),
        status: 'queued',
        progress: 0,
        log: 'Mock 部署任务已加入队列。',
        model_node_id: id('model'),
        created_at: now(),
        updated_at: now(),
      }
      state.deployments.unshift(deployment)
      state.models.push({
        id: deployment.model_node_id!,
        display_name: deployment.display_name,
        model_identifier: deployment.served_model_name,
        base_url: deployment.base_url,
        timeout_seconds: Number(payload.timeout_seconds || 180),
        is_enabled: true,
        is_default: !state.models.some(model => model.is_default),
        gpu_indices: Array.isArray(payload.gpu_indices) ? payload.gpu_indices as number[] : [0, 1],
        tensor_parallel_size: Number(payload.tensor_parallel_size || 2),
        description: '由模型部署任务自动登记。',
        created_at: now(),
      })
      save(state)
      return response(deployment)
    },
    prompts: async () => response(load().prompts.sort((a, b) => a.version - b.version)),
    createPrompt: async (body: string) => {
      const state = load()
      state.prompts.push({ id: id('prompt'), version: Math.max(...state.prompts.map(prompt => prompt.version)) + 1, body, is_active: false, created_at: now() })
      save(state)
      return response({ ok: true })
    },
    activatePrompt: async (promptId: string) => update(state => state.prompts.forEach(prompt => { prompt.is_active = prompt.id === promptId })),
    updatePrompt: async (promptId: string, body: string) => update(state => { state.prompts.find(prompt => prompt.id === promptId)!.body = body }),
    deletePrompt: async (promptId: string) => update(state => {
      const prompt = state.prompts.find(item => item.id === promptId)
      if (!prompt || prompt.is_active || state.prompts.length <= 1) throw new Error('当前启用版本或最后一个版本不能删除')
      state.prompts = state.prompts.filter(item => item.id !== promptId)
    }),
    tasks: async (status?: TaskStatus | '') => response(load().tasks.filter(task => !status || task.status === status).map(taskToAdmin)),
  },
}

async function createReview(model_node_id: string, input_mode: string, display_name: string, file_count: number, check_types: string[]) {
  const state = load()
  const user = requireUser(state)
  const taskId = id('review')
  const created = now()
  const activeTasks = state.tasks.filter((task) => task.status === 'queued' || task.status === 'running')
  const files = input_mode === 'archive' || input_mode === 'folder'
    ? demoFiles(file_count, taskId)
    : [{ id: 'file-1', relative_path: display_name, size_bytes: 4096 }]
  const task: ReviewTask = {
    id: taskId,
    owner_id: user.id,
    model_node_id,
    input_mode,
    display_name,
    status: activeTasks.length ? 'queued' : 'running',
    progress: activeTasks.length ? 0 : 18,
    queued_ahead_count: activeTasks.length,
    file_count,
    finding_count: 0,
    files,
    check_types,
    created_at: created,
    updated_at: created,
    started_at: activeTasks.length ? undefined : created,
  }
  state.tasks.unshift(task)
  save(state)
  return response(task)
}

async function update(mutator: (state: MockState) => void) {
  const state = load()
  mutator(state)
  save(state)
  return response({ ok: true })
}
