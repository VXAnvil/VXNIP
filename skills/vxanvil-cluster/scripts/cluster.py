#!/usr/bin/env python3
"""Plan mixed VXAnvil/NVKE installations or read-only existing-cluster checks over trusted SSH."""
import argparse
import base64
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

import artifacts as runtime_assets

HERE = Path(__file__).resolve().parent
BUNDLE_MANIFEST = HERE.parent / 'references/runtime-artifacts.json'
RELEASE = {'provider': '633b2efb1d3b087376b995c906887a3d8c994ba7',
           'nvke': '2f31480b360cbd7fc9d9db7a6ba274a8d0fd5049'}
SSH_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
               '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15',
               '-o', 'ServerAliveCountMax=3']


class ExternalGate(RuntimeError):
    """A known idempotent route gate, not an uncertain installation result."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def private_dir(path):
    path = Path(path).absolute()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise ValueError('State directory must be owned by this user and not a symlink')
    os.chmod(path, 0o700)
    return path


def save(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.next-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(canonical(value) + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def connection(node):
    if node.get('control_path'):
        return {'ssh': node['ssh'], 'control_path': node['control_path']}
    return node['ssh']


def ssh(target, command):
    options = list(SSH_OPTIONS)
    if isinstance(target, dict):
        path = Path(target['control_path']).expanduser().absolute()
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('ControlPath must be an existing socket owned by the current user')
        options.extend(['-S', str(path), '-o', 'ControlMaster=no'])
        target = target['ssh']
    if not isinstance(target, str) or not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.@-]*', target):
        raise ValueError('Use a single SSH config alias or user@hostname; options belong in SSH config')
    return ['ssh', *options, target, command]


def remote(target, command, data=None, log=None, timeout=60, external_gate=False):
    result = subprocess.run(ssh(target, command), input=data, stdout=log or subprocess.PIPE,
                            stderr=log or subprocess.PIPE, timeout=timeout, check=False)
    if result.returncode:
        if external_gate and result.returncode == 75:
            if external_gate == 'runtime-download':
                raise ExternalGate('Pinned runtime download/verification is pending; no source-build fallback or node installation was started')
            raise ExternalGate('Tailscale route/grant approval or connectivity is pending; no worker installation was started')
        target = target['ssh'] if isinstance(target, dict) else target
        diagnostic = result.stderr.decode(errors='replace') if isinstance(result.stderr, bytes) else ''
        if result.returncode == 255 and 'Permission denied' in diagnostic:
            raise RuntimeError('SSH authentication rejected on ' + target + '; supply a configured SSH alias/IdentityFile')
        if result.returncode == 255 and 'Host key verification failed' in diagnostic:
            raise RuntimeError('SSH host key is not trusted on ' + target + '; verify it through a trusted channel')
        if 'sudo:' in diagnostic and ('password' in diagnostic or 'not allowed' in diagnostic):
            raise RuntimeError('Non-interactive sudo is unavailable on ' + target)
        raise RuntimeError('SSH operation failed on ' + target + '; inspect private logs; do not blindly retry')
    return result.stdout


def probe(target, require_root=True):
    code = (HERE / 'probe.py').read_text()
    prefix = 'sudo -n ' if require_root else ''
    value = json.loads(remote(target, prefix + 'python3 -c ' + shlex.quote(code)))
    identity = json.loads(remote(target, "python3 -c " + shlex.quote(
        'import json,os,pwd; print(json.dumps({"user":pwd.getpwuid(os.getuid()).pw_name,'
        '"address":os.environ.get("SSH_CONNECTION", "").split()[-2]}))')))
    value.update(identity)
    return value


def node_info(config, facts, role, mode='create', network_mode='lan'):
    if facts['os'] != 'ubuntu' or facts['version'] != '24.04' or facts['arch'] not in ('amd64', 'arm64'):
        raise ValueError(role + ': requires Ubuntu 24.04 AMD64/ARM64')
    kind = 'vm' if facts['virtualization'] else 'baremetal'
    if config.get('kind', kind) != kind or facts.get('container'):
        raise ValueError(role + ': observed machine type differs or is a container')
    if mode == 'create' and network_mode == 'tailscale' and (facts.get('tailscale_state') != 'Running' or facts.get('tailscale') is None or not any(
            iface.get('ifname') == 'tailscale0' for iface in facts['addresses'])):
        raise ValueError(role + ': Tailscale must already be installed and authenticated')
    if mode == 'create' and (facts['allocated'] is not False or facts['swap'] or facts.get('existing_cni', False) is not False):
        raise ValueError(role + ': existing allocation, CNI or swap; no automatic reset/swap removal')
    min_cpu, min_memory, min_disk = (6, 16, 100) if role == 'manager' else (2, 4, 20)
    if mode == 'create' and (facts['cpus'] < min_cpu or facts['memory'] < min_memory * 2**30 * 0.95
            or facts['free_disk'] < min_disk * 2**30):
        raise ValueError(f'{role}: needs {min_cpu} CPUs, {min_memory} GiB RAM and {min_disk} GiB free disk')
    cloud = facts.get('cloud_init', {})
    if mode == 'create' and (cloud.get('status') != 'done' or cloud.get('errors') or any(
            key != 'DEPRECATED' for key in cloud.get('recoverable_errors', {}))):
        raise ValueError(role + ': cloud-init must complete without non-deprecation errors')
    if mode == 'create' and facts.get('unknown'):
        raise ValueError(role + ': required inventory observations are unknown')
    if mode == 'create' and facts['containers'] and (role == 'manager' or kind == 'vm'):
        raise ValueError(role + ': dedicated manager/VM must not have existing containers')
    if mode == 'create' and role == 'manager' and facts.get('available_memory', facts['memory']) < 12 * 2**30:
        raise ValueError('manager: at least 12 GiB available RAM required for the management stack')
    if mode == 'create' and role == 'manager' and not facts['kernel_config']:
        raise ValueError('manager: matching /boot kernel config is required')
    if mode == 'create' and role == 'manager' and facts.get('seed_media'):
        raise ValueError('manager: detach the cloud-init seed/config-drive before starting a privileged tenant')
    if mode == 'create' and role == 'worker' and kind == 'baremetal':
        if facts.get('prepared_runtime') or facts.get('missing_baremetal_tools'):
            raise ValueError('baremetal: existing prepared runtime or missing prerequisite tools; do not adopt/reconfigure it')
        if not facts.get('shared_cri') or not re.search(r'\bv?2\.\d', facts.get('containerd_version', '')):
            raise ValueError('baremetal: existing shared containerd 2.x and Docker are required for isolated-runtime preparation')
        if not facts.get('docker_running', True):
            raise ValueError('baremetal: the existing Docker daemon must be healthy')
    default_address = facts['address']
    if network_mode == 'tailscale' and role == 'manager' and not config.get('address'):
        default_address = next((r['prefsrc'] for r in facts.get('default_route', []) if r.get('prefsrc')), default_address)
    address = str(ipaddress.IPv4Address(config.get('address', default_address)))
    interfaces = [ipaddress.IPv4Interface(f"{a['local']}/{a['prefixlen']}")
                  for iface in facts['addresses'] for a in iface.get('addr_info', [])
                  if a.get('family') == 'inet' and a['local'] == address]
    private = [ipaddress.ip_network(cidr) for cidr in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    is_tail = ipaddress.ip_address(address) in ipaddress.ip_network('100.64.0.0/10')
    if len(interfaces) != 1 or not (any(ipaddress.ip_address(address) in n for n in private) or
                                                   network_mode == 'tailscale' and role == 'worker' and is_tail):
        raise ValueError(role + ': select a locally assigned private/Tailscale address appropriate for its role')
    if network_mode == 'tailscale' and role == 'worker' and not any(
            iface['ifname'] == 'tailscale0' and any(a.get('local') == address for a in iface.get('addr_info', [])) for iface in facts['addresses']):
        raise ValueError('Tailscale worker address must belong to tailscale0')
    user = config.get('worker_user', config.get('user', facts['user']))
    if mode == 'create' and role == 'worker' and (not re.fullmatch(r'[a-z_][a-z0-9_-]*', user) or user not in facts['users']):
        raise ValueError('worker: select a non-root account with a /home directory using worker_user')
    result = {'ssh': config['ssh'], 'address': address, 'subnet': str(interfaces[0].network),
              'user': user, 'facts': facts, 'kind': kind,
              'scheduling': 'experimental-NoSchedule' if kind == 'baremetal' and role == 'worker' else 'standard'}
    if config.get('control_path'):
        result['control_path'] = str(Path(config['control_path']).expanduser().absolute())
    return result


def select_networks(nodes, requested):
    used = []
    for node in nodes:
        for route in node['facts']['routes']:
            if route.get('dst') not in (None, 'default') and ':' not in route['dst']:
                used.append(ipaddress.ip_network(route['dst'], strict=False))
        used.extend(ipaddress.ip_network(n) for n in node['facts']['docker_networks'])
    ovn_bridge = ipaddress.ip_network('10.44.0.1/32')
    if any(ovn_bridge.overlaps(n) for n in used):
        raise ValueError('Existing route conflicts with pinned OVN local bridge 10.44.0.1')
    used.append(ovn_bridge)
    result = {}
    private = [ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    choices = {'docker': [f'172.{n}.91.0/24' for n in range(30, 15, -1)],
               'pod': [f'10.{n}.0.0/16' for n in range(220, 240)],
               'service': [f'10.{n}.0.0/16' for n in range(240, 250)]}
    for kind, candidates in choices.items():
        candidates = [requested[kind]] if kind in requested else candidates
        for candidate in candidates:
            net = ipaddress.ip_network(candidate)
            if net.version != 4 or not any(net.subnet_of(n) for n in private) or net.num_addresses < 32:
                raise ValueError('Use an IPv4 private subnet with at least 32 addresses')
            if not any(net.overlaps(other) for other in used):
                result[kind] = str(net)
                used.append(net)
                break
        else:
            raise ValueError('No non-overlapping ' + kind + ' subnet; specify networks explicitly')
    return result


def runtime_hashes():
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(HERE.iterdir()) if p.suffix in ('.py', '.sh')}


def workload_name(site):
    prefix, suffix = site.split('-', 1)
    return prefix + '-w-' + suffix


def make_plan(config, get_facts=None):
    if set(config) - {'site', 'manager', 'workers', 'networks', 'mode', 'network_mode', 'existing', 'artifact_mode'}:
        raise ValueError('Unknown connection configuration fields')
    mode, network_mode = config.get('mode', 'create'), config.get('network_mode', 'lan')
    if mode not in ('create', 'existing') or network_mode not in ('lan', 'tailscale'):
        raise ValueError('Use mode create/existing and network_mode lan/tailscale')
    artifact_mode = config.get('artifact_mode', 'release')
    if artifact_mode not in ('release', 'source'):
        raise ValueError('Use artifact_mode release or explicit developer source mode')
    catalog = runtime_assets.catalog(BUNDLE_MANIFEST, RELEASE) if mode == 'create' and artifact_mode == 'release' else None
    manager_asset = None
    if get_facts is None:
        get_facts = lambda target: probe(target, require_root=mode == 'create')
    site = config.get('site', 'vxanvil-lab')
    if not re.fullmatch(r'vxanvil-[a-z0-9](?:[a-z0-9-]{0,18}[a-z0-9])?', site):
        raise ValueError('Pinned runtimes require a short vxanvil-* site name')
    workers = config.get('workers', [])
    if not isinstance(workers, list) or not workers:
        raise ValueError('At least one worker is required')
    manager = node_info(config['manager'], get_facts(connection(config['manager'])), 'manager', mode, network_mode)
    workers = [node_info(w, get_facts(connection(w)), 'worker', mode, network_mode) for w in workers]
    nodes = [manager, *workers]
    if len({n['facts']['machine_id'] for n in nodes}) != len(nodes):
        raise ValueError('Machines must have distinct machine IDs; aliases cannot refer to the same host')
    if len({n['facts']['hostname'] for n in nodes}) != len(nodes):
        raise ValueError('Machines must have distinct hostnames')
    if len({n['address'] for n in nodes}) != len(nodes):
        raise ValueError('Node addresses must be unique')
    lan = ipaddress.ip_network(manager['subnet'])
    if network_mode == 'lan' and any(ipaddress.ip_address(n['address']) not in lan or n['subnet'] != str(lan) for n in workers):
        raise ValueError('LAN mode requires the same directly connected LAN; use Tailscale mode for remote workers')
    if mode == 'existing':
        existing = config.get('existing', {})
        required = ('management_kubeconfig', 'workload_kubeconfig', 'api', 'cluster', 'spire_address')
        if any(not existing.get(key) for key in required):
            raise ValueError('Existing mode requires explicit kubeconfig paths, API, cluster and SPIRE endpoint')
        if any(not isinstance(existing[k], str) or not existing[k].startswith('/') for k in required[:2]):
            raise ValueError('Existing kubeconfig paths must be absolute')
        if not re.fullmatch(r'https://(?:[0-9]{1,3}\.){3}[0-9]{1,3}:6443', existing['api']):
            raise ValueError('Existing API must name the exact private IPv4 TLS endpoint')
        api_ip = ipaddress.IPv4Address(existing['api'].split('//')[1].split(':')[0])
        if not any(api_ip in ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
            raise ValueError('Existing API must be a private endpoint')
        spire = ipaddress.IPv4Address(existing['spire_address'])
        if not spire.is_private and spire not in ipaddress.ip_network('100.64.0.0/10'):
            raise ValueError('SPIRE endpoint must be private/Tailscale')
        networks = config.get('networks', {})
        changes, artifacts = [], {}
    else:
        if config.get('existing'):
            raise ValueError('Existing site settings cannot authorize a new installation')
        existing = None
        networks = select_networks(nodes, config.get('networks', {}))
        if artifact_mode == 'source':
            artifacts = {manager['facts']['arch']: {'builder': 'manager'}}
            for i, node in enumerate(workers):
                arch = node['facts']['arch']
                if arch not in artifacts:
                    if node['facts'].get('available_memory', node['facts']['memory']) < 6 * 2**30 or node['facts']['free_disk'] < 80 * 2**30:
                        raise ValueError('A foreign-architecture native artifact builder needs 6 GiB available RAM and 80 GiB free disk')
                    artifacts[arch] = {'builder': 'worker-' + str(i)}
            changes = ['Developer source mode: compile pinned sources and build runtime images on the targets']
        else:
            manager_asset = catalog['assets']['manager-' + manager['facts']['arch']]
            artifacts = {}
            for i, node in enumerate(workers):
                arch = node['facts']['arch']
                artifacts.setdefault(arch, {'builder': 'worker-' + str(i), 'asset': catalog['assets']['node-' + arch]})
            changes = ['Download and verify the pinned prebuilt manager/node runtimes; never fall back to a source build',
                       'Install runtime prerequisites; no Go/compiler/image build on user machines']
        changes += ['Create new management and workload control plane; do not adopt existing allocations',
                    'Prepare VM or isolated bare-metal runtime and append a scoped Provider SSH key',
                    'Register/allocate only the planned hosts and verify network/identity',
                    'In LAN mode, retain the planned API /32 route across NVKE network reconfiguration',
                    'Preserve existing worker Docker containers, shared runtime, driver and network interfaces']
        if network_mode == 'tailscale':
            changes += ['Merge API/SPIRE route advertisements without SNAT; accept routes on workers',
                        'Require externally approved tailnet routes/grants; derive OVN MTU from all peer routes']
    return {'version': 3, 'created': int(time.time()), 'site': site, 'release': RELEASE,
            'workload_cluster': workload_name(site) if mode == 'create' else existing['cluster'],
            'manager': manager, 'workers': workers, 'networks': networks,
            'runtime': runtime_hashes(), 'execution': 'observe-existing' if mode == 'existing' else 'cpu-mixed-docker-control-plane',
            'mode': mode, 'network_mode': network_mode, 'artifact_mode': artifact_mode,
            'manager_asset': manager_asset, 'artifacts': artifacts, 'existing': existing, 'changes': changes}


def checked_plan(path, expected=None):
    plan = json.loads(Path(path).read_text())
    if expected and digest(plan) != expected:
        raise ValueError('Plan digest differs from the reviewed plan')
    if plan.get('version') != 3 or plan['release'] != RELEASE or plan['runtime'] != runtime_hashes():
        raise ValueError('Runtime/source changed; create and review a new plan')
    if plan.get('mode') == 'create' and plan.get('workload_cluster') != workload_name(plan['site']):
        raise ValueError('Workload cluster name must be distinct from the management site')
    return plan


def root_path(plan):
    return '/var/lib/vxanvil-bootstrap/' + plan['site']


def initialize(target, root, identity):
    program = '''import json,os,pathlib,sys
p=pathlib.Path(sys.argv[1]); identity=sys.argv[2]
parent=p.parent
if parent.is_symlink() or p.is_symlink(): raise SystemExit("Symlink state directory")
parent.mkdir(mode=0o700,exist_ok=True)
if parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022: raise SystemExit("Insecure state parent")
new=not p.exists()
if new: p.mkdir(mode=0o700)
if p.stat().st_uid != 0 or p.stat().st_mode & 0o077: raise SystemExit("Insecure state directory")
marker=p/"plan.digest"
if not new and (not marker.is_file() or marker.read_text()!=identity): raise SystemExit("State belongs to another plan")
if new: marker.write_text(identity); marker.chmod(0o600)
for name in ("runtime","input"): (p/name).mkdir(mode=0o700,exist_ok=True)
'''
    remote(target, 'sudo -n python3 -c ' + shlex.quote(program) + ' ' + shlex.quote(root)
           + ' ' + shlex.quote(identity))


def upload_runtime(node, plan):
    root = root_path(plan)
    target = connection(node)
    initialize(target, root, digest(plan))
    with tempfile.TemporaryFile() as archive:
        with tarfile.open(fileobj=archive, mode='w') as tar:
            for name in plan['runtime']:
                tar.add(HERE / name, arcname=name, recursive=False)
        archive.seek(0)
        remote(target, 'sudo -n tar -xf - -C ' + shlex.quote(root + '/runtime'), archive.read())
    settings = {'plan': plan, 'node': node, 'root': root}
    payload = base64.b64encode(canonical(settings)).decode()
    remote(target, 'sudo -n python3 -c ' + shlex.quote(
        'import base64,pathlib,sys; p=pathlib.Path(sys.argv[1]); '
        'p.write_bytes(base64.b64decode(sys.argv[2])); p.chmod(0o600)')
        + ' ' + shlex.quote(root + '/settings.json') + ' ' + payload)


def phase(node, plan, name, log):
    command = shlex.join(['sudo', '-n', 'bash', root_path(plan) + '/runtime/runtime.sh',
                         name, root_path(plan)])
    remote(connection(node), command, log=log, timeout=8 * 3600, external_gate='runtime-download' if name in ('fetch-manager', 'fetch-node') else name == 'tailscale-worker')


def transfer(manager, worker, plan, log):
    root = root_path(plan)
    source = ssh(connection(manager), 'sudo -n tar -cf - -C ' + shlex.quote(root + '/handoff') + ' .')
    destination = ssh(connection(worker), 'sudo -n tar -xf - -C ' + shlex.quote(root + '/input'))
    with subprocess.Popen(source, stdout=subprocess.PIPE, stderr=log) as sender:
        try:
            result = subprocess.run(destination, stdin=sender.stdout, stdout=log, stderr=log,
                                    timeout=3600, check=False)
            sender.stdout.close()
            status = sender.wait(timeout=60)
        except BaseException:
            sender.kill()
            raise
    if result.returncode or status:
        raise RuntimeError('Artifact transfer incomplete; inspect private logs')


def artifact_builder(plan, worker):
    name = plan['artifacts'][worker['facts']['arch']]['builder']
    return plan['manager'] if name == 'manager' else plan['workers'][int(name.split('-')[1])]


def transfer_artifacts(builder, worker, plan, log):
    root = root_path(plan)
    remote(connection(worker), 'sudo -n install -d -m 0700 ' + shlex.quote(root + '/input/artifacts'))
    source = ssh(connection(builder), 'sudo -n tar -cf - -C ' + shlex.quote(root + '/work/artifacts') + ' .')
    destination = ssh(connection(worker), 'sudo -n tar -xf - -C ' + shlex.quote(root + '/input/artifacts'))
    with subprocess.Popen(source, stdout=subprocess.PIPE, stderr=log) as sender:
        try:
            result = subprocess.run(destination, stdin=sender.stdout, stdout=log, stderr=log, timeout=3600, check=False)
            sender.stdout.close()
            status = sender.wait(timeout=60)
        except BaseException:
            sender.kill()
            raise
    if status or result.returncode:
        raise RuntimeError('Architecture-specific artifact transfer incomplete')


def run_step(state, name, action):
    file = state / 'journal.json'
    journal = json.loads(file.read_text()) if file.exists() else {}
    previous = journal.get(name, {}).get('state')
    if previous == 'completed':
        return
    if previous in ('running', 'unknown'):
        raise RuntimeError(name + ': prior result unknown; inspect remote state before recovery; no automatic replay')
    journal[name] = {'state': 'running', 'started': int(time.time())}
    save(file, journal)
    print('Running ' + name, flush=True)
    try:
        action()
    except ExternalGate:
        journal[name]['state'] = 'blocked'
        save(file, journal)
        raise
    except BaseException:
        journal[name]['state'] = 'unknown'
        save(file, journal)
        raise
    journal[name].update(state='completed', finished=int(time.time()))
    save(file, journal)


def apply(plan, state):
    identity = digest(plan)
    binding = state / 'binding.json'
    if binding.exists() and json.loads(binding.read_text()) != {'plan': identity}:
        raise ValueError('Local state belongs to another plan')
    save(state / 'result.json', {'ready': False, 'state': 'checking', 'plan_digest': identity,
                                'checked_at': int(time.time())})
    if plan.get('execution') == 'observe-existing':
        import audit
        result = audit.audit(plan, state)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result['checks_passed']:
            raise RuntimeError('Existing-cluster audit has failed/unverified checks or protected resources changed; no installation was attempted')
        return
    # Always recheck the trusted connection and physical identity, including after resume.
    current_nodes = []
    for node in [plan['manager'], *plan['workers']]:
        current = probe(connection(node))
        if any(current[k] != node['facts'][k] for k in ('machine_id', 'host_key', 'arch', 'kernel')):
            raise ValueError('Machine identity/architecture/kernel changed on ' + node['ssh'])
        current_nodes.append(current)
    journal_file = state / 'journal.json'
    journal = json.loads(journal_file.read_text()) if journal_file.exists() else {}
    install_started = any(name in journal for name in ('fetch-manager', 'prepare-manager', 'build', 'management', 'control-plane', 'join'))
    if not install_started:
        if time.time() - plan['created'] > 3600:
            raise ValueError('Plan older than one hour; discover fresh facts before starting')
        fresh_nodes = []
        for node, current in zip([plan['manager'], *plan['workers']], current_nodes):
            fresh_nodes.append(node_info(node, current, 'manager' if node == plan['manager'] else 'worker',
                                         network_mode=plan.get('network_mode', 'lan')))
        select_networks(fresh_nodes, plan['networks'])
        save(binding, {'plan': identity})
    log_file = state / 'execution.log'
    fd = os.open(log_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'ab', buffering=0) as log:
        manager = plan['manager']
        run_step(state, 'upload-manager', lambda: upload_runtime(manager, plan))
        manager_steps = ('fetch-manager', 'prepare-manager', 'load-manager') if plan.get('artifact_mode', 'release') == 'release' else ('build',)
        for name in (*manager_steps, 'management', 'control-plane', 'handoff'):
            run_step(state, name, lambda name=name: phase(manager, plan, name, log))
        for i, worker in enumerate(plan['workers']):
            run_step(state, f'upload-worker-{i}', lambda worker=worker: upload_runtime(worker, plan))
            run_step(state, f'transfer-worker-{i}', lambda worker=worker: transfer(manager, worker, plan, log))
            if worker['kind'] == 'baremetal':
                run_step(state, f'protect-worker-{i}', lambda worker=worker: phase(worker, plan, 'protect', log))
        for arch, source in plan.get('artifacts', {}).items():
            if source['builder'] != 'manager':
                builder = plan['workers'][int(source['builder'].split('-')[1])]
                source_mode = plan.get('artifact_mode', 'release') == 'source'
                step = ('build-arch-' if source_mode else 'fetch-arch-') + arch
                action = 'node-assets' if source_mode else 'fetch-node'
                run_step(state, step, lambda builder=builder, action=action: phase(builder, plan, action, log))
        if plan.get('network_mode') == 'tailscale':
            for i, worker in enumerate(plan['workers']):
                run_step(state, f'tailscale-worker-{i}', lambda worker=worker: phase(worker, plan, 'tailscale-worker', log))
        for i, worker in enumerate(plan['workers']):
            builder = artifact_builder(plan, worker)
            if builder != worker:
                run_step(state, f'artifacts-worker-{i}', lambda builder=builder, worker=worker: transfer_artifacts(builder, worker, plan, log))
            run_step(state, f'prepare-worker-{i}', lambda worker=worker: phase(worker, plan, 'worker', log))
            if worker['kind'] == 'baremetal':
                phase(worker, plan, 'check-protected', log)
        run_step(state, 'join', lambda: phase(manager, plan, 'join', log))
        # Verification always runs afresh; completed install checkpoints are not a health claim.
        phase(manager, plan, 'verify', log)
        for worker in plan['workers']:
            if worker['kind'] == 'baremetal':
                phase(worker, plan, 'check-protected', log)
    raw = remote(connection(manager), 'sudo -n cat ' + shlex.quote(root_path(plan) + '/result.json'))
    result = json.loads(raw)
    if result.get('plan_digest') != identity or result.get('ready') is not True:
        raise ValueError('Verification result does not match this plan')
    save(state / 'result.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('plan', help='Read-only remote discovery and local plan creation')
    p.add_argument('--connections', required=True)
    p.add_argument('--output', required=True)
    p = commands.add_parser('apply', help='Execute the exact reviewed plan; changes target machines')
    p.add_argument('--plan', required=True)
    p.add_argument('--digest', required=True)
    p.add_argument('--state-directory', required=True)
    p = commands.add_parser('status', help='Read local progress; does not check current cluster health')
    p.add_argument('--state-directory', required=True)
    args = parser.parse_args(argv)
    if args.command == 'plan':
        output = Path(args.output).absolute()
        private_dir(output.parent)
        if output.exists():
            raise ValueError('Plan output already exists; choose a new file')
        plan = make_plan(json.loads(Path(args.connections).read_text()))
        save(output, plan)
        print(json.dumps({'plan': str(output), 'digest': digest(plan), 'changes': plan['changes'],
                          'networks': plan['networks'], 'workload_cluster': plan['workload_cluster'],
                          'machines': len(plan['workers']) + 1}, indent=2))
    elif args.command == 'apply':
        plan = checked_plan(args.plan, args.digest)
        state = private_dir(args.state_directory)
        with open(state / 'lock', 'a', encoding='utf-8') as lock:
            os.chmod(state / 'lock', 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            apply(plan, state)
    else:
        path = Path(args.state_directory) / 'journal.json'
        result = path.parent / 'result.json'
        print(json.dumps({'steps': json.loads(path.read_text()) if path.exists() else {},
                          'verification': json.loads(result.read_text()) if result.exists() else None,
                          'health': 'last observation only; not a live check'}, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.SubprocessError) as error:
        print('Error: ' + str(error), file=sys.stderr)
        sys.exit(1)
