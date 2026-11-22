"""Register known SSH-verified hosts and create fixed CPU pools through NVKE."""
import json
import os
from pathlib import Path
import subprocess
import sys


def run(args, data=None):
    return subprocess.run(args, input=data, text=True, capture_output=True, check=True,
                          timeout=180).stdout


def main(settings):
    plan, root = settings['plan'], Path(settings['root'])
    mgmt = ['kubectl', '--kubeconfig', str(root / 'site/management.kubeconfig'), '--request-timeout=20s']
    nvke = ['nvkectl', '--site', plan['site']]
    clusters = json.loads(run([*nvke, 'get', 'clusters', '-o', 'json']))
    clusters = clusters if isinstance(clusters, list) else clusters['clusters']
    matches = [c for c in clusters if c['name'] == plan['workload_cluster']]
    if len(matches) != 1:
        raise ValueError('Expected one workload cluster at the selected site')
    cluster = matches[0]['id']
    for i, worker in enumerate(plan['workers']):
        key = worker['facts']['host_key']
        name = 'host-' + worker['facts']['machine_id'][:32]
        # Never trust a scan of an unknown address; use the key read over the operator's trusted SSH session.
        if not key.startswith('ssh-ed25519 ') or '\n' in key:
            raise ValueError('Invalid verified worker host key')
        known = root / 'site/known-hosts'
        existing = subprocess.run(['ssh-keygen', '-F', worker['address'], '-f', str(known)],
                                  capture_output=True, text=True, check=False)
        if existing.returncode == 0:
            keys = [' '.join(line.split()[1:3]) for line in existing.stdout.splitlines()
                    if line and not line.startswith('#')]
            if key not in keys:
                raise ValueError('Provider SSH host trust conflicts with the verified worker')
        elif existing.returncode == 1:
            with known.open('a') as stream:
                stream.write('\n' + worker['address'] + ' ' + key + '\n')
        else:
            raise ValueError('Unable to read Provider SSH trust')
        spec = {'machineID': worker['facts']['machine_id'], 'nodeIP': worker['address'],
                'sshTarget': worker['user'] + '@' + worker['address'], 'allowBootstrap': True}
        # A fresh bootstrap never adopts or patches existing inventory with the same name.
        host = json.loads(run([*mgmt, 'get', 'vxniphost', name, '--ignore-not-found', '-o', 'json']) or 'null')
        if host is not None:
            raise ValueError('Host inventory already exists; inspect ownership, do not automatically adopt it')
        obj = {'apiVersion': 'vxnip.infrastructure.cluster.x-k8s.io/v1alpha1', 'kind': 'VXNIPHost',
               'metadata': {'name': name, 'labels': {'vxnip.nvke.dev/bootstrap': plan['site']}}, 'spec': spec}
        run([*mgmt, 'create', '-f', '-'], json.dumps(obj))
        run([*mgmt, 'wait', '--for=jsonpath={.status.discoveryReady}=true', 'vxniphost/' + name,
             '--timeout=120s'])
        host = json.loads(run([*mgmt, 'get', 'vxniphost', name, '-o', 'json']))
        if (any(host['spec'].get(key) != value for key, value in spec.items())
                or host['status'].get('claim')
                or host['status'].get('observedGeneration') != host['metadata']['generation']):
            raise ValueError('Host identity or claim differs before allocation')
        pool = {'clusterID': cluster, 'name': 'cpu-' + str(i), 'nodeNamePrefix': 'cpu' + str(i),
                'desiredNodeCount': 1, 'systemReservedMemory': '512Mi', 'kubeReservedMemory': '256Mi',
                'nodeLabels': {'vxnip.nvke.dev/gateway': 'local'}, 'autoModeDisabled': True,
                'provisioningMonitorDisabled': True,
                'infra': {'machineTemplate': {'apiVersion': 'infrastructure.cluster.x-k8s.io/v1alpha1',
                          'kind': 'VXNIPMachineTemplate', 'spec': {'template': {'spec': {
                              'hostRef': name, 'bootstrapMode': 'NVKE'}}}}}}
        path = root / 'tenant' / ('cpu-' + str(i) + '.json')
        path.write_text(json.dumps(pool))
        os.chmod(path, 0o600)
        run([*nvke, 'create', 'nodepool', '-f', str(path)])


if __name__ == '__main__':
    main(json.loads(Path(sys.argv[1]).read_text()))
