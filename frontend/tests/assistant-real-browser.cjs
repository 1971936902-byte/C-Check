const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')
const { chromium } = require(process.argv[2] || 'playwright')
const session = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'))
const base = process.argv[4]
const output = path.resolve(process.argv[5])
const nonce = Date.now().toString(36)
const filePrefix = 'browser-' + nonce + '-'
fs.mkdirSync(output, { recursive: true })

;(async () => {
  const browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  const page = await context.newPage(), errors = []
  page.on('pageerror', error => errors.push(error.message))
  page.setDefaultTimeout(30000)
  const screenshot = async name => {
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
    await page.screenshot({ path: path.join(output, name + '.png'), fullPage: true })
  }
  try {
    await page.goto(base + '/login')
    await page.getByPlaceholder('账号', { exact: true }).fill(session.username)
    await page.getByPlaceholder('密码', { exact: true }).fill(session.password)
    await page.getByRole('button', { name: '登录平台', exact: true }).click()
    await page.waitForURL('**/workspace')
    await page.getByRole('link', { name: 'AI智能助手', exact: true }).click()
    await page.waitForFunction(() => document.querySelector('.availability')?.textContent.includes('模型空闲'))
    await page.locator('label.el-radio-button').filter({ hasText: '知识文件' }).click()
    await page.locator('input[type=file]').setInputFiles([
      { name: filePrefix + 'calibration.txt', mimeType: 'text/plain', buffer: Buffer.from('Project Borealis calibration value is 73.') },
      { name: filePrefix + 'marker.md', mimeType: 'text/markdown', buffer: Buffer.from('Project Borealis verification marker is CEDAR-2048.') },
    ])
    await page.waitForFunction(prefix => [...document.querySelectorAll('.file-list li')]
      .filter(row => row.textContent.includes(prefix)).length === 2 &&
      [...document.querySelectorAll('.file-list li')].filter(row => row.textContent.includes(prefix))
        .every(row => row.textContent.includes('可检索')), filePrefix, { timeout: 90000 })
    await screenshot('assistant-real-files-1440')
    const create = async (title, rag) => {
      await page.getByRole('button', { name: '新建会话', exact: true }).click()
      await page.locator('#assistant-title').fill(title)
      const toggle = page.getByRole('checkbox', { name: '启用知识检索增强', exact: true })
      if (await toggle.isChecked() !== rag) {
        await page.locator('label.el-checkbox').filter({ hasText: '启用知识检索增强' }).click()
      }
      assert.equal(await toggle.isChecked(), rag)
      await page.getByRole('dialog').getByRole('button', { name: '创建', exact: true }).click()
      await page.waitForURL('**/assistant?conversation=*')
      await page.getByRole('heading', { name: title, exact: true }).waitFor()
    }
    const send = async message => {
      await page.getByLabel('消息', { exact: true }).fill(message)
      await page.getByRole('button', { name: '发送', exact: true }).click()
    }
    await create('浏览器知识检索验收 ' + nonce, true)
    await send('According to the uploaded Borealis documents, give the calibration value and verification marker.')
    await page.waitForFunction(() => [...document.querySelectorAll('.message.assistant .message-content')]
      .some(el => el.textContent.includes('73') && el.textContent.includes('CEDAR-2048')), undefined, { timeout: 120000 })
    await page.getByRole('button', { name: '停止生成', exact: true }).waitFor({ state: 'hidden', timeout: 120000 })
    await page.locator('.sources').first().waitFor()
    await screenshot('assistant-real-rag-1440')
    await page.reload()
    await page.getByText('CEDAR-2048', { exact: false }).first().waitFor()
    await create('浏览器审查抢占验收 ' + nonce, false)
    await send('Write 500 numbered C programming tips with explanations.')
    await page.waitForFunction(() => [...document.querySelectorAll('.message.assistant .message-content')]
      .some(el => el.textContent.length > 20 && !el.textContent.includes('正在生成…')))
    const token = await page.evaluate(() => localStorage.getItem('c-check-token'))
    const headers = { Authorization: 'Bearer ' + token }
    const models = await context.request.get(base + '/api/models', { headers })
    assert(models.ok())
    const rows = await models.json()
    const model = rows.find(item => item.is_default) || rows[0]
    assert(model)
    const source = '#include <string.h>\n' + Array.from({ length: 8 }, (_, i) =>
      `void browser_${i}(void) { char buffer[2]; strcpy(buffer, "overflow-${i}"); }`).join('\n')
    const submitted = await context.request.post(base + '/api/reviews/text', { headers, data: {
      model_node_id: model.id, display_name: 'browser-priority.c', check_types: ['buffer_overflow'], source_text: source,
    } })
    assert.equal(submitted.status(), 201)
    const task = await submitted.json()
    await page.getByText('因审查任务启动而中断', { exact: true }).waitFor()
    await page.waitForFunction(() => document.querySelector('.availability')?.textContent.includes('审查任务优先'))
    assert(await page.getByLabel('消息', { exact: true }).isDisabled())
    assert(await page.getByRole('button', { name: '发送', exact: true }).isDisabled())
    await screenshot('assistant-real-busy-1440')
    await page.setViewportSize({ width: 390, height: 980 })
    await screenshot('assistant-real-busy-390')
    await page.reload()
    await page.getByText('因审查任务启动而中断', { exact: true }).waitFor()
    let completed = false
    for (let attempt = 0; attempt < 180; attempt++) {
      const response = await context.request.get(base + '/api/reviews/' + task.id, { headers })
      assert(response.ok())
      const review = await response.json()
      assert.notEqual(review.status, 'failed', review.error_message)
      if (review.status === 'completed') { completed = true; break }
      await new Promise(resolve => setTimeout(resolve, 1000))
    }
    assert(completed, 'review did not finish')
    await page.waitForFunction(() => document.querySelector('.availability')?.textContent.includes('模型空闲'))
    await send('Reply with OK.')
    await page.getByRole('button', { name: '停止生成', exact: true }).waitFor({ state: 'hidden', timeout: 120000 })
    assert((await page.locator('.message.assistant .message-content').last().innerText()).trim())
    for (const width of [1440, 768, 390]) {
      await page.setViewportSize({ width, height: 1000 })
      await screenshot('assistant-real-resumed-' + width)
    }
    await send('Write 500 more numbered C programming tips.')
    await page.waitForFunction(() => {
      const el = [...document.querySelectorAll('.message.assistant .message-content')].at(-1)
      return el && el.textContent.length > 20 && !el.textContent.includes('正在生成…')
    })
    await page.getByRole('button', { name: '停止生成', exact: true }).click()
    await page.getByText('已停止生成', { exact: true }).waitFor()
    let drained = false
    for (let attempt = 0; attempt < 60; attempt++) {
      const response = await context.request.get(base + '/api/assistant/status', { headers })
      assert(response.ok())
      if ((await response.json()).available) { drained = true; break }
      await new Promise(resolve => setTimeout(resolve, 200))
    }
    assert(drained, 'explicit stop did not release compute')
    await page.waitForFunction(() => document.querySelector('.availability')?.textContent.includes('模型空闲'))
    assert.deepEqual(errors, [])
    fs.writeFileSync(path.join(output, 'verification.json'), JSON.stringify({ mockedApi: false,
      login: true, navigation: true, batchUpload: true, rag: true, sources: true, reloadHistory: true,
      realReviewPreemption: true, busyDisabled: true, interruptedHistoryReload: true,
      resumesAfterReview: true, explicitStop: true, widths: [1440, 768, 390], pageErrors: errors }, null, 2))
    console.log('REAL_BROWSER_LOGIN_UPLOAD_RAG_HISTORY_PRIORITY_RESPONSIVE_RESUME_AND_STOP_OK')
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {})
    fs.writeFileSync(path.join(output, 'failure.json'), JSON.stringify({ url: page.url(),
      error: error.message, pageErrors: errors }, null, 2))
    throw error
  } finally { await browser.close() }
})().catch(error => { console.error(error.message); process.exitCode = 1 })
  .finally(() => { if (process.argv[6]) fs.writeFileSync(process.argv[6], 'done') })
