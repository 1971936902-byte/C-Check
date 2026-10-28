import { describe, expect, it, vi } from 'vitest'
vi.mock('./client', () => ({ api: {}, TOKEN_KEY: 'test-token' }))
import { readAssistantEvents, type AssistantEvent } from './assistant'

function stream(text: string, byteByByte = false) {
  const bytes = new TextEncoder().encode(text)
  return new ReadableStream<Uint8Array>({ start(controller) {
    if (byteByByte) bytes.forEach(byte => controller.enqueue(new Uint8Array([byte])))
    else controller.enqueue(bytes)
    controller.close()
  } })
}
describe('assistant event stream', () => {
  it('handles split UTF-8 and CRLF boundaries without losing text', async () => {
    const events: AssistantEvent[] = []
    await readAssistantEvents(stream('data: {"event":"delta","text":"中文"}\r\n\r\ndata: {"event":"done"}\r\n\r\n', true), event => events.push(event))
    expect(events).toEqual([{ event: 'delta', text: '中文' }, { event: 'done' }])
  })
  it('does not label an interrupted answer as complete', async () => {
    const events: AssistantEvent[] = []
    await readAssistantEvents(stream('data: {"event":"error","code":"review_priority_interrupted"}\n\ndata: {"event":"done"}\n\n'), event => events.push(event))
    expect(events).toEqual([{ event: 'error', code: 'review_priority_interrupted' }])
  })
  it('reports a connection ending without a terminal event', async () => {
    await expect(readAssistantEvents(stream('data: {"event":"delta","text":"partial"}\n\n'), () => {})).rejects.toThrow('不完整')
  })
  it('rejects malformed event shapes', async () => {
    await expect(readAssistantEvents(stream('data: {"event":"delta"}\n\n'), () => {})).rejects.toThrow('无效')
  })
})
