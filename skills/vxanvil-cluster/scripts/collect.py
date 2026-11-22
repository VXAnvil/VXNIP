"""Remote read-only collector; exports whitelisted facts, never auth material."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import time


def call(args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=45, check=False)
    if result.returncode:
        label = args[args.index('get')+1] if 'get' in args else 'configuration'
        raise ValueError('Read-only command failed: ' + args[0] + ' ' + label)
    return result.stdout.strip()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def snapshot(settings):
    existing = settings['existing']
    manager = ['kubectl', '--kubeconfig', existing['management_kubeconfig'], '--request-timeout=20s']
    workload = ['kubectl', '--kubeconfig', existing['workload_kubeconfig'], '--request-timeout=20s']

    def obj(prefix, *args):
        return json.loads(call(prefix + list(args) + ['-o', 'json']))

    for prefix in (manager, workload):
        config = json.loads(call(prefix + ['config', 'view', '--minify', '-o', 'json']))
        cluster = config['clusters'][0]['cluster']
        if cluster.get('insecure-skip-tls-verify') or not (cluster.get('certificate-authority') or cluster.get('certificate-authority-data')):
            raise ValueError('Existing kubeconfig must validate API certificates')
    api = call(workload + ['config', 'view', '--minify', '-o', 'jsonpath={.clusters[0].cluster.server}'])
    if api != existing['api'] or call(workload + ['get', '--raw=/readyz']) != 'ok':
        raise ValueError('Unexpected or unready existing workload API')
    hosts = obj(manager, 'get', 'vxanvilhosts')['items']
    machines = obj(manager, 'get', 'vxanvilmachines.infrastructure.cluster.x-k8s.io', '-A')['items']
    parents = obj(manager, 'get', 'machines.cluster.x-k8s.io', '-A')['items']
    nodes = obj(workload, 'get', 'nodes')['items']
    context = {'__name__': 'included_verifier'}
    exec(compile(base64.b64decode(settings['verifier']).decode(), 'verify.py', 'exec'), context)
    bindings = []
    expected = [(existing['api'].split('//')[1].split(':')[0], None)] + [(n['address'], n['machine_id']) for n in settings['workers']]
    for address, identity in expected:
        h = [h for h in hosts if h['spec']['nodeIP'] == address]
        if len(h) != 1:
            raise ValueError('Expected exactly one existing Host for ' + address)
        host = h[0]
        claim = host['status'].get('claim') or {}
        m = [m for m in machines if all(m['metadata'].get(k) == claim.get(k) for k in ('name', 'namespace', 'uid'))]
        if len(m) != 1:
            raise ValueError('Existing Host claim does not identify exactly one Machine')
        machine = m[0]
        owners = [o for o in machine['metadata'].get('ownerReferences', []) if o['kind'] == 'Machine' and o['apiVersion'].startswith('cluster.x-k8s.io/')]
        p = [p for p in parents if owners and p['metadata']['uid'] == owners[0]['uid'] and p['metadata']['namespace'] == machine['metadata']['namespace']]
        if len(p) != 1 or p[0]['spec']['clusterName'] != existing['cluster']:
            raise ValueError('Existing CAPI Machine belongs to another cluster')
        parent = p[0]
        n = [n for n in nodes if n['metadata']['name'] == parent['status'].get('nodeRef', {}).get('name')]
        if len(n) != 1:
            raise ValueError('Existing CAPI Node reference is unresolved')
        bindings.append(context['validate_binding'](host, machine, parent, n[0], address, identity))
    workloads = []
    for resource in ('deployments', 'statefulsets', 'daemonsets', 'jobs', 'cronjobs'):
        for item in obj(workload, 'get', resource, '-A')['items']:
            workloads.append({'kind': resource, 'ns': item['metadata']['namespace'], 'name': item['metadata']['name'],
                              'uid': item['metadata']['uid'], 'generation': item['metadata']['generation'],
                              'spec_digest': fingerprint(item['spec'])})
    pods = [{'ns': p['metadata']['namespace'], 'name': p['metadata']['name'], 'uid': p['metadata']['uid'],
             'spec_digest': fingerprint(p['spec']), 'node': p['spec'].get('nodeName'),
             'containers': [{'name': c['name'], 'id': c.get('containerID'), 'image': c.get('imageID'), 'restarts': c['restartCount']}
                            for c in p.get('status', {}).get('containerStatuses', [])]}
            for p in obj(workload, 'get', 'pods', '-A')['items']]
    public_ca = call(workload + ['config', 'view', '--raw', '--flatten', '--minify', '-o', 'jsonpath={.clusters[0].cluster.certificate-authority-data}'])
    if not public_ca:
        raise ValueError('Cannot obtain public workload CA')
    protected = {'management_uid': obj(manager, 'get', 'namespace', 'kube-system')['metadata']['uid'],
                 'ovn_mtu': call(workload + ['-n', 'ovn-kubernetes', 'get', 'configmap', 'ovn-config', '-o', 'jsonpath={.data.mtu}']),
                 'nodes': sorted([{'uid': n['metadata']['uid'], 'providerID': n['spec'].get('providerID'), 'bootID': n['status']['nodeInfo']['bootID']} for n in nodes], key=lambda n: n['uid']),
                 'hosts': sorted([{'uid': h['metadata']['uid'], 'spec_digest': fingerprint(h['spec']), 'claim': h.get('status', {}).get('claim')} for h in hosts], key=lambda h: h['uid']),
                 'machines': sorted([{'uid': m['metadata']['uid'], 'spec_digest': fingerprint(m['spec'])} for m in machines], key=lambda m: m['uid']),
                 'workloads': sorted(workloads, key=lambda w: w['uid']), 'pods': sorted(pods, key=lambda p: p['uid'])}
    return {'checked_at': int(time.time()), 'api': api, 'bindings': bindings,
            'protected': protected, 'protected_digest': fingerprint(protected), 'public_ca': public_ca,
            'coverage': {'node_identity': True, 'node_ref_uid': all(b['node_ref_uid_verified'] for b in bindings),
                         'tls_api': True, 'new_pod_http': False, 'service_dns_http': False}}
