"""Preserve existing clusters: read-only identity/API/network audit and snapshot comparison."""
import base64
import json
from pathlib import Path
import shlex

import cluster


def collect(plan):
    settings = {'existing': plan['existing'], 'workers': [{'address': w['address'], 'machine_id': w['facts']['machine_id']} for w in plan['workers']],
                'verifier': base64.b64encode((cluster.HERE / 'verify.py').read_bytes()).decode()}
    program = (cluster.HERE / 'collect.py').read_text() + '''
import sys
try:
    value=snapshot(json.loads(base64.b64decode(sys.argv[1])))
except (ValueError,KeyError,subprocess.SubprocessError) as error:
    value={'collector_error':type(error).__name__,'reason':str(error) if isinstance(error,ValueError) else 'Required API data unavailable or timed out'}
print(json.dumps(value))
'''
    value = json.loads(cluster.remote(cluster.connection(plan['manager']), 'python3 -c ' + shlex.quote(program) + ' '
                                     + shlex.quote(base64.b64encode(cluster.canonical(settings)).decode()), timeout=240))
    if 'collector_error' in value:
        raise RuntimeError('Read-only collector blocked: ' + value['reason'])
    return value


def network_probe(node, api, spire, ca, worker_address=None):
    settings = {'api': api, 'spire': spire, 'ca': ca, 'worker': worker_address}
    # No files, Pods, service changes, Tailscale preference updates or privileged commands.
    code = '''import base64,json,socket,ssl,subprocess,sys,urllib.request
s=json.loads(base64.b64decode(sys.argv[1])); result={}
def tcp(host,port):
    try:
        with socket.create_connection((host,port),timeout=5): return True
    except OSError: return False
if s['worker']:
    result['agent_tcp']=tcp(s['worker'],15005); result['kubelet_tcp']=tcp(s['worker'],10250)
else:
    result['spire_tcp']=tcp(s['spire'],30441)
    try:
        ctx=ssl.create_default_context(cadata=base64.b64decode(s['ca']).decode())
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=ctx))
        with opener.open(s['api']+'/readyz',timeout=10) as response:
            result['api_tls']=response.status==200 and response.read(64).strip()==b'ok'
    except (OSError,ValueError): result['api_tls']=False
    result['api_route']=json.loads(subprocess.check_output(['ip','-j','-4','route','get',s['api'].split('//')[1].split(':')[0]],text=True))
peer=s['worker'] or s['api'].split('//')[1].split(':')[0]
result['peer_route']=json.loads(subprocess.check_output(['ip','-j','-4','route','get',peer],text=True))
dev=result['peer_route'][0]['dev']
link=json.loads(subprocess.check_output(['ip','-j','link','show','dev',dev],text=True))[0]
result['interface_mtu']=link['mtu']; result['pod_mtu_budget']=link['mtu']-100
print(json.dumps(result))
'''
    return json.loads(cluster.remote(cluster.connection(node), 'python3 -c ' + shlex.quote(code) + ' '
                                    + shlex.quote(base64.b64encode(cluster.canonical(settings)).decode()), timeout=60))


def audit(plan, state):
    if plan['execution'] != 'observe-existing':
        raise ValueError('Read-only audit requires an explicit existing-cluster plan')
    protected_hosts = []
    for node in [plan['manager'], *plan['workers']]:
        current = cluster.probe(cluster.connection(node), require_root=False)
        if any(current[k] != node['facts'][k] for k in ('machine_id', 'host_key', 'arch', 'kernel')):
            raise ValueError('Existing machine identity changed; do not reuse the old plan')
        protected_hosts.append({'node': node, 'facts': current})
    print('Checking existing allocation and workload baseline', flush=True)
    before = collect(plan)
    cluster.save(state / 'before.json', before)
    network = []
    for worker in plan['workers']:
        print('Checking existing network paths for ' + worker['ssh'], flush=True)
        from_manager = network_probe(plan['manager'], before['api'], plan['existing']['spire_address'], before['public_ca'], worker['address'])
        from_worker = network_probe(worker, before['api'], plan['existing']['spire_address'], before['public_ca'])
        network.append({'worker': worker['ssh'], 'manager': from_manager, 'worker_path': from_worker})
    print('Comparing protected workloads and host services', flush=True)
    after = collect(plan)
    cluster.save(state / 'after.json', after)
    host_unchanged = True
    for item in protected_hosts:
        current = cluster.probe(cluster.connection(item['node']), require_root=False)
        host_unchanged &= all(current.get(k) == item['facts'].get(k) for k in ('machine_id', 'kernel', 'driver', 'services', 'tailscale', 'containers'))
    unchanged = before['protected'] == after['protected'] and host_unchanged
    paths_ok = all(item['manager'].get('agent_tcp') and item['manager'].get('kubelet_tcp') and
                   item['worker_path'].get('spire_tcp') and item['worker_path'].get('api_tls') for item in network)
    routes_ok = plan['network_mode'] != 'tailscale' or all(
        any(r.get('dev') == 'tailscale0' for r in item['worker_path']['api_route']) and
        any(r.get('dev') == 'tailscale0' for r in item['manager']['peer_route']) for item in network)
    mtu = int(after['protected']['ovn_mtu'])
    mtu_ok = all(mtu <= path['pod_mtu_budget'] for item in network for path in (item['manager'], item['worker_path']))
    preferences = protected_hosts[0]['facts'].get('tailscale')
    tailscale_ok = plan['network_mode'] != 'tailscale' or bool(preferences and preferences.get('NoSNAT') is True and all(
        (item['facts'].get('tailscale') or {}).get('RouteAll') is True for item in protected_hosts[1:]))
    result = {'ready': None, 'checks_passed': bool(unchanged and paths_ok and routes_ok and mtu_ok and tailscale_ok), 'mode': 'existing-readonly',
              'plan_digest': cluster.digest(plan), 'checked_at': after['checked_at'], 'unchanged': unchanged,
              'bindings': after['bindings'], 'network': network, 'coverage': after['coverage'],
              'ovn_mtu': mtu, 'mtu_within_budget': mtu_ok, 'tailscale_preferences_match': tailscale_ok,
              'protection': {'hosts': len(after['protected']['hosts']), 'nodes': len(after['protected']['nodes']),
                             'workloads': len(after['protected']['workloads']), 'pods': len(after['protected']['pods'])},
              'note': 'No fresh Pod/Service probes or installation was performed; existing protection compares observations, not causation.'}
    result['coverage']['host_docker'] = all('command:docker' not in item['facts'].get('unknown', []) for item in protected_hosts)
    result['coverage']['spire_authentication'] = False
    cluster.save(state / 'result.json', result)
    return result
