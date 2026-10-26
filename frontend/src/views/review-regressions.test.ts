import { describe, expect, it } from 'vitest'
import { createLatestRequest } from './latest-request'
import { modelFormPayload, modelFormValues } from './model-form'
import { graphTarget } from './graph-files'
import { decodeSourceBytes } from './source-decoding'
import type { CodeIndexEdge, ModelNode } from '../types'

describe('review UI regressions', () => {
  it('ignores old responses and responses after disposal', async () => {
    const requests = createLatestRequest()
    const first = requests.begin()
    const second = requests.begin()
    let value = ''
    await Promise.resolve().then(() => { if (second()) value = 'new' })
    await Promise.resolve().then(() => { if (first()) value = 'old' })
    expect(value).toBe('new')
    requests.dispose()
    expect(second()).toBe(false)
    expect(requests.begin()()).toBe(false)
  })

  it('does not copy hidden GPU or node identity fields into new model requests', () => {
    const row = { id: 'old', gpu_indices: [3], tensor_parallel_size: 2, display_name: 'old' } as ModelNode
    const form = modelFormValues(row)
    Object.assign(form, modelFormValues())
    expect(modelFormPayload(form, false)).not.toHaveProperty('gpu_indices')
    expect(modelFormPayload(form, false)).not.toHaveProperty('id')
    expect(modelFormPayload(form, false).api_key).toBeUndefined()
    expect(modelFormPayload(form, true).api_key).toBeNull()
  })

  it('resolves include edges using real file IDs without guessing ambiguous basenames', () => {
    const files = [{ id: 'c-id', file_path: 'src/main.c' }, { id: 'h-id', file_path: 'include/main.h' }]
    const edge: CodeIndexEdge = { id: 'edge', confidence: 1, source_id: 'c-id', target_id: null, edge_type: 'FILE_INCLUDES_FILE', metadata: { include: '../include/main.h' } }
    expect(graphTarget(edge, files)).toBe('h-id')
    expect(graphTarget({ ...edge, metadata: { include: 'main.h' } }, files)).toBeUndefined()
    expect(graphTarget({ ...edge, target_id: 'resolved' }, files)).toBe('resolved')
  })

  it('decodes UTF-8, GBK and UTF-16 source previews', () => {
    expect(decodeSourceBytes(new TextEncoder().encode('// 中文').buffer)).toBe('// 中文')
    expect(decodeSourceBytes(new Uint8Array([0xd6, 0xd0, 0xce, 0xc4]).buffer)).toBe('中文')
    expect(decodeSourceBytes(new Uint8Array([0xff, 0xfe, 0x2d, 0x4e, 0x87, 0x65]).buffer)).toBe('中文')
  })
})
