"""Project-scoped network planning and verification; never prunes shared networks."""
import ipaddress
import json

from .core import DeploymentError, run

DEFAULT_SUBNETS = {'data': '172.30.10.0/26', 'inference': '172.30.10.64/26', 'edge': '172.30.10.128/26'}


def subnets(site):
    values = site.get('network_subnets', DEFAULT_SUBNETS)
    if not isinstance(values, dict) or set(values) != set(DEFAULT_SUBNETS):
        raise DeploymentError('network_subnets must contain exactly data, inference and edge')
    values = dict(values)
    from .assistant import settings as assistant_settings
    assistant = assistant_settings(site)
    if assistant['enabled']:
        values['assistant'] = assistant['subnet']
    parsed = {}
    try:
        for name, value in values.items():
            if not isinstance(value, str):
                raise ValueError('CIDR must be a string')
            net = ipaddress.IPv4Network(value, strict=True)
            private = any(net.subnet_of(ipaddress.IPv4Network(block)) for block in
                          ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
            if not 16 <= net.prefixlen <= 28 or not private:
                raise ValueError('use private IPv4 CIDRs with prefixes /16 through /28')
            parsed[name] = net
    except ValueError as exc:
        raise DeploymentError('invalid fixed network subnet: ' + str(exc)) from exc
    for name, left in parsed.items():
        if any(left.overlaps(right) for other, right in parsed.items() if other != name):
            raise DeploymentError('project network subnets overlap')
    return {name: str(parsed[name]) for name in values}


def specifications(site):
    return {name: dict({'internal': True} if name != 'edge' else {}, driver='bridge',
            ipam={'driver': 'default', 'config': [{'subnet': cidr}]}) for name, cidr in subnets(site).items()}


def inventory():
    ids = run(['docker', 'network', 'ls', '-q']).split()
    return json.loads(run(['docker', 'network', 'inspect', *ids])) if ids else []


def owned(engine, network):
    from .engine import project_name
    project = project_name(engine.site)
    labels = network.get('Labels') or {}
    name = labels.get('com.docker.compose.network')
    return (name in {*DEFAULT_SUBNETS, 'assistant'} and labels.get('com.docker.compose.project') == project
            and network['Name'] == project + '_' + name)


def assert_scope(engine, rows):
    from .engine import project_name
    project = project_name(engine.site)
    expected_names = {project + '_' + name for name in (*DEFAULT_SUBNETS, 'assistant')}
    for network in rows:
        if network['Name'] in expected_names and not owned(engine, network):
            raise DeploymentError('project network name belongs to another owner: ' + network['Name'])
        if not owned(engine, network):
            continue
        for container in (network.get('Containers') or {}):
            data = json.loads(run(['docker', 'inspect', container]))[0]
            labels = data['Config'].get('Labels') or {}
            if (labels.get('com.docker.compose.project') != project
                    or labels.get('com.docker.compose.project.config_files') != str(engine.file)):
                raise DeploymentError('foreign endpoint attached to project network; refusing teardown')


def preflight(site):
    from .engine import Engine
    engine = Engine(site)
    desired = {name: ipaddress.IPv4Network(cidr) for name, cidr in subnets(site).items()}
    rows = inventory()
    assert_scope(engine, rows)
    ignored_routes = set()
    for network in rows:
        is_ours = owned(engine, network)
        for entry in network.get('IPAM', {}).get('Config') or []:
            if not entry.get('Subnet'):
                continue
            net = ipaddress.ip_network(entry['Subnet'])
            if net.version != 4:
                continue
            if is_ours:
                bridge = (network.get('Options') or {}).get('com.docker.network.bridge.name', 'br-' + network['Id'][:12])
                ignored_routes.add((str(net), bridge))
                continue
            if any(net.overlaps(target) for target in desired.values()):
                raise DeploymentError('fixed subnet overlaps another Docker network: ' + network['Name'])
    routes = json.loads(run(['ip', '-j', '-4', 'route', 'show', 'table', 'all']))
    for route in routes:
        destination = route.get('dst', 'default')
        if destination in {'default', '0.0.0.0/0'}:
            continue
        net = ipaddress.IPv4Network(destination, strict=False)
        if any(route.get('dev') == device and net.subnet_of(ipaddress.IPv4Network(owned_cidr))
               for owned_cidr, device in ignored_routes):
            continue
        if any(net.overlaps(target) for target in desired.values()):
            raise DeploymentError('fixed subnet overlaps host/LAN/VPN route: ' + destination)
    return {'subnets': subnets(site), 'conflicts': False, 'changes_applied': False}


def matches(actual, expected):
    expected_subnets = [entry['subnet'] for entry in expected.get('ipam', {}).get('config', [])]
    actual_subnets = [entry['Subnet'] for entry in actual.get('IPAM', {}).get('Config') or [] if entry.get('Subnet')]
    return (actual.get('Driver') == expected.get('driver', 'bridge')
            and actual.get('IPAM', {}).get('Driver', 'default') == expected.get('ipam', {}).get('driver', 'default')
            and not actual.get('EnableIPv6', False)
            and bool(actual.get('Internal')) == bool(expected.get('internal'))
            and (not expected_subnets or actual_subnets == expected_subnets))


def needs_recreate(engine, document):
    rows = inventory()
    assert_scope(engine, rows)
    expected = document['networks']
    if not set(DEFAULT_SUBNETS) <= set(expected) <= {*DEFAULT_SUBNETS, 'assistant'}:
        raise DeploymentError('unexpected network set in project recipe')
    return any(row['Labels']['com.docker.compose.network'] not in expected
               or not matches(row, expected[row['Labels']['com.docker.compose.network']])
               for row in rows if owned(engine, row))


def teardown(engine):
    assert_scope(engine, inventory())
    # No -v, no prune, no forced disconnection of other projects.
    return engine.compose('down', '--timeout', '120')


def verify(engine):
    from .engine import project_name
    document = json.loads(engine.file.read_text())
    rows = inventory()
    assert_scope(engine, rows)
    for name, spec in document['networks'].items():
        found = [row for row in rows if row['Name'] == project_name(engine.site) + '_' + name and owned(engine, row)]
        if len(found) != 1 or not matches(found[0], spec):
            raise DeploymentError('running project network differs from recipe: ' + name)
    return {'verified': True, 'networks': list(document['networks'])}


def verify_container(engine, specification, inspection, document):
    from .engine import project_name
    prefix = project_name(engine.site) + '_'
    expected = {prefix + name for name in specification['networks']}
    actual = inspection.get('NetworkSettings', {}).get('Networks', {})
    if set(actual) != expected:
        raise DeploymentError('running container network attachments differ from recipe')
    for name in specification['networks']:
        cidrs = document['networks'][name].get('ipam', {}).get('config', [])
        if cidrs:
            address = actual[prefix + name].get('IPAddress', '')
            try:
                valid = ipaddress.IPv4Address(address) in ipaddress.IPv4Network(cidrs[0]['subnet'])
            except ValueError:
                valid = False
            if not valid:
                raise DeploymentError('running container address is outside the configured subnet')
