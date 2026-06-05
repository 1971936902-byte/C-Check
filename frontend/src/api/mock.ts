import type { AdminTask, AdminUser, Dashboard, ModelCatalogItem, ModelDeployment, ModelNode, Prompt, Report, ResourceSnapshot, ReviewTask, TaskStatus, User } from '../types'

const STATE_KEY = 'c-check-mock-state'
const SESSION_KEY = 'c-check-mock-session'
const STATE_VERSION = 4

type MockState = {
  version: number
  users: AdminUser[]
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
const demoFiles = (count = 6) => ['src/main.c', 'src/parser.c', 'src/config.c', 'src/network.c', 'include/config.h', 'include/protocol.h']
  .slice(0, count)
  .map((relative_path, index) => ({ id: `file-${index + 1}`, relative_path, size_bytes: 180 + index * 73 }))

const findings = [
  { severity: 'high' as const, category: 'memory_safety', title: '固定长度缓冲区存在越界写入风险', description: '使用 strcpy 复制外部输入时未校验长度，输入超过目标缓冲区容量会破坏栈内存。', file_path: 'src/parser.c', line: 42, remediation: '改用 snprintf 或显式校验输入长度，并为结尾的空字符预留空间。', code_snippet: [{ line: 40, content: 'char name[32];', kind: 'context' as const }, { line: 41, content: 'const char *input = request->name;', kind: 'context' as const }, { line: 42, content: 'strcpy(name, input);', kind: 'removed' as const }, { line: 43, content: 'process_name(name);', kind: 'context' as const }], fixed_snippet: [{ line: 40, content: 'char name[32];', kind: 'context' as const }, { line: 41, content: 'const char *input = request->name;', kind: 'context' as const }, { line: 42, content: 'snprintf(name, sizeof(name), \"%s\", input);', kind: 'added' as const }, { line: 43, content: 'process_name(name);', kind: 'context' as const }] },
  { severity: 'medium' as const, category: 'logic', title: '文件句柄在异常分支未关闭', description: '读取失败后函数提前返回，已打开的 FILE 指针没有释放。', file_path: 'src/config.c', line: 87, remediation: '将资源释放集中到统一 cleanup 分支，所有退出路径都执行 fclose。', code_snippet: [{ line: 85, content: 'FILE *file = fopen(path, \"r\");', kind: 'context' as const }, { line: 86, content: 'if (read_config(file, config) < 0) {', kind: 'context' as const }, { line: 87, content: '    return -1;', kind: 'removed' as const }, { line: 88, content: '}', kind: 'context' as const }], fixed_snippet: [{ line: 85, content: 'FILE *file = fopen(path, \"r\");', kind: 'context' as const }, { line: 86, content: 'if (read_config(file, config) < 0) {', kind: 'context' as const }, { line: 87, content: '    fclose(file);', kind: 'added' as const }, { line: 88, content: '    return -1;', kind: 'added' as const }, { line: 89, content: '}', kind: 'context' as const }] },
  { severity: 'low' as const, category: 'portability', title: '整数类型依赖平台位宽', description: '使用 long 保存协议字段，在不同 ABI 下位宽可能不同。', file_path: 'include/protocol.h', line: 18, remediation: '改用 stdint.h 中的 uint32_t，并在序列化时明确字节序。', code_snippet: [{ line: 17, content: 'typedef struct packet_header {', kind: 'context' as const }, { line: 18, content: '    unsigned long payload_size;', kind: 'removed' as const }, { line: 19, content: '} packet_header_t;', kind: 'context' as const }], fixed_snippet: [{ line: 17, content: 'typedef struct packet_header {', kind: 'context' as const }, { line: 18, content: '    uint32_t payload_size;', kind: 'added' as const }, { line: 19, content: '} packet_header_t;', kind: 'context' as const }] },
  { severity: 'suggestion' as const, category: 'performance', title: '循环内重复计算字符串长度', description: '循环条件每次调用 strlen，会重复扫描字符串。', file_path: 'src/utils.c', line: 31, remediation: '进入循环前缓存字符串长度，减少重复遍历。', code_snippet: [{ line: 30, content: 'size_t count_letters(const char *text) {', kind: 'context' as const }, { line: 31, content: '    for (size_t i = 0; i < strlen(text); ++i) {', kind: 'removed' as const }, { line: 32, content: '        inspect(text[i]);', kind: 'context' as const }], fixed_snippet: [{ line: 30, content: 'size_t count_letters(const char *text) {', kind: 'context' as const }, { line: 31, content: '    const size_t length = strlen(text);', kind: 'added' as const }, { line: 32, content: '    for (size_t i = 0; i < length; ++i) {', kind: 'added' as const }, { line: 33, content: '        inspect(text[i]);', kind: 'context' as const }] },
]

const modelCatalog: ModelCatalogItem[] = [
  {
    key: 'deepseek-coder-14b-instruct',
    display_name: 'DeepSeek-Coder 14B Instruct',
    model_identifier: 'deepseek-ai/deepseek-coder-14b-instruct',
    description: '适合复杂逻辑与安全审计的 C/C++ 代码模型。',
    recommended_source: 'modelscope',
    huggingface_repo: 'deepseek-ai/deepseek-coder-14b-instruct',
    modelscope_repo: 'deepseek-ai/deepseek-coder-14b-instruct',
    default_port: 8101,
    default_served_model_name: 'deepseek-coder-14b-instruct',
    estimated_vram_gb: 28,
    tags: ['c', 'security', 'instruct'],
  },
  {
    key: 'codellama-13b-instruct',
    display_name: 'CodeLlama 13B Instruct',
    model_identifier: 'codellama/CodeLlama-13b-Instruct-hf',
    description: '适合作为兼容性与代码规范审查的备选节点。',
    recommended_source: 'huggingface',
    huggingface_repo: 'codellama/CodeLlama-13b-Instruct-hf',
    modelscope_repo: 'LLM-Research/CodeLlama-13b-Instruct-hf',
    default_port: 8102,
    default_served_model_name: 'codellama-13b-instruct',
    estimated_vram_gb: 26,
    tags: ['c', 'instruct', 'fallback'],
  },
  {
    key: 'starcoder2-15b',
    display_name: 'StarCoder2 15B',
    model_identifier: 'bigcode/starcoder2-15b',
    description: '适合作为批量代码质量检查节点。',
    recommended_source: 'huggingface',
    huggingface_repo: 'bigcode/starcoder2-15b',
    modelscope_repo: 'AI-ModelScope/starcoder2-15b',
    default_port: 8103,
    default_served_model_name: 'starcoder2-15b',
    estimated_vram_gb: 30,
    tags: ['c', 'batch', 'code'],
  },
]

const makeReport = (reportId: string, taskId: string): Report => ({
  id: reportId,
  task_id: taskId,
  summary: '本次审查覆盖内存安全、逻辑正确性、可移植性与性能。建议优先修复缓冲区越界风险，再处理资源释放与类型兼容问题。',
  score: 78,
  high_count: 1,
  medium_count: 1,
  low_count: 1,
  suggestion_count: 1,
  category_counts: { memory_safety: 1, logic: 1, portability: 1, performance: 1 },
  result_json: { summary: '发现 4 个需要关注的问题。', score: 78, findings },
})

const seedState = (): MockState => {
  const created = now()
  const taskId = 'review-seeded'
  const reportId = 'report-seeded'
  return {
    version: STATE_VERSION,
    users: [
      { id: 'user-admin', username: 'admin', role: 'admin', is_enabled: true, created_at: created },
      { id: 'user-demo', username: 'demo', role: 'user', is_enabled: true, created_at: created },
      { id: 'user-disabled', username: 'disabled_user', role: 'user', is_enabled: false, created_at: created },
    ],
    models: [
      { id: 'model-qwen', display_name: 'Qwen3-Coder 30B', model_identifier: 'qwen3-coder-30b', base_url: 'http://gpu-node-01:8000', timeout_seconds: 120, is_enabled: true, is_default: true, description: '适合日常批量代码审查与规范检查。', created_at: created },
      { id: 'model-deepseek', display_name: 'DeepSeek-Coder 33B', model_identifier: 'deepseek-coder-33b-instruct', base_url: 'http://gpu-node-02:8000', timeout_seconds: 180, is_enabled: true, is_default: false, description: '适合复杂逻辑与安全漏洞审计。', created_at: created },
    ],
    deployments: [
      {
        id: 'deploy-seeded',
        catalog_key: 'deepseek-coder-14b-instruct',
        display_name: 'DeepSeek-Coder 14B Instruct',
        model_identifier: 'deepseek-ai/deepseek-coder-14b-instruct',
        source: 'modelscope',
        source_repository: 'deepseek-ai/deepseek-coder-14b-instruct',
        served_model_name: 'deepseek-coder-14b-instruct',
        base_url: 'http://127.0.0.1:8101',
        port: 8101,
        service_name: 'c-check-vllm-deepseek-coder-14b-instruct',
        status: 'manual_required',
        progress: 10,
        log: '自动部署未开启；请在 Linux GPU 服务器设置 MODEL_DEPLOYMENT_ENABLED=true 后执行。',
        model_node_id: 'model-qwen',
        created_at: created,
        updated_at: created,
      },
    ],
    prompts: [
      { id: 'prompt-2', version: 2, body: 'C 语言企业级审查提示词：检查内存安全、逻辑漏洞、性能、规范与可移植性。', is_active: true, created_at: created },
      { id: 'prompt-1', version: 1, body: 'C 语言基础审查提示词。', is_active: false, created_at: created },
    ],
    tasks: [
      { id: taskId, owner_id: 'user-admin', model_node_id: 'model-qwen', input_mode: 'archive', display_name: 'embedded-gateway-demo.zip', status: 'completed', progress: 100, duration_ms: 12840, file_count: 6, finding_count: 4, report_id: reportId, files: demoFiles(), check_types: ['memory_safety', 'logic', 'portability', 'performance'], created_at: created, updated_at: created, completed_at: created },
      { id: 'review-running', owner_id: 'user-demo', model_node_id: 'model-deepseek', input_mode: 'file', display_name: 'network_driver.c', status: 'running', progress: 62, file_count: 1, finding_count: 0, created_at: created, updated_at: created },
      { id: 'review-failed', owner_id: 'user-demo', model_node_id: 'model-qwen', input_mode: 'archive', display_name: 'legacy-module.zip', status: 'failed', progress: 100, error_message: '模型节点暂时不可用，请稍后重试。', model_log: '[demo] selected model node is unavailable', duration_ms: 3100, file_count: 8, finding_count: 0, created_at: created, updated_at: created, completed_at: created },
    ],
    reports: [makeReport(reportId, taskId)],
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
  id: task.id, owner_id: task.owner_id, model_node_id: task.model_node_id, display_name: task.display_name,
  status: task.status, progress: task.progress, finding_count: task.finding_count, error_message: task.error_message, created_at: task.created_at,
})

export const resetMockState = () => {
  localStorage.removeItem(STATE_KEY)
  localStorage.removeItem(SESSION_KEY)
}

export const mockApi = {
  auth: {
    login: async (username: string, password: string) => {
      const state = load()
      const user = state.users.find((item) => item.username === username)
      const validPassword = username === 'admin' ? 'admin12345678' : username === 'demo' ? 'demo12345678' : ''
      if (!user || !user.is_enabled || password !== validPassword) throw new Error('账号或密码错误')
      localStorage.setItem(SESSION_KEY, username)
      return response({ access_token: `mock-token-${username}` })
    },
    me: async () => response(requireUser(load()) as User),
    password: async () => response({ ok: true }),
  },
  models: async () => {
    const state = load()
    const models = state.models.filter((model) => model.is_enabled).sort((a, b) => Number(b.is_default) - Number(a.is_default))
    return response(requireUser(state).role === 'admin' ? models : models.filter((model) => model.is_default))
  },
  reviews: {
    submitText: async (model_node_id: string, source_text: string, check_types: string[]) => createReview(model_node_id, 'text', 'snippet.c', source_text ? 1 : 0, check_types),
    submitFile: async (mode: 'file' | 'archive', model_node_id: string, file: File, check_types: string[]) => createReview(model_node_id, mode, file.name, mode === 'archive' ? 6 : 1, check_types),
    submitDemoArchive: async (check_types: string[]) => createReview('model-qwen', 'archive', 'embedded-gateway-live-demo.zip', 6, check_types),
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
        const countKey = `${String(params.severity)}_count` as 'high_count' | 'medium_count' | 'low_count' | 'suggestion_count'
        tasks = tasks.filter((task) => {
          const report = state.reports.find((item) => item.id === task.report_id)
          return Boolean(report?.[countKey])
        })
      }
      const sortBy = String(params?.sort_by || 'created_at')
      const sortDir = params?.sort_dir === 'asc' ? 1 : -1
      const modelName = (task: ReviewTask) => state.models.find((model) => model.id === task.model_node_id)?.display_name || task.model_node_id
      const testerName = (task: ReviewTask) => state.users.find((user) => user.id === task.owner_id)?.username || task.owner_id
      const value = (task: ReviewTask) => sortBy === 'tester_name' ? testerName(task) : sortBy === 'model' ? modelName(task) : task[sortBy as keyof ReviewTask] ?? ''
      tasks.sort((left, right) => String(value(left)).localeCompare(String(value(right)), 'zh-CN', { numeric: true }) * sortDir)
      const total = tasks.length
      const offset = Number(params?.offset || 0), limit = Number(params?.limit || 20)
      return response({ items: tasks.slice(offset, offset + limit).map((task) => ({ ...task, tester_name: testerName(task) })), total })
    },
    get: async (taskId: string) => {
      const state = load()
      const task = visibleTasks(state).find((item) => item.id === taskId)
      if (!task) throw new Error('审查任务不存在')
      if (task.status === 'queued' || task.status === 'running') {
        const polls = (state.polls[task.id] || 0) + 1
        state.polls[task.id] = polls
        Object.assign(task, polls >= 3
          ? { status: 'completed' as TaskStatus, progress: 100, duration_ms: 2860, finding_count: 4, report_id: `report-${task.id}`, completed_at: now() }
          : { status: 'running' as TaskStatus, progress: polls === 1 ? 34 : 68 })
        task.updated_at = now()
        save(state)
      }
      return response(task)
    },
    remove: async (taskId: string) => {
      const state = load()
      state.tasks = state.tasks.filter((task) => task.id !== taskId)
      state.reports = state.reports.filter((report) => report.task_id !== taskId)
      save(state)
      return response({ ok: true })
    },
  },
  reports: {
    get: async (reportId: string) => {
      const report = load().reports.find((item) => item.id === reportId)
      if (!report) throw new Error('审查报告不存在')
      return response(report)
    },
    download: async (reportId: string, format: 'markdown' | 'pdf') => {
      const report = load().reports.find((item) => item.id === reportId)
      if (!report) throw new Error('审查报告不存在')
      return response(new Blob([format === 'markdown' ? `# C-Check 审查报告\n\n${report.summary}` : 'C-Check demo PDF report'], { type: format === 'markdown' ? 'text/markdown' : 'application/pdf' }))
    },
  },
  admin: {
    dashboard: async () => {
      const state = load()
      return response({
        users: state.users.length, enabled_users: state.users.filter((user) => user.is_enabled).length,
        models: state.models.length, enabled_models: state.models.filter((model) => model.is_enabled).length,
        tasks: state.tasks.length, queued_tasks: state.tasks.filter((task) => task.status === 'queued').length,
        running_tasks: state.tasks.filter((task) => task.status === 'running').length,
        completed_tasks: state.tasks.filter((task) => task.status === 'completed').length,
        failed_tasks: state.tasks.filter((task) => task.status === 'failed').length,
      } satisfies Dashboard)
    },
    resources: async () => {
      const state = load()
      const tasks = {
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
      return response({
        captured_at: now(),
        system: {
          cpu_percent: 28,
          load_average_1m: 1.35,
          memory_total_bytes: 64 * 1024 ** 3,
          memory_used_bytes: 31 * 1024 ** 3,
          memory_percent: 48.4,
          disk_total_bytes: 500 * 1024 ** 3,
          disk_used_bytes: 182 * 1024 ** 3,
          disk_percent: 36.4,
        },
        gpus: [
          { index: 0, name: 'NVIDIA A100', utilization_percent: 62, memory_used_mb: 24576, memory_total_mb: 81920, memory_percent: 30, temperature_c: 58, power_w: 220 },
        ],
        models: state.models.map((model) => ({
          node_id: model.id,
          display_name: model.display_name,
          base_url: model.base_url,
          metrics_available: model.is_enabled,
          prompt_throughput_tps: model.is_enabled ? 1200 : null,
          generation_throughput_tps: model.is_enabled ? 150 : null,
          running_requests: model.is_enabled ? 2 : 0,
          pending_requests: 0,
          gpu_kv_cache_usage_percent: model.is_enabled ? 27.4 : null,
        })),
        tasks,
      } satisfies ResourceSnapshot)
    },
    users: async () => response(load().users),
    createUser: async (payload: { username: string; password: string; role: string }) => {
      const state = load()
      state.users.push({ id: id('user'), username: payload.username, role: payload.role === 'admin' ? 'admin' : 'user', is_enabled: true, created_at: now() })
      save(state)
      return response({ ok: true })
    },
    enableUser: async (userId: string, is_enabled: boolean) => update(state => state.users.find(user => user.id === userId)!.is_enabled = is_enabled),
    resetPassword: async () => response({ ok: true }),
    models: async () => response(load().models),
    saveModel: async (payload: Partial<ModelNode> & { display_name: string; model_identifier: string; base_url: string }, modelId?: string) => {
      const state = load()
      if (modelId) Object.assign(state.models.find((model) => model.id === modelId)!, payload)
      else state.models.push({ id: id('model'), timeout_seconds: 120, is_enabled: true, is_default: !state.models.some(model => model.is_default), ...payload })
      save(state)
      return response({ ok: true })
    },
    enableModel: async (modelId: string, is_enabled: boolean) => update(state => state.models.find(model => model.id === modelId)!.is_enabled = is_enabled),
    defaultModel: async (modelId: string) => update(state => state.models.forEach(model => { model.is_default = model.id === modelId })),
    deleteModel: async (modelId: string) => update(state => { state.models = state.models.filter(model => model.id !== modelId) }),
    modelHealth: async () => response({ ok: true }),
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
        status: 'queued',
        progress: 0,
        log: 'Deployment task queued.',
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
        description: '由模型部署任务自动登记',
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
      if (!prompt || prompt.is_active || state.prompts.length <= 1) throw new Error('当前启用版本或最后一个版本不可删除')
      state.prompts = state.prompts.filter(item => item.id !== promptId)
    }),
    tasks: async (status?: TaskStatus | '') => response(load().tasks.filter(task => !status || task.status === status).map(taskToAdmin)),
  },
}

async function createReview(model_node_id: string, input_mode: string, display_name: string, file_count: number, check_types: string[]) {
  const state = load()
  const user = requireUser(state)
  const taskId = id('review')
  const reportId = `report-${taskId}`
  const created = now()
  const files = input_mode === 'archive' ? demoFiles(file_count) : [{ id: 'file-1', relative_path: display_name, size_bytes: 180 }]
  const task: ReviewTask = { id: taskId, owner_id: user.id, model_node_id, input_mode, display_name, status: 'queued', progress: 8, file_count, finding_count: 0, files, check_types, created_at: created, updated_at: created }
  state.tasks.unshift(task)
  state.reports.push(makeReport(reportId, taskId))
  save(state)
  return response(task)
}

async function update(mutator: (state: MockState) => void) {
  const state = load()
  mutator(state)
  save(state)
  return response({ ok: true })
}
