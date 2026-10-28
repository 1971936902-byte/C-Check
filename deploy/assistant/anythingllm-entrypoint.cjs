const fs = require('node:fs')
const { spawn, spawnSync } = require('node:child_process')

function secret(name) {
  const file = process.env[name + '_FILE']
  if (!file || !file.startsWith('/run/secrets/')) throw new Error('Missing secret mount: ' + name)
  const value = fs.readFileSync(file, 'utf8').replace(/[\r\n]+$/, '')
  if (value.length < 32 || value.includes('\0')) throw new Error('Invalid secret: ' + name)
  return value
}

async function main() {
  const gatewayKey = secret('ASSISTANT_GATEWAY_KEY')
  const apiKey = secret('ASSISTANT_ANYTHINGLLM_API_KEY')
  process.env.GENERIC_OPEN_AI_API_KEY = gatewayKey
  process.env.GENERIC_OPEN_AI_EMBEDDING_API_KEY = gatewayKey
  process.env.CCHECK_ASSISTANT_MODE = 'true'
  process.env.STORAGE_DIR = '/app/server/storage'
  process.env.CHECKPOINT_DISABLE = '1'
  const migration = spawnSync(process.execPath, ['/app/server/node_modules/prisma/build/index.js',
    'migrate', 'deploy', '--schema=/app/server/prisma/schema.prisma'],
    { cwd: '/app/server', env: process.env, stdio: 'inherit' })
  if (migration.status !== 0) throw new Error('AnythingLLM migration failed')
  const { PrismaClient } = require('/app/server/node_modules/@prisma/client')
  const prisma = new PrismaClient()
  try {
    await prisma.api_keys.upsert({ where: { secret: apiKey },
      create: { secret: apiKey, name: 'C-Check internal assistant' }, update: {} })
  } finally { await prisma.$disconnect() }
  const children = ['/app/server/index.js', '/app/collector/index.js'].map(file => spawn(process.execPath,
    [file], { cwd: file.includes('/collector/') ? '/app/collector' : '/app/server', env: process.env, stdio: 'inherit' }))
  let stopping = false
  const stop = (code) => {
    if (stopping) return
    stopping = true
    for (const child of children) child.kill('SIGTERM')
    const timer = setTimeout(() => { for (const child of children) child.kill('SIGKILL'); process.exit(code) }, 10000)
    Promise.all(children.map(child => new Promise(resolve => {
      if (child.exitCode !== null || child.signalCode !== null) resolve()
      else child.once('exit', resolve)
    }))).then(() => { clearTimeout(timer); process.exit(code) })
  }
  for (const child of children) { child.once('exit', code => stop(code || 1)); child.once('error', () => stop(1)) }
  process.on('SIGTERM', () => stop(0))
  process.on('SIGINT', () => stop(0))
}
main().catch(error => { console.error('AnythingLLM startup failed:', error.name); process.exitCode = 1 })
