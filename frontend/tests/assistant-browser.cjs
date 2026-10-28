const fs = require('node:fs')
const path = require('node:path')
const http = require('node:http')
const assert = require('node:assert/strict')
const { chromium } = require(process.argv[2] || 'playwright')
const dist = path.resolve(__dirname, '../dist')
const output = path.resolve(process.argv[3] || 'browser-results')
fs.mkdirSync(output, { recursive: true })
const server = http.createServer((req, res) => {
  let file = path.join(dist, decodeURIComponent(new URL(req.url, 'http://localhost').pathname))
  if (!file.startsWith(dist + path.sep)) { res.writeHead(403); return res.end() }
  if (!fs.existsSync(file) || !fs.statSync(file).isFile()) file = path.join(dist, 'index.html')
  res.setHeader('content-type', ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' })[path.extname(file)] || 'application/octet-stream')
  fs.createReadStream(file).pipe(res)
})

;(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const browser = await chromium.launch({ headless: true })
  const results = []
  try {
    for (const width of [1440, 768, 390]) {
      const context = await browser.newContext({ viewport: { width, height: 980 } })
      await context.addInitScript(() => localStorage.setItem('c-check-token', 'browser-fixture'))
      let busy = false, interrupt = false, uploaded = false
      const threads = [{ id: 'conversation-1', title: '无线通信模块分析', rag_enabled: true, status: 'ready', created_at: '2026-10-27' }]
      const files = [{ id: 'file-1', filename: 'E5_Wireless_Protocol_接口规范与异常处理说明_20261027.txt', size_bytes: 30241, status: 'ready', error: null }]
      await context.route('**/terminal-config.json', route => route.fulfill({ json: {} }))
      await context.route('**/api/**', async route => {
        const request = route.request(), url = new URL(request.url())
        if (url.pathname === '/api/auth/me') return route.fulfill({ json: { id: 'ui-user', username: '工程师', role: 'admin', is_enabled: true } })
        if (url.pathname === '/api/assistant/status') return route.fulfill({ json: { enabled: true, available: !busy, reason: busy ? 'review_busy' : 'idle' } })
        if (url.pathname === '/api/assistant/conversations') {
          if (request.method() === 'POST') threads.push({ id: 'conversation-2', ...request.postDataJSON(), status: 'ready', created_at: '2026-10-27' })
          return route.fulfill({ json: request.method() === 'POST' ? threads.at(-1) : threads })
        }
        if (url.pathname.endsWith('/messages')) {
          if (request.method() === 'GET') return route.fulfill({ json: { messages: [
            { role: 'user', content: '请结合知识文件解释接收超时处理。' },
            { role: 'assistant', content: '接收超时应区分数据不完整和通信中断，并保留重试次数及错误状态。\n\nint retry_count = 0;', sources: [{ title: '通信协议说明.txt', text: '收到完整数据帧后才更新处理状态。' }] },
          ] } })
          const events = interrupt ? [{ event: 'delta', text: '分析过程中' }, { event: 'error', code: 'review_priority_interrupted' }]
            : [{ event: 'delta', text: '已收到你的问题。' }, { event: 'done' }]
          return route.fulfill({ contentType: 'text/event-stream', body: events.map(e => 'data: ' + JSON.stringify(e) + '\n\n').join('') })
        }
        if (url.pathname === '/api/assistant/documents') {
          if (request.method() === 'POST') { uploaded = true; assert(request.postData().includes('batch-one.txt')); assert(request.postData().includes('batch-two.md')) }
          return route.fulfill({ json: files })
        }
        return route.fulfill({ status: 404, json: { detail: 'unknown fixture endpoint' } })
      })
      const page = await context.newPage(), errors = []
      page.on('pageerror', e => errors.push(e.message))
      await page.goto(`http://127.0.0.1:${server.address().port}/assistant?conversation=conversation-1`)
      await page.getByText('接收超时应区分', { exact: false }).waitFor()
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
      await page.screenshot({ path: path.join(output, `assistant-chat-${width}.png`), fullPage: true })
      await page.getByLabel('消息', { exact: true }).fill('继续分析')
      await page.getByRole('button', { name: '发送', exact: true }).click()
      await page.getByText('已收到你的问题。', { exact: true }).waitFor()
      busy = true
      await page.waitForFunction(() => document.querySelector('.availability')?.textContent.includes('审查任务优先'))
      assert(await page.getByRole('button', { name: '发送', exact: true }).isDisabled())
      assert(await page.getByLabel('消息', { exact: true }).isDisabled())
      await page.screenshot({ path: path.join(output, `assistant-busy-${width}.png`), fullPage: true })
      busy = false
      await page.waitForFunction(() => document.querySelector('.availability')?.textContent.includes('模型空闲'))
      interrupt = true
      await page.getByLabel('消息', { exact: true }).fill('再分析一次')
      await page.getByRole('button', { name: '发送', exact: true }).click()
      await page.getByText('因审查任务启动而中断', { exact: true }).waitFor()
      await page.locator('label.el-radio-button').filter({ hasText: '知识文件' }).click()
      assert(await page.getByRole('radio', { name: '知识文件', exact: true }).isChecked())
      await page.locator('input[type=file]').setInputFiles([
        { name: 'batch-one.txt', mimeType: 'text/plain', buffer: Buffer.from('one') },
        { name: 'batch-two.md', mimeType: 'text/markdown', buffer: Buffer.from('two') },
      ])
      await page.waitForTimeout(200)
      assert(uploaded)
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
      await page.screenshot({ path: path.join(output, `assistant-files-${width}.png`), fullPage: true })
      assert.deepEqual(errors, [])
      results.push({ width, overflow: false, busyDisabled: true, interruptionShown: true, batchUpload: true })
      await context.close()
    }
    fs.writeFileSync(path.join(output, 'verification.json'), JSON.stringify({ mockedApi: true, results }, null, 2))
    console.log(JSON.stringify(results))
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)) }
})().catch(error => { console.error(error); process.exitCode = 1; server.close() })
