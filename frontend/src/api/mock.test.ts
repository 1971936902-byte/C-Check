import { beforeEach, describe, expect, it } from 'vitest'
import { reportApi, reviewApi } from './client'
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
    expect((await mockApi.reviews.get(created.data.id)).data.status).toBe('queued')
    expect((await mockApi.reviews.get(created.data.id)).data.status).toBe('running')
    await mockApi.reviews.get(created.data.id)
    const completed = await mockApi.reviews.get(created.data.id)
    expect(completed.data.status).toBe('completed')
    expect(completed.data.report_id).toBeTruthy()
  })

  it('creates a multi-file demo archive for task progress testing', async () => {
    const created = await mockApi.reviews.submitDemoArchive(['memory_safety'])
    expect(created.data.files).toHaveLength(12)
    expect(created.data.check_types).toEqual(['memory_safety'])
  })

  it('simulates text, single file, zip, and folder inputs through the frontend client', async () => {
    const cFile = new File(['int main(void) { return 0; }\n'], 'main.c', { type: 'text/x-csrc' })
    const headerFile = new File(['#pragma once\nint value(void);\n'], 'include/value.h', { type: 'text/x-chdr' })
    const zipFile = new File(['PK mock archive bytes'], 'firmware.zip', { type: 'application/zip' })
    const folderFiles = [
      new File(['int app(void) { return 1; }\n'], 'app.c'),
      new File(['int driver(void) { return 2; }\n'], 'driver.c'),
      headerFile,
    ]

    const textTask = await reviewApi.submitText('model-qwen', 'int snippet(void) { return 0; }', ['logic'], 'snippet-input.c')
    const fileTask = await reviewApi.submitFile('file', 'model-qwen', cFile, ['memory_safety'], 'single-file.c')
    const zipTask = await reviewApi.submitFile('archive', 'model-qwen', zipFile, ['compatibility'], 'firmware.zip')
    const folderTask = await reviewApi.submitFolder('model-qwen', folderFiles, ['logic', 'maintainability'], 'firmware-folder')

    expect(textTask.data).toMatchObject({ input_mode: 'text', file_count: 1, display_name: 'snippet-input.c' })
    expect(fileTask.data).toMatchObject({ input_mode: 'file', file_count: 1, display_name: 'single-file.c' })
    expect(zipTask.data).toMatchObject({ input_mode: 'archive', file_count: 12, display_name: 'firmware.zip' })
    expect(folderTask.data).toMatchObject({ input_mode: 'folder', file_count: 3, display_name: 'firmware-folder' })
    expect(folderTask.data.files).toHaveLength(3)
  })

  it('handles burst submissions and still produces downloadable reports', async () => {
    const submissions = await Promise.all([
      ...Array.from({ length: 8 }, (_, index) => reviewApi.submitText('model-mock', `int t${index}(void){return ${index};}`, ['logic'], `stress-text-${index}.c`)),
      ...Array.from({ length: 4 }, (_, index) => reviewApi.submitFile('file', 'model-mock', new File([`int f${index};`], `stress-${index}.c`), ['memory_safety'])),
      ...Array.from({ length: 4 }, (_, index) => reviewApi.submitFile('archive', 'model-mock', new File(['PK'], `stress-${index}.zip`), ['compatibility'])),
      ...Array.from({ length: 4 }, (_, index) => reviewApi.submitFolder('model-mock', [
        new File([`int a${index};`], `folder-${index}/a.c`),
        new File([`int b${index};`], `folder-${index}/b.c`),
        new File([`int h${index};`], `folder-${index}/b.h`),
      ], ['maintainability'], `stress-folder-${index}`)),
    ])

    expect(submissions).toHaveLength(20)
    expect(submissions.map((item) => item.data.input_mode)).toEqual(expect.arrayContaining(['text', 'file', 'archive', 'folder']))
    expect(submissions.filter((item) => item.data.status === 'queued').length).toBeGreaterThan(0)

    const first = submissions[0].data
    let current = first
    for (let i = 0; i < 4 && current.status !== 'completed'; i += 1) {
      current = (await reviewApi.get(first.id)).data
    }

    expect(current.status).toBe('completed')
    expect(current.report_id).toBeTruthy()
    const markdown = await reportApi.download(current.report_id!, 'markdown')
    expect(await markdown.data.text()).toContain(current.display_name)
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
    expect((await mockApi.reviews.list({ severity: 'high' })).data.items.map((task) => task.id)).toEqual(['review-demo-completed', 'review-seeded'])
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
