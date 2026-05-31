import axios from 'axios'
import type { AdminTask, AdminUser, Dashboard, ModelNode, Prompt, Report, ReviewTask, TaskStatus, User } from '../types'

export const TOKEN_KEY = 'c-check-token'
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

export const errorMessage = (error: unknown) => {
  if (axios.isAxiosError(error)) return error.response?.data?.detail || error.message
  return error instanceof Error ? error.message : '请求失败，请稍后重试'
}
export const authApi = {
  login: (username: string, password: string) => api.post<{ access_token: string }>('/auth/login', { username, password }),
  me: () => api.get<User>('/auth/me'),
  password: (current_password: string, new_password: string) => api.post('/auth/password', { current_password, new_password }),
}
export const reviewApi = {
  models: () => api.get<ModelNode[]>('/models'),
  submitText: (model_node_id: string, source_text: string) => api.post<ReviewTask>('/reviews/text', { model_node_id, source_text }),
  submitFile: (mode: 'file' | 'archive', modelNodeId: string, file: File) => {
    const body = new FormData(); body.append('model_node_id', modelNodeId); body.append('file', file)
    return api.post<ReviewTask>(`/reviews/${mode}`, body)
  },
  list: (params?: Record<string, unknown>) => api.get<ReviewTask[]>('/reviews', { params }),
  get: (id: string) => api.get<ReviewTask>(`/reviews/${id}`),
  remove: (id: string) => api.delete(`/reviews/${id}`),
}
export const reportApi = {
  get: (id: string) => api.get<Report>(`/reports/${id}`),
  download: (id: string, format: 'markdown' | 'pdf') => api.get(`/reports/${id}/${format}`, { responseType: 'blob' }),
}
export const adminApi = {
  dashboard: () => api.get<Dashboard>('/admin/dashboard'),
  users: () => api.get<AdminUser[]>('/admin/users'),
  createUser: (payload: { username: string; password: string; role: string }) => api.post('/admin/users', payload),
  enableUser: (id: string, is_enabled: boolean) => api.patch(`/admin/users/${id}/enabled`, { is_enabled }),
  resetPassword: (id: string, password: string) => api.post(`/admin/users/${id}/password`, { password }),
  models: () => api.get<ModelNode[]>('/admin/models'),
  saveModel: (payload: Partial<ModelNode> & { display_name: string; model_identifier: string; base_url: string }, id?: string) => id ? api.put(`/admin/models/${id}`, payload) : api.post('/admin/models', payload),
  enableModel: (id: string, is_enabled: boolean) => api.patch(`/admin/models/${id}/enabled`, { is_enabled }),
  deleteModel: (id: string) => api.delete(`/admin/models/${id}`),
  modelHealth: (id: string) => api.post(`/models/${id}/health`),
  prompts: () => api.get<Prompt[]>('/admin/prompts'),
  createPrompt: (body: string) => api.post('/admin/prompts', { body }),
  activatePrompt: (id: string) => api.post(`/admin/prompts/${id}/activate`),
  tasks: (status?: TaskStatus | '') => api.get<AdminTask[]>('/admin/tasks', { params: { status: status || undefined } }),
}
