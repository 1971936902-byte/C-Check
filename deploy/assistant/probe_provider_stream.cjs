const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')

async function main() {
  const root = process.argv[2]
  if (!root) throw new Error('Pass the prepared, patched AnythingLLM source directory')
  const source = fs.readFileSync(path.join(root, 'server/utils/AiProviders/genericOpenAi/index.js'), 'utf8')
  const start = source.indexOf('  handleStream(response, stream, responseProps) {')
  const end = source.indexOf('\n  /**', start)
  assert(start > 0 && end > start)
  const events = []
  // Exercise the pinned provider's actual method without loading model SDKs.
  const Probe = vm.runInNewContext('class Probe {' + source.slice(start, end) +
    '\n#extractLlamaCppTimings() {}\n}; Probe', {
    process: { env: { CCHECK_ASSISTANT_MODE: 'true' } },
    uuidv4: () => 'test-id', extractReasoningContent: () => undefined,
    clientAbortedHandler: (resolve, text) => resolve(text),
    isAbortError: error => error.name === 'AbortError',
    writeResponseChunk: (response, event) => events.push(event),
    console: { log() {} },
  })
  const response = new EventEmitter()
  response.locals = {}
  let aborted = false
  const interrupted = {
    controller: { abort() { aborted = true } }, endMeasurement() {},
    async *[Symbol.asyncIterator]() {
      yield { choices: [{ delta: { content: 'partial' } }] }
      throw new Error('review_priority_interrupted')
    },
  }
  assert.equal(await new Probe().handleStream(response, interrupted, {}), 'partial')
  assert.equal(response.locals.ccheckInterrupted, 'review_priority_interrupted')
  response.emit('close')
  assert.equal(response.locals.ccheckInterrupted, 'review_priority_interrupted')
  assert(aborted)

  events.length = 0
  const completed = new EventEmitter()
  completed.locals = {}
  const normal = {
    endMeasurement() {},
    async *[Symbol.asyncIterator]() {
      yield { choices: [{ delta: { content: 'complete' } }] }
      yield { choices: [{ delta: {}, finish_reason: 'stop' }] }
    },
  }
  assert.equal(await new Probe().handleStream(completed, normal, {}), 'complete')
  assert.equal(completed.locals.ccheckInterrupted, undefined)
  assert(events.every(event => event.close === false))
  assert.equal(completed.listenerCount('close'), 0)
  console.log('PROVIDER_INTERRUPTION_CONTRACT_OK')
}
main().catch(error => { console.error(error); process.exitCode = 1 })
