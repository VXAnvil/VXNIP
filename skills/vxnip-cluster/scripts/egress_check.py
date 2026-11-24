"""Fresh egress acceptance on every scheduled workload node."""
import json
import os
from pathlib import Path
import subprocess
import time

IMAGE = 'docker.io/library/python:3.12-slim-bookworm'


def run(args, data=None):
    return subprocess.run(args, input=data, text=True, capture_output=True, timeout=40, check=True).stdout


def verify(settings, bindings):
    root, plan = Path(settings['root']), settings['plan']
    k = ['kubectl', '--kubeconfig', str(root / 'tenant/workload.kubeconfig'), '--request-timeout=15s']
    nodes = json.loads(run(k + ['get', 'nodes', '-o', 'json']))['items']
    byname = {n['metadata']['name']: n for n in nodes}
    for binding in bindings:
        node = byname[binding['node']]
        if node['metadata']['uid'] != binding['node_uid'] or node['spec']['providerID'] != binding['provider_id']:
            raise ValueError('Egress probe Node identity differs')
    created = json.loads(run(k + ['create', '-f', '-', '-o', 'json'], json.dumps({
        'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'generateName': 'vxnip-egress-check-'}})))
    ns, uid = created['metadata']['name'], created['metadata']['uid']
    directory = root / 'verification-logs'
    directory.mkdir(mode=0o700, exist_ok=True)
    record = {'namespace': ns, 'namespace_uid': uid, 'checked_at': int(time.time()), 'results': []}
    log = directory / ('egress-' + str(time.time_ns()) + '.json')
    def save():
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(record, output, indent=2)
    save()
    try:
        run(k + ['-n', ns, 'create', 'configmap', 'egress-check',
                 '--from-file=check.py=' + str(root / 'runtime/egress_probe.py')])
        for i, binding in enumerate(bindings):
            hostname = byname[binding['node']]['metadata']['labels']['kubernetes.io/hostname']
            pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'probe-' + str(i), 'namespace': ns},
                   'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                            'nodeSelector': {'kubernetes.io/hostname': hostname},
                            'tolerations': [
                                {'key': 'vxnip.nvke.dev/experiment', 'operator': 'Equal', 'value': 'true', 'effect': 'NoSchedule'},
                                {'key': 'node-role.kubernetes.io/control-plane', 'operator': 'Exists', 'effect': 'NoSchedule'}],
                            'containers': [{'name': 'egress', 'image': IMAGE,
                                'command': ['python', '-B', '/validation/check.py', json.dumps(plan.get('egress', {}))],
                                'resources': {'requests': {'cpu': '5m', 'memory': '32Mi'}, 'limits': {'cpu': '100m', 'memory': '128Mi'}},
                                'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                                'volumeMounts': [{'name': 'code', 'mountPath': '/validation', 'readOnly': True}]}],
                            'volumes': [{'name': 'code', 'configMap': {'name': 'egress-check'}}]}}
            run(k + ['create', '-f', '-'], json.dumps(pod))
        deadline = time.monotonic() + 240
        for i, binding in enumerate(bindings):
            name = 'probe-' + str(i)
            while True:
                pod = json.loads(run(k + ['-n', ns, 'get', 'pod', name, '-o', 'json']))
                phase = pod['status'].get('phase')
                if phase in ('Succeeded', 'Failed'):
                    break
                if time.monotonic() >= deadline:
                    record['failed_pod'] = pod; save()
                    raise ValueError('Egress Pod timed out; inspect private log ' + str(log))
                time.sleep(2)
            output = json.loads(run(k + ['-n', ns, 'logs', name]))
            record['results'].append({'node': binding['node'], 'node_uid': binding['node_uid'],
                'pod_uid': pod['metadata']['uid'], 'probe': output,
                'image_id': pod['status'].get('containerStatuses', [{}])[0].get('imageID')})
            save()
            if (phase != 'Succeeded' or pod['spec']['nodeName'] != binding['node']
                    or output.get('passed') is not True
                    or any(output.get('checks', {}).get(key, {}).get('passed') is not True
                           for key in ('external_dns', 'numeric_tcp', 'https_verified'))):
                raise ValueError('Pod external DNS/TCP/verified HTTPS failed; inspect private log ' + str(log))
            node = json.loads(run(k + ['get', 'node', binding['node'], '-o', 'json']))
            if node['metadata']['uid'] != binding['node_uid'] or node['spec']['providerID'] != binding['provider_id']:
                raise ValueError('Egress probe Node was replaced during acceptance')
        record['passed'] = True
    finally:
        current = json.loads(run(k + ['get', 'namespace', ns, '-o', 'json']))
        if current['metadata']['uid'] != uid:
            raise ValueError('Egress namespace identity changed; refusing cleanup')
        run(k + ['delete', 'namespace', ns, '--wait=true', '--timeout=30s'])
        record['namespace_removed'] = True
        save()
    return record
