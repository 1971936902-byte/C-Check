import type { Message } from '../api/assistant'

export type GenerationPhase = 'waiting' | 'sources' | 'generating'
export function nearBottom(area: Pick<HTMLElement, 'scrollHeight' | 'scrollTop' | 'clientHeight'>): boolean {
  return area.scrollHeight - area.scrollTop - area.clientHeight <= 48
}
export function questionForAnswer(messages: Message[], index: number): string {
  if (index < 0 || index >= messages.length) return ''
  for (let i = index; i >= 0; i--) if (messages[i]?.role === 'user') return messages[i]!.content
  return ''
}
export function generationLabel(phase: GenerationPhase, rag: boolean): string {
  return phase === 'generating' ? '正在生成回答' : phase === 'sources' ? '已获取引用，等待模型响应'
    : rag ? '等待知识检索与模型响应' : '等待模型响应'
}
