const fs = require('node:fs')
const path = require('node:path')
const http = require('node:http')
const assert = require('node:assert/strict')
const { chromium } = require(process.argv[2] || 'playwright')
const dist = path.resolve(__dirname, '../dist')
const output = path.resolve(process.argv[3] || 'terminal-help-results')
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
      await context.route('**/api/auth/me', route => route.fulfill({ json: {
        id: 'ui-admin', username: '管理员', role: 'admin', is_enabled: true,
      } }))
      await context.route('**/terminal-config.json', route => route.fulfill({ json: { url: 'https://localhost:8803/guacamole/' } }))
      const page = await context.newPage(), errors = []
      page.on('pageerror', error => errors.push(error.message))
      await page.goto(`http://127.0.0.1:${server.address().port}/help`)
      await page.getByRole('tab', { name: '远程终端', exact: true }).click()
      const panel = page.getByRole('tabpanel', { name: '远程终端', exact: true })
      await panel.getByRole('heading', { name: '远程 SSH 终端（管理员）' }).waitFor()
      for (const name of ['terminal-install', 'terminal-start', 'terminal-stop', 'terminal-status', 'terminal-check', 'terminal-configure', 'terminal-logs']) {
        assert((await panel.textContent()).includes(name))
      }
      assert.equal(await panel.getByRole('button', { name: /复制.*命令/ }).count(), 3)
      assert.equal(await page.getByRole('link', { name: '远程终端（新窗口）' }).getAttribute('href'), 'https://localhost:8803/guacamole/')
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
      await page.screenshot({ path: path.join(output, `terminal-help-${width}.png`), fullPage: true })
      assert.deepEqual(errors, [])
      results.push({ width, overflow: false, commands: 7, independentHelpTab: true })
      await context.close()
    }
    fs.writeFileSync(path.join(output, 'verification.json'), JSON.stringify({ mockedAuth: true, results }, null, 2))
    console.log(JSON.stringify(results))
  } finally {
    await browser.close()
    await new Promise(resolve => server.close(resolve))
  }
})().catch(error => { console.error(error); process.exitCode = 1; server.close() })
