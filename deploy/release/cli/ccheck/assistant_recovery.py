"""Resume assistant failures using owned process replacement as the idle proof."""
import json
from pathlib import Path
import secrets
import time

from . import assistant
from .core import DeploymentError, atomic_json, checked_path, digest, installation_lock, verify_release


def assert_no_pending(site):
    path = checked_path(site['data_dir'], 'state/assistant-recovery.json')
    if path.exists() and json.loads(path.read_text()).get('status') in {'running', 'failed'}:
        raise DeploymentError('assistant recovery is pending; resume ccheck assistant-recover before backup/update')


def recover_stack(engine, release_sha256):
    if not assistant.enabled(engine.site):
        raise DeploymentError('assistant is disabled')
    path = checked_path(engine.root, 'state/assistant-recovery.json')
    previous = json.loads(path.read_text()) if path.exists() else None
    was_pending = bool(previous and previous.get('status') in {'running', 'failed'})
    if previous and previous.get('status') in {'running', 'failed', 'blocked'}:
        if previous.get('release_sha256') != release_sha256 or previous.get('instance') != engine.site['instance']:
            raise DeploymentError('pending assistant recovery belongs to a different release/instance')
        journal = previous
    else:
        journal = {'instance': engine.site['instance'], 'release_sha256': release_sha256,
            'token': secrets.token_hex(32), 'prior_maintenance': (engine.root / 'data/control/maintenance').exists()}
    began = False
    def step(name, action):
        journal.update(stage=name, status='running', time=time.time())
        atomic_json(path, journal)
        return action()
    try:
        step('fence-admission', lambda: engine.management('assistant-recovery-begin', '--recovery-token', journal['token']))
        began = True
        engine.maintenance(True)
        step('stop-assistant-writers', lambda: engine.compose('stop', '--timeout', '30', 'assistant-worker', 'anythingllm'))
        step('stop-owned-models', lambda: engine.compose('stop', '--timeout', '120', 'qwen', 'embedding'))
        step('replace-qwen', lambda: engine.compose('up', '-d', '--force-recreate', 'qwen'))
        engine.wait(['qwen'])
        step('replace-jina', lambda: engine.compose('up', '-d', '--force-recreate', 'embedding'))
        engine.wait(['embedding'])
        step('restart-private-services', lambda: engine.compose('up', '-d', '--force-recreate', 'assistant-gateway', 'anythingllm'))
        engine.wait(['assistant-gateway', 'anythingllm'])
        result = step('release-confirmed-idle', lambda: engine.management(
            'assistant-recovery-finish', '--recovery-token', journal['token']))
        step('restart-document-worker', lambda: engine.compose('up', '-d', '--force-recreate', 'assistant-worker'))
        engine.wait(['assistant-worker'])
        engine.maintenance(journal['prior_maintenance'])
        journal.update(status='completed', stage='ready', time=time.time())
        atomic_json(path, journal)
        print('ASSISTANT_RECOVERED', flush=True)
        return result
    except Exception:
        journal.update(status='failed' if began or was_pending else 'blocked', time=time.time())
        atomic_json(path, journal)
        if began:
            engine.maintenance(True)
        raise


def recover(site):
    from .engine import Engine, render_compose
    from .lifecycle import installed
    root = Path(site['data_dir'])
    engine = Engine(site)
    with installation_lock(root):
        state = installed(site)
        release = verify_release(state['release_root'], root / 'config/publisher.pem', state['publisher'])
        if release['_sha256'] != state['release_sha256']:
            raise DeploymentError('installed release differs from the recovery source')
        from .inference import configure
        configure(site, release)
        assistant.validate_release(site, release)
        assistant.verify(site)
        if (digest(root / 'config/site.json') != state['site_sha256'] or
                json.loads(engine.file.read_text()) != render_compose(site, state['images'], state['uid'], state['gid'])):
            raise DeploymentError('managed configuration differs from the installed recipe')
        engine.management('data-health')
        return recover_stack(engine, state['release_sha256'])
