import axios from 'axios'
import type { AdminTask, AdminUser, Dashboard, ModelCatalogItem, ModelDeployment, ModelNode, Prompt, Report, ResourceSnapshot, ReviewTask, ReviewTaskPage, TaskStatus, User } from '../types'
import { mockApi } from './mock'

export const TOKEN_KEY = 'c-check-token'
export const MOCK_API_ENABLED = import.meta.env.DEV && import.meta.env.VITE_USE_MOCK_API !== 'false'
export const api = axios.create({ baseURL: '/api', timeout: 30000 })

api.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})
api.interceptors.response.use(undefined, (error) => {
  if (error.response?.status === 401) {
    localStorage.removeItem(TOKEN_KEY)
    if (!location.pathname.includes('/login')) location.assign('/login')
  }
  return Promise.reject(error)
})

const formatErrorDetail = (detail: unknown) => {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (typeof item === 'string') return item
        if (item && typeof item === 'object' && 'msg' in item) {
          const loc = Array.isArray(item.loc) ? item.loc.join('.') : ''
          return loc ? `${loc}: ${item.msg}` : String(item.msg)
        }
        return ''
      })
      .filter(Boolean)
    return messages.length ? messages.join('；') : '请求参数无效，请检查筛选条件'
  }
  if (detail && typeof detail === 'object') return JSON.stringify(detail)
  return ''
}

export const errorMessage = (error: unknown) => {
  if (axios.isAxiosError(error)) return formatErrorDetail(error.response?.data?.detail) || error.message || '请求失败，请稍后重试'
  return error instanceof Error ? error.message : '请求失败，请稍后重试'
}

const reviewFormData = (modelNodeId: string, checkTypes: string[], displayName?: string) => {
  const body = new FormData()
  body.append('model_node_id', modelNodeId)
  body.append('check_types', JSON.stringify(checkTypes))
  if (displayName) body.append('display_name', displayName)
  return body
}

export const authApi = {
  login: (username: string, password: string) => MOCK_API_ENABLED ? mockApi.auth.login(username, password) : api.post<{ access_token: string }>('/auth/login', { username, password }),
  me: () => MOCK_API_ENABLED ? mockApi.auth.me() : api.get<User>('/auth/me'),
  password: (current_password: string, new_password: string) => MOCK_API_ENABLED ? mockApi.auth.password() : api.post('/auth/password', { current_password, new_password }),
}
export const reviewApi = {
  models: () => MOCK_API_ENABLED ? mockApi.models() : api.get<ModelNode[]>('/models'),
  submitText: (model_node_id: string, source_text: string, check_types: string[], display_name?: string) => MOCK_API_ENABLED ? mockApi.reviews.submitText(model_node_id, source_text, check_types, display_name) : api.post<ReviewTask>('/reviews/text', { model_node_id, source_text, check_types, display_name }),
  submitFile: (mode: 'file' | 'archive', modelNodeId: string, file: File, checkTypes: string[], displayName?: string) => {
    if (MOCK_API_ENABLED) return mockApi.reviews.submitFile(mode, modelNodeId, file, checkTypes, displayName)
    const body = reviewFormData(modelNodeId, checkTypes, displayName)
    body.append('file', file)
    return api.post<ReviewTask>(`/reviews/${mode}`, body)
  },
  submitFolder: (modelNodeId: string, files: File[], checkTypes: string[], displayName?: string) => {
    if (MOCK_API_ENABLED) return mockApi.reviews.submitFile('archive', modelNodeId, files[0], checkTypes, displayName)
    const body = reviewFormData(modelNodeId, checkTypes, displayName)
    files.forEach((file) => body.append('files', file, file.webkitRelativePath || file.name))
    return api.post<ReviewTask>('/reviews/folder', body)
  },
  submitDemoArchive: (checkTypes: string[]) => mockApi.reviews.submitDemoArchive(checkTypes),
  list: (params?: Record<string, unknown>) => MOCK_API_ENABLED ? mockApi.reviews.list(params) : api.get<ReviewTaskPage>('/reviews', { params }),
  get: (id: string) => MOCK_API_ENABLED ? mockApi.reviews.get(id) : api.get<ReviewTask>(`/reviews/${id}`),
  pin: (id: string) => MOCK_API_ENABLED ? mockApi.reviews.pin(id) : api.post<ReviewTask>(`/reviews/${id}/pin`),
  remove: (id: string) => MOCK_API_ENABLED ? mockApi.reviews.remove(id) : api.delete(`/reviews/${id}`),
}
export const reportApi = {
  get: (id: string) => MOCK_API_ENABLED ? mockApi.reports.get(id) : api.get<Report>(`/reports/${id}`),
  download: (id: string, format: 'markdown' | 'pdf') => MOCK_API_ENABLED ? mockApi.reports.download(id, format) : api.get(`/reports/${id}/${format}`, { responseType: 'blob' }),
}
export const adminApi = {
  dashboard: () => MOCK_API_ENABLED ? mockApi.admin.dashboard() : api.get<Dashboard>('/admin/dashboard'),
  resources: () => MOCK_API_ENABLED ? mockApi.admin.resources() : api.get<ResourceSnapshot>('/admin/resources'),
  users: () => MOCK_API_ENABLED ? mockApi.admin.users() : api.get<AdminUser[]>('/admin/users'),
  createUser: (payload: { username: string; password: string; role: string }) => MOCK_API_ENABLED ? mockApi.admin.createUser(payload) : api.post('/admin/users', payload),
  enableUser: (id: string, is_enabled: boolean) => MOCK_API_ENABLED ? mockApi.admin.enableUser(id, is_enabled) : api.patch(`/admin/users/${id}/enabled`, { is_enabled }),
  resetPassword: (id: string, password: string) => MOCK_API_ENABLED ? mockApi.admin.resetPassword() : api.post(`/admin/users/${id}/password`, { password }),
  models: () => MOCK_API_ENABLED ? mockApi.admin.models() : api.get<ModelNode[]>('/admin/models'),
  saveModel: (payload: Partial<ModelNode> & { display_name: string; model_identifier: string; base_url: string }, id?: string) => MOCK_API_ENABLED ? mockApi.admin.saveModel(payload, id) : id ? api.put(`/admin/models/${id}`, payload) : api.post('/admin/models', payload),
  enableModel: (id: string, is_enabled: boolean) => MOCK_API_ENABLED ? mockApi.admin.enableModel(id, is_enabled) : api.patch(`/admin/models/${id}/enabled`, { is_enabled }),
  defaultModel: (id: string) => MOCK_API_ENABLED ? mockApi.admin.defaultModel(id) : api.post(`/admin/models/${id}/default`),
  deleteModel: (id: string) => MOCK_API_ENABLED ? mockApi.admin.deleteModel(id) : api.delete(`/admin/models/${id}`),
  modelHealth: (id: string) => MOCK_API_ENABLED ? mockApi.admin.modelHealth() : api.post(`/models/${id}/health`),
  modelCatalog: () => MOCK_API_ENABLED ? mockApi.admin.modelCatalog() : api.get<ModelCatalogItem[]>('/admin/model-catalog'),
  modelDeployments: () => MOCK_API_ENABLED ? mockApi.admin.modelDeployments() : api.get<ModelDeployment[]>('/admin/model-deployments'),
  createModelDeployment: (payload: Record<string, unknown>) => MOCK_API_ENABLED ? mockApi.admin.createModelDeployment(payload) : api.post<ModelDeployment>('/admin/model-deployments', payload),
  prompts: () => MOCK_API_ENABLED ? mockApi.admin.prompts() : api.get<Prompt[]>('/admin/prompts'),
  createPrompt: (body: string) => MOCK_API_ENABLED ? mockApi.admin.createPrompt(body) : api.post('/admin/prompts', { body }),
  updatePrompt: (id: string, body: string) => MOCK_API_ENABLED ? mockApi.admin.updatePrompt(id, body) : api.put(`/admin/prompts/${id}`, { body }),
  deletePrompt: (id: string) => MOCK_API_ENABLED ? mockApi.admin.deletePrompt(id) : api.delete(`/admin/prompts/${id}`),
  activatePrompt: (id: string) => MOCK_API_ENABLED ? mockApi.admin.activatePrompt(id) : api.post(`/admin/prompts/${id}/activate`),
  tasks: (status?: TaskStatus | '') => MOCK_API_ENABLED ? mockApi.admin.tasks(status) : api.get<AdminTask[]>('/admin/tasks', { params: { status: status || undefined } }),
  pinTask: (id: string) => MOCK_API_ENABLED ? mockApi.reviews.pin(id) : api.post<AdminTask>(`/reviews/${id}/pin`),
  removeTask: (id: string) => MOCK_API_ENABLED ? mockApi.reviews.remove(id) : api.delete(`/reviews/${id}`),
}
