import { describe, expect, it } from 'vitest'
import { nearBottom, questionForAnswer, generationLabel } from './assistant-interactions'

describe('assistant interaction decisions', () => {
  it('only follows output when the reader is near the bottom', () => {
    expect(nearBottom({ scrollHeight: 1000, clientHeight: 400, scrollTop: 580 })).toBe(true)
    expect(nearBottom({ scrollHeight: 1000, clientHeight: 400, scrollTop: 100 })).toBe(false)
  })
  it('replays the paired question, not the latest draft or an assistant answer', () => {
    const messages = [{ role: 'user' as const, content: 'first' }, { role: 'assistant' as const, content: 'answer' }, { role: 'user' as const, content: 'second' }]
    expect(questionForAnswer(messages, 1)).toBe('first')
    expect(questionForAnswer(messages, 2)).toBe('second')
    expect(questionForAnswer(messages, -1)).toBe('')
    expect(questionForAnswer(messages, 99)).toBe('')
    expect(questionForAnswer([{ role: 'assistant', content: 'no question' }], 0)).toBe('')
  })
  it('does not invent retrieval completion while waiting for upstream output', () => {
    expect(generationLabel('waiting', true)).toBe('等待知识检索与模型响应')
    expect(generationLabel('waiting', false)).toBe('等待模型响应')
    expect(generationLabel('sources', true)).toBe('已获取引用，等待模型响应')
    expect(generationLabel('generating', true)).toBe('正在生成回答')
  })
})
