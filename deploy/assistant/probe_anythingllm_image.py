"""Probe the real provider with disposable storage, no host ports and no GPU."""
import argparse
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from prepare_storage import prepare


def run(*args, timeout=30):
    return subprocess.run(args, check=True, text=True, capture_output=True,
                          timeout=timeout).stdout.strip()


def safe_diagnostic(error, logs, secrets):
    parts = [logs]
    for field in ('stdout', 'stderr'):
        value = getattr(error, field, None)
        if isinstance(value, bytes):
            value = value.decode('utf-8', 'replace')
        if value:
            parts.append(value)
    diagnostic = '\n'.join(parts)
    for credential in secrets.iterdir():
        value = credential.read_text().strip()
        if value:
            diagnostic = diagnostic.replace(value, '[redacted]')
    return diagnostic[-12000:]


CHECK = r"""
const fs = require('node:fs');
const assert = require('node:assert/strict');
const http = require('node:http');
let embeddingCalls = 0;
let retrievedContext = false;
// This fixture validates provider wiring, not model quality or GPU cancellation.
const gateway = http.createServer(async (request, response) => {
  try {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : {};
    response.setHeader('Content-Type', 'application/json');
    if (request.url === '/v1/embeddings') {
      assert.equal(body.model, 'jina-code-embeddings-1.5b');
      assert.equal(body.encoding_format, 'float');
      embeddingCalls++;
      const inputs = Array.isArray(body.input) ? body.input : [body.input];
      response.end(JSON.stringify({object: 'list', model: body.model, data: inputs.map((_, index) =>
        ({object: 'embedding', index, embedding: Array.from({length: 1536}, (_, i) => i === 0 ? 1 : 0)})),
        usage: {prompt_tokens: 1, total_tokens: 1}}));
    } else if (request.url === '/v1/chat/completions') {
      assert.equal(body.model, 'qwen2.5-coder-32b-instruct-fp8');
      retrievedContext = body.messages.some(message => typeof message.content === 'string'
        && message.content.includes('calibration value is 73'));
      response.end(JSON.stringify({id: 'probe-completion', object: 'chat.completion', created: 1,
        model: body.model, choices: [{index: 0, message: {role: 'assistant',
          content: 'Probe response: calibration value is 73.'}, finish_reason: 'stop'}],
        usage: {prompt_tokens: 1, completion_tokens: 1, total_tokens: 2}}));
    } else {
      response.statusCode = 404;
      response.end(JSON.stringify({error: 'unknown probe endpoint'}));
    }
  } catch {
    response.statusCode = 500;
    response.end(JSON.stringify({error: 'probe model contract mismatch'}));
  }
});
async function runProbe() {
  assert.equal(process.getuid(), 10001);
  assert.throws(() => fs.writeFileSync('/app/server/readonly-probe', 'no'));
  const base = 'http://127.0.0.1:3001/api/v1';
  const key = fs.readFileSync('/run/secrets/assistant_anythingllm_api_key', 'utf8').trim();
  const headers = {Authorization: `Bearer ${key}`};
  const unauthorized = await fetch(base + '/workspaces', {signal: AbortSignal.timeout(10000)});
  assert.ok([401, 403].includes(unauthorized.status));
  async function request(path, options = {}) {
    const response = await fetch(base + path, {...options, signal: AbortSignal.timeout(60000),
      headers: {...headers, ...options.headers}});
    assert.ok(response.ok, `${path}: HTTP ${response.status}`);
    return response.json();
  }
  assert.equal((await request('/auth')).authenticated, true);
  const name = 'ccheck-provider-probe';
  const workspace = await request('/workspace/new', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name,
      chatProvider: 'generic-openai', chatModel: 'qwen2.5-coder-32b-instruct-fp8'})});
  assert.equal(workspace.workspace.slug, name);
  assert.equal((await request('/workspace/' + name)).workspace[0].slug, name);
  const form = new FormData();
  form.append('file', new Blob(['C-Check parser probe. The calibration value is 73.']), 'probe.txt');
  const uploaded = await request('/document/upload', {method: 'POST', body: form});
  assert.equal(uploaded.success, true);
  assert.ok(uploaded.documents.length > 0);
  assert.ok(uploaded.documents.every(document => typeof document.location === 'string'));
  console.log('ANYTHINGLLM_AUTH_STORAGE_PARSER_PROBE_OK');
  await request('/workspace/' + name + '/update-embeddings', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({adds: uploaded.documents.map(document => document.location), deletes: []})});
  const indexed = (await request('/workspace/' + name)).workspace[0].documents;
  assert.ok(indexed.length > 0);
  const answer = await request('/workspace/' + name + '/chat', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: 'What is the calibration value?', mode: 'query'})});
  assert.ok(answer.textResponse.includes('73'));
  assert.ok(answer.sources.length > 0);
  assert.ok(embeddingCalls >= 2);
  assert.ok(retrievedContext, 'retrieved document must reach the chat model');
  console.log('ANYTHINGLLM_RAG_MOCK_MODEL_PROBE_OK');
}
async function main() {
  await new Promise((resolve, reject) => {gateway.once('error', reject); gateway.listen(8010, '127.0.0.1', resolve)});
  try { await runProbe() }
  finally { await new Promise(resolve => gateway.close(resolve)) }
}
main().catch(error => {console.error(error.message); process.exit(1)});
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    resources = Path(__file__).resolve().parent
    image = run('docker', 'image', 'inspect', '--format', '{{.Id}}', args.image)
    name = 'ccheck-provider-probe-' + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix='ccheck-provider-probe-') as temporary:
        root = Path(temporary).resolve()
        prepare(root)
        mounts = [(resources, '/opt/assistant', True),
                  (root / 'data/assistant/storage', '/app/server/storage', False),
                  (root / 'data/assistant/collector-hotdir', '/app/collector/hotdir', False),
                  (root / 'data/assistant/collector-storage', '/app/collector/storage', False),
                  (root / 'data/assistant/server.env', '/app/server/.env', False)]
        command = ['docker', 'run', '-d', '--pull=never', '--name', name,
                   '--network=none', '--add-host=assistant-gateway:127.0.0.1',
                   '--runtime=runc', '--user=10001:10001', '--read-only',
                   '--cap-drop=ALL', '--security-opt=no-new-privileges:true', '--pids-limit=512',
                   '--memory=4g', '--cpus=2', '--tmpfs=/tmp:size=536870912,mode=1777',
                   '--env-file', str(resources / 'anythingllm.env.example'), '-e', 'HOME=/tmp',
                   '-e', 'NVIDIA_VISIBLE_DEVICES=void']
        for key in ('assistant_gateway_key', 'assistant_anythingllm_api_key'):
            target = '/run/secrets/' + key
            mounts.append((root / 'secrets' / key, target, True))
            command += ['-e', key.upper() + '_FILE=' + target]
        for source, target, readonly in mounts:
            command += ['--mount', f'type=bind,src={source},dst={target}' + (',readonly' if readonly else '')]
        command.append(image)
        try:
            run(*command)
            for attempt in range(60):
                try:
                    run('docker', 'exec', name, 'node', '-e',
                        "Promise.all(['http://127.0.0.1:3001/api/ping','http://127.0.0.1:8888/accepts'].map(u=>fetch(u,{signal:AbortSignal.timeout(3000)}))).then(rs=>{if(rs.some(r=>!r.ok)||!require('fs').existsSync('/app/server/storage/comkey/ipc-pub.pem'))process.exit(1)}).catch(()=>process.exit(1))")
                    break
                except subprocess.CalledProcessError:
                    if run('docker', 'inspect', '--format', '{{.State.Running}}', name) != 'true':
                        raise RuntimeError('provider exited during startup') from None
                    if attempt == 59:
                        raise RuntimeError('provider readiness timed out') from None
                    time.sleep(2)
            print(run('docker', 'exec', name, 'node', '-e', CHECK, timeout=180))
            print('Provider image:', image)
        except Exception as error:
            try:
                result = subprocess.run(['docker', 'logs', '--tail', '80', name],
                                        text=True, capture_output=True, timeout=15)
                logs = result.stdout + result.stderr
            except subprocess.TimeoutExpired:
                logs = 'Provider log collection timed out.'
            print(safe_diagnostic(error, logs, root / 'secrets'))
            raise RuntimeError('Provider probe failed; see redacted diagnostics above.') from None
        finally:
            try:
                removed = subprocess.run(['docker', 'rm', '-f', name], timeout=30,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if removed.returncode:
                    print('WARNING: verify cleanup of probe container ' + name)
            except subprocess.TimeoutExpired:
                print('WARNING: cleanup timed out for probe container ' + name)


if __name__ == '__main__':
    main()
