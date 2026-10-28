import { api, TOKEN_KEY } from './client'

export interface AssistantState { enabled: boolean; available: boolean; reason: string; operation?: string | null }
export interface Conversation { id: string; title: string; rag_enabled: boolean; status: string; created_at: string }
export interface KnowledgeDocument { id: string; filename: string; size_bytes: number; status: string; error: string | null }
export interface Source { title: string; text?: string; pageContent?: string }
export interface Message { role: 'user' | 'assistant'; content: string; sources?: Source[]; interrupted?: string }
export type AssistantEvent = { event: 'delta'; text: string } | { event: 'sources'; sources: Source[] }
  | { event: 'done' } | { event: 'error'; code: string }

export const assistantApi = {
  status: () => api.get<AssistantState>('/assistant/status'),
  conversations: () => api.get<Conversation[]>('/assistant/conversations', { params: { limit: 100 } }),
  create: (title: string, rag_enabled: boolean) => api.post<Conversation>('/assistant/conversations', { title, rag_enabled }),
  prepare: (id: string) => api.post<Conversation>(`/assistant/conversations/${id}/prepare`),
  remove: (id: string) => api.delete(`/assistant/conversations/${id}`),
  history: (id: string) => api.get<{ messages: Message[] }>(`/assistant/conversations/${id}/messages`),
  documents: () => api.get<KnowledgeDocument[]>('/assistant/documents'),
  retry: (id: string) => api.post(`/assistant/documents/${id}/retry`),
  upload: (files: File[]) => {
    const body = new FormData()
    files.forEach(file => body.append('files', file, file.name))
    return api.post<KnowledgeDocument[]>('/assistant/documents', body, { timeout: 120000 })
  },
}

export async function readAssistantEvents(body: ReadableStream<Uint8Array>, receive: (event: AssistantEvent) => void) {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = '', terminal = false
  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      buffer = buffer.replace(/\r\n/g, '\n')
      let boundary: number
      while ((boundary = buffer.indexOf('\n\n')) !== -1) {
        const block = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const data = block.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
        if (!data) continue
        const event = JSON.parse(data) as AssistantEvent
        if (!event || !['delta', 'sources', 'done', 'error'].includes(event.event)
          || (event.event === 'delta' && typeof event.text !== 'string')
          || (event.event === 'error' && typeof event.code !== 'string')
          || (event.event === 'sources' && !Array.isArray(event.sources))) throw new Error('无效的助手响应')
        receive(event)
        if (event.event === 'done' || event.event === 'error') { terminal = true; return }
      }
      if (buffer.length > 1024 * 1024) throw new Error('助手响应过大')
      if (done) break
    }
    if (!terminal) throw new Error('连接已断开，回答可能不完整')
  } finally {
    await reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }
}

export async function sendAssistantMessage(id: string, message: string, signal: AbortSignal, receive: (event: AssistantEvent) => void) {
  const response = await fetch(`/api/assistant/conversations/${encodeURIComponent(id)}/messages`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${localStorage.getItem(TOKEN_KEY) || ''}` },
    body: JSON.stringify({ message }), signal, credentials: 'same-origin',
  })
  if (response.status === 401) { localStorage.removeItem(TOKEN_KEY); location.assign('/login'); throw new Error('登录已过期') }
  if (!response.ok) {
    const error = await response.json().catch(() => ({}))
    throw new Error(typeof error.detail === 'string' ? error.detail : '消息发送失败')
  }
  if (!response.body) throw new Error('浏览器未收到回答流')
  await readAssistantEvents(response.body, receive)
}

export function assistantReason(reason: string): string {
  return ({ idle: '模型空闲', review_busy: '审查任务优先，助手暂不可用', assistant_busy: '模型正在处理助手请求',
    disabled: 'AI 智能助手尚未启用', maintenance: '系统维护中', scheduler_not_initialized: '助手调度尚未初始化',
    scheduler_recovery_required: '正在等待计算状态确认', review_priority_interrupted: '因审查任务启动而中断',
    'Interrupted by a higher-priority review.': '因审查任务启动而中断',
    assistant_interrupted: '回答在完成前中断', 'Response interrupted before completion.': '回答在完成前中断',
    assistant_service_unavailable: '助手服务暂不可用', assistant_upstream_unavailable: '模型服务暂不可用',
    assistant_recovery_required: '计算状态待确认，请联系管理员', unavailable: '无法确认模型状态，暂不可发送',
  } as Record<string, string>)[reason] || reason
}
