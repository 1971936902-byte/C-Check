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

  it('rejects wrong passwords and disabled users', async () => {
    await expect(mockApi.auth.login('admin', 'wrong-password')).rejects.toThrow('账号或密码错误')
    await expect(mockApi.auth.login('disabled_user', 'disabled12345678')).rejects.toThrow('账号或密码错误')
  })

  it('changes passwords and requires the updated credential on the next login', async () => {
    await mockApi.auth.password('admin12345678', 'adminChanged1234')

    await expect(mockApi.auth.login('admin', 'admin12345678')).rejects.toThrow('账号或密码错误')
    await expect(mockApi.auth.login('admin', 'adminChanged1234')).resolves.toMatchObject({
      data: { access_token: 'mock-token-admin' },
    })
  })

  it('moves a submitted review through queued, running, and completed states', async () => {
    const created = await mockApi.reviews.submitText('model-qwen', 'int main(void) { return 0; }', ['memory_safety', 'logic'])
    expect(created.data.status).toBe('queued')
    expect(created.data.check_types).toEqual(['memory_safety', 'logic'])
    expect(created.data.files?.map((file) => file.relative_path)).toEqual(['snippet.c'])
    await mockApi.reviews.get(created.data.id)
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

  it('prevents normal users from reading or deleting other users tasks', async () => {
    await mockApi.auth.login('demo', 'demo12345678')

    await expect(mockApi.reviews.get('review-seeded')).rejects.toThrow('审查任务不存在')
    await expect(mockApi.reviews.remove('review-seeded')).rejects.toThrow('审查任务不存在')
  })

  it('returns first-stage review shaped findings in reports', async () => {
    const report = (await mockApi.reports.get('report-seeded')).data
    const finding = report.result_json.findings[0]
    const allFindings = report.result_json.findings
    expect(finding).toEqual({
      severity: expect.any(String),
      category: expect.any(String),
      title: expect.any(String),
      description: expect.any(String),
      file_path: expect.any(String),
      line: expect.any(Number),
    })
    expect(finding.code_snippet).toBeUndefined()
    expect(finding.fixed_snippet).toBeUndefined()
    expect(finding.remediation).toBeUndefined()
    expect(allFindings.every((item) => item.description && item.file_path && item.line)).toBe(true)
    expect(allFindings.some((item) => item.category === 'other')).toBe(true)
  })

  it('filters review history by severity and creation time', async () => {
    expect((await mockApi.reviews.list({ severity: 'high' })).data.items.map((task) => task.id)).toContain('review-seeded')
    expect((await mockApi.reviews.list({ start_time: '2999-01-01T00:00:00.000Z' })).data.items).toHaveLength(0)
  })

  it('paginates and sorts review history for history table coverage', async () => {
    const firstPage = await mockApi.reviews.list({ offset: 0, limit: 2, sort_by: 'created_at', sort_dir: 'desc' })
    const secondPage = await mockApi.reviews.list({ offset: 2, limit: 2, sort_by: 'created_at', sort_dir: 'desc' })

    expect(firstPage.data.items).toHaveLength(2)
    expect(secondPage.data.items).toHaveLength(2)
    expect(firstPage.data.items.map((task) => task.id)).not.toEqual(secondPage.data.items.map((task) => task.id))
    expect(firstPage.data.total).toBeGreaterThan(4)
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

  it('allows admins to register a user who can log in with the initial password', async () => {
    await mockApi.admin.createUser({ username: 'new_reviewer', password: 'newReviewer1234', role: 'user' })

    await mockApi.auth.login('new_reviewer', 'newReviewer1234')

    expect((await mockApi.auth.me()).data).toMatchObject({ username: 'new_reviewer', role: 'user' })
  })

  it('prevents duplicate users and applies enable disable state to login', async () => {
    await expect(mockApi.admin.createUser({ username: 'demo', password: 'demo12345678', role: 'user' })).rejects.toThrow('用户名已存在')
    await mockApi.admin.enableUser('user-demo', false)
    await expect(mockApi.auth.login('demo', 'demo12345678')).rejects.toThrow('账号或密码错误')

    await mockApi.admin.enableUser('user-demo', true)
    await expect(mockApi.auth.login('demo', 'demo12345678')).resolves.toMatchObject({
      data: { access_token: 'mock-token-demo' },
    })
  })

  it('isolates normal users while admins can see concurrent work from everyone', async () => {
    await mockApi.auth.login('demo', 'demo12345678')
    const demoTask = await mockApi.reviews.submitText('model-mock', 'int demo(void) { return 0; }', ['logic'], 'demo-task.c')
    const demoList = await mockApi.reviews.list({ limit: 100 })
    expect(demoList.data.items.every((task) => task.owner_id === 'user-demo')).toBe(true)

    await mockApi.auth.login('alice', 'alice12345678')
    const aliceTask = await mockApi.reviews.submitText('model-mock', 'int alice(void) { return 0; }', ['memory_safety'], 'alice-task.c')
    const aliceList = await mockApi.reviews.list({ limit: 100 })
    expect(aliceList.data.items.map((task) => task.id)).toContain(aliceTask.data.id)
    expect(aliceList.data.items.map((task) => task.id)).not.toContain(demoTask.data.id)

    await mockApi.auth.login('admin', 'admin12345678')
    const adminList = await mockApi.reviews.list({ limit: 100 })
    expect(adminList.data.items.map((task) => task.id)).toEqual(expect.arrayContaining([demoTask.data.id, aliceTask.data.id]))
  })

  it('keeps queued tasks serial and supports pinning the next task', async () => {
    const first = await mockApi.reviews.submitText('model-mock', 'int a;', ['logic'], 'first.c')
    const second = await mockApi.reviews.submitText('model-mock', 'int b;', ['logic'], 'second.c')

    expect(second.data.status).toBe('queued')
    expect(second.data.queued_ahead_count).toBeGreaterThanOrEqual(1)

    const pinned = await mockApi.reviews.pin(second.data.id)
    expect(pinned.data.queue_priority).toBeTruthy()
    expect(pinned.data.queued_ahead_count).toBe(0)

    await mockApi.reviews.get(first.data.id)
    await mockApi.reviews.get(first.data.id)
    await mockApi.reviews.get(first.data.id)
    const completed = await mockApi.reviews.get(first.data.id)
    expect(completed.data.status).toBe('completed')
  })

  it('replaces the previously pinned queued task when another task is pinned', async () => {
    const first = await mockApi.reviews.submitText('model-mock', 'int first;', ['logic'], 'first-queued.c')
    const second = await mockApi.reviews.submitText('model-mock', 'int second;', ['logic'], 'second-queued.c')

    await mockApi.reviews.pin(first.data.id)
    const replacement = await mockApi.reviews.pin(second.data.id)
    const queued = (await mockApi.reviews.list({ status: 'queued', limit: 100 })).data.items

    expect(replacement.data.queue_priority).toBe(1)
    expect(queued.filter((task) => task.queue_priority).map((task) => task.id)).toEqual([second.data.id])
  })

  it('downloads markdown and pdf reports from mock data', async () => {
    const markdown = await mockApi.reports.download('report-seeded', 'markdown')
    const pdf = await mockApi.reports.download('report-seeded', 'pdf')

    expect(await markdown.data.text()).toContain('C-Check 审查报告')
    expect(await pdf.data.text()).toContain('mock PDF report')
  })

  it('reports missing report download and read errors clearly', async () => {
    await expect(mockApi.reports.get('report-missing')).rejects.toThrow('审查报告不存在')
    await expect(mockApi.reports.download('report-missing', 'markdown')).rejects.toThrow('审查报告不存在')
  })

  it('exposes dual GPU resource data for admin monitoring pages', async () => {
    const resources = (await mockApi.admin.resources()).data

    expect(resources.gpus).toHaveLength(2)
    expect(resources.tasks.running_tasks).toBeGreaterThan(0)
    expect(resources.models.some((model) => model.base_url.startsWith('mock://'))).toBe(true)
  })
})
