"""Disposable, CPU-only real-Docker network migration/data-preservation test."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cli'))
from ccheck.core import DeploymentError, atomic_json, run
from ccheck.engine import Engine, project_name
from ccheck import networks
from ccheck.lifecycle import begin_network_migration, finish_network_migration, assert_no_pending_network_migration


def inventory():
    ids = run(['docker', 'ps', '-aq']).split()
    return {row['Id']: row['State']['StartedAt'] for row in json.loads(run(['docker', 'inspect', *ids]))} if ids else {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-tree', required=True)
    args = parser.parse_args()
    before = inventory()
    root = Path(tempfile.mkdtemp(prefix='ccheck-network-integration-'))
    site = {'instance': 'net-check-' + uuid.uuid4().hex[:8], 'data_dir': str(root)}
    engine = Engine(site)
    foreign = None
    (root / 'data').mkdir(mode=0o755)
    (root / 'data').chmod(0o755)
    (root / 'secrets').mkdir(mode=0o700)
    (root / 'data/sentinel').write_text('existing-data-must-survive')
    (root / 'data/sentinel').chmod(0o644)
    (root / 'secrets/fixture').write_text('existing-fixture-key')
    previous = {'name': project_name(site), 'services': {'probe': {
        'image': args.image, 'pull_policy': 'never', 'restart': 'no', 'user': '10001:10001',
        'read_only': True, 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
        'runtime': 'runc', 'environment': {'NVIDIA_VISIBLE_DEVICES': 'void'},
        'entrypoint': ['sh'], 'command': ['-c', "trap 'exit 0' TERM INT; while :; do sleep 1 & wait $!; done"],
        'networks': list(networks.DEFAULT_SUBNETS),
        'volumes': [{'type': 'bind', 'source': str(root / 'data'), 'target': '/check', 'read_only': True}]}},
        'networks': {'data': {'internal': True}, 'inference': {'internal': True}, 'edge': {}}}
    desired = copy.deepcopy(previous)
    desired['networks'] = networks.specifications(site)
    checks = {}
    try:
        networks.preflight(site)
        atomic_json(engine.file, previous)
        engine.compose('up', '-d')
        checks['old_auto_network_requires_recreate'] = networks.needs_recreate(engine, desired)
        assert checks['old_auto_network_requires_recreate']
        begin_network_migration(engine, site, previous, None)
        networks.teardown(engine)
        atomic_json(engine.file, desired)
        engine.compose('up', '-d')
        networks.verify(engine)
        container = engine.compose('ps', '-q', 'probe').strip()
        actual = json.loads(run(['docker', 'inspect', container]))[0]
        networks.verify_container(engine, desired['services']['probe'], actual, desired)
        assert engine.compose('exec', '-T', 'probe', 'cat', '/check/sentinel').strip() == 'existing-data-must-survive'
        assert (root / 'secrets/fixture').read_text() == 'existing-fixture-key'
        checks['fixed_subnets_and_data_preserved'] = True
        finish_network_migration(root)
        assert_no_pending_network_migration(root)
        checks['idempotent_no_recreate'] = not networks.needs_recreate(engine, desired)
        foreign = run(['docker', 'run', '-d', '--rm', '--network', project_name(site) + '_data',
                       '--entrypoint', 'sh', args.image, '-c', 'sleep 600']).strip()
        try:
            networks.teardown(engine)
        except DeploymentError:
            checks['foreign_endpoint_refuses_teardown'] = True
        else:
            raise AssertionError('foreign endpoint was not protected')
        run(['docker', 'rm', '-f', foreign])
        foreign = None
        networks.teardown(engine)
        atomic_json(engine.file, previous)
        engine.compose('up', '-d')
        networks.verify(engine)
        assert engine.compose('exec', '-T', 'probe', 'cat', '/check/sentinel').strip() == 'existing-data-must-survive'
        checks['previous_recipe_recovery_and_data_preserved'] = True
        networks.teardown(engine)
        assert inventory() == before
        assert all(checks.values())
        result = {'passed': True, 'source_tree': args.source_tree, 'checks': checks,
                  'existing_containers_unchanged': len(before), 'gpu_tested': False}
        args.output.write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
    finally:
        if foreign:
            run(['docker', 'rm', '-f', foreign])
        if engine.file.exists():
            networks.teardown(engine)
        shutil.rmtree(root)


if __name__ == '__main__':
    main()
