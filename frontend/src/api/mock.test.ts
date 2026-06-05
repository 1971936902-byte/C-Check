import { beforeEach, describe, expect, it } from 'vitest'
import { mockApi, resetMockState } from './mock'

class MemoryStorage implements Storage {
  private values = new Map<string, string>()
  get length() { return this.values.size }
  clear() { this.values.clear() }
  getItem(key: string) { return this.values.get(key) ?? null }
  key(index: number) { return [...this.values.keys()][index] ?? null }
  removeItem(key: string) { this.values.delete(key) }
  setItem(key: string, value: string) { this.values.set(key, value) }
}

Object.defineProperty(globalThis, 'localStorage', { value: new MemoryStorage() })

describe('mockApi', () => {
  beforeEach(async () => {
    resetMockState()
    await mockApi.auth.login('admin', 'admin12345678')
  })

  it('logs in with the seeded admin account and returns the current user', async () => {
    const login = await mockApi.auth.login('admin', 'admin12345678')
    expect(login.data.access_token).toContain('mock-token-admin')
    expect((await mockApi.auth.me()).data).toMatchObject({ username: 'admin', role: 'admin' })
  })

  it('moves a submitted review through queued, running, and completed states', async () => {
    const created = await mockApi.reviews.submitText('model-qwen', 'int main(void) { return 0; }', ['memory_safety', 'logic'])
    expect(created.data.status).toBe('queued')
    expect(created.data.check_types).toEqual(['memory_safety', 'logic'])
    expect(created.data.files?.map((file) => file.relative_path)).toEqual(['snippet.c'])
    expect((await mockApi.reviews.get(created.data.id)).data.status).toBe('running')
    await mockApi.reviews.get(created.data.id)
    const completed = await mockApi.reviews.get(created.data.id)
    expect(completed.data.status).toBe('completed')
    expect(completed.data.report_id).toBeTruthy()
  })

  it('creates a multi-file demo archive for task progress testing', async () => {
    const created = await mockApi.reviews.submitDemoArchive(['memory_safety'])
    expect(created.data.files).toHaveLength(6)
    expect(created.data.check_types).toEqual(['memory_safety'])
  })

  it('removes a review from history', async () => {
    const reviews = await mockApi.reviews.list()
    await mockApi.reviews.remove(reviews.data.items[0].id)
    expect((await mockApi.reviews.list()).data.total).toBe(reviews.data.total - 1)
  })

  it('includes Git-style source and fixed code snippets in reports', async () => {
    const report = (await mockApi.reports.get('report-seeded')).data
    const finding = report.result_json.findings[0]
    expect(finding.code_snippet?.some((line) => line.kind === 'removed')).toBe(true)
    expect(finding.fixed_snippet?.some((line) => line.kind === 'added')).toBe(true)
  })

  it('filters review history by severity and creation time', async () => {
    expect((await mockApi.reviews.list({ severity: 'high' })).data.items.map((task) => task.id)).toEqual(['review-seeded'])
    expect((await mockApi.reviews.list({ start_time: '2999-01-01T00:00:00.000Z' })).data.items).toHaveLength(0)
  })

  it('exposes model catalog and creates deployment records', async () => {
    const catalog = (await mockApi.admin.modelCatalog()).data
    expect(catalog.map((model) => model.key)).toContain('starcoder2-15b')

    const deployment = await mockApi.admin.createModelDeployment({
      catalog_key: 'starcoder2-15b',
      source: 'huggingface',
      base_url: 'http://127.0.0.1:8103',
    })

    expect(deployment.data.display_name).toBe('StarCoder2 15B')
    expect((await mockApi.admin.modelDeployments()).data[0].id).toBe(deployment.data.id)
    expect((await mockApi.admin.models()).data.some((model) => model.model_identifier === 'starcoder2-15b')).toBe(true)
  })
})
