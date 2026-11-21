"""Verify current Host -> infrastructure Machine -> CAPI Machine -> Ready Node."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def validate_binding(host, machine, parent, node, expected_ip, expected_id=None):
    if any(obj['metadata'].get('deletionTimestamp') for obj in (host, machine, parent, node)):
        raise ValueError('Allocation or Node is being deleted')
    if expected_id and host['spec']['machineID'] != expected_id:
        raise ValueError('Host physical identity changed')
    if host['spec']['nodeIP'] != expected_ip:
        raise ValueError('Unexpected Host address')
    if (host['status'].get('discoveryReady') is not True or
            host['status'].get('observedGeneration') != host['metadata']['generation']):
        raise ValueError('Host discovery does not cover current generation')
    claim = host['status'].get('claim') or {}
    if any(claim.get(k) != machine['metadata'].get(k) for k in ('name', 'namespace', 'uid')):
        raise ValueError('Host claim does not identify the infrastructure Machine')
    if machine['spec']['hostRef'] != host['metadata']['name']:
        raise ValueError('Machine references another Host')
    owners = [owner for owner in machine['metadata'].get('ownerReferences', [])
              if owner['kind'] == 'Machine' and owner['apiVersion'].startswith('cluster.x-k8s.io/')]
    if len(owners) != 1 or any(owners[0][k] != parent['metadata'][k] for k in ('name', 'uid')):
        raise ValueError('CAPI Machine owner identity mismatch')
    ref = parent['spec']['infrastructureRef']
    if (ref.get('name') != machine['metadata']['name'] or ref.get('kind') != 'VMetalMachine'
            or ref.get('apiGroup', ref.get('apiVersion', '').split('/')[0]) != 'infrastructure.cluster.x-k8s.io'):
        raise ValueError('CAPI infrastructure reference mismatch')
    provider_id = 'vmetal://' + machine['metadata']['uid']
    if machine['spec'].get('providerID') != provider_id or node['spec'].get('providerID') != provider_id:
        raise ValueError('Node providerID does not match infrastructure allocation UID')
    node_ref = parent['status'].get('nodeRef') or {}
    # CAPI v1beta2 in the pinned NVKE environment publishes a name-only nodeRef.
    # Enforce UID when supplied; otherwise retain the allocation-derived providerID,
    # address, physical Host binding and observed Node UID checks without inventing a UID.
    if node_ref.get('name') != node['metadata']['name'] or ('uid' in node_ref and node_ref['uid'] != node['metadata']['uid']):
        raise ValueError('CAPI nodeRef does not identify the workload Node')
    if not any(c['type'] == 'Ready' and c['status'] == 'True' for c in node['status'].get('conditions', [])):
        raise ValueError('Node is not Ready')
    if not any(a['type'] == 'InternalIP' and a['address'] == expected_ip for a in node['status']['addresses']):
        raise ValueError('Node address differs from the expected machine')
    return {'host': host['metadata']['name'], 'machine': machine['metadata']['name'],
            'node': node['metadata']['name'], 'node_uid': node['metadata']['uid'], 'provider_id': provider_id,
            'node_ref_uid_verified': 'uid' in node_ref}


def run(args):
    return subprocess.run(args, text=True, capture_output=True, check=True, timeout=900).stdout


def run_probe(args, log):
    """Keep probe diagnostics private and available when a subprocess fails."""
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        result = subprocess.run(args, text=True, stdout=output, stderr=subprocess.STDOUT,
                                check=False, timeout=900)
    if result.returncode:
        raise ValueError('Network probe failed; inspect private log ' + str(log))


def main(settings):
    plan, root = settings['plan'], Path(settings['root'])
    workload = root / 'tenant/workload.kubeconfig'
    kube = ['kubectl', '--kubeconfig', str(workload), '--request-timeout=20s']
    mgmt = ['kubectl', '--kubeconfig', str(root / 'site/management.kubeconfig'), '--request-timeout=20s']
    cp_ip = str(ipaddress.ip_network(plan['networks']['docker']).network_address + 20)
    api = 'https://' + cp_ip + ':6443'
    if run([*kube, 'config', 'view', '--minify', '-o', 'jsonpath={.clusters[0].cluster.server}']).strip() != api:
        raise ValueError('Workload API differs from plan')
    run([*kube, 'get', '--raw=/readyz'])
    expected = [(cp_ip, None)] + [(w['address'], w['facts']['machine_id']) for w in plan['workers']]
    evidence = []
    # The bounded poll waits for CAPI nodeRef and current-generation discovery as well as Ready.
    for address, identity in expected:
        last_error = None
        for _ in range(90):
            try:
                hosts = json.loads(run([*mgmt, 'get', 'vmetalhosts', '-o', 'json']))['items']
                hosts = [h for h in hosts if h['spec']['nodeIP'] == address]
                if len(hosts) != 1:
                    raise ValueError('Expected exactly one Host for planned node address')
                host = hosts[0]
                claim = host['status'].get('claim') or {}
                machine = json.loads(run([*mgmt, '-n', claim['namespace'], 'get',
                                          'vmetalmachine.infrastructure.cluster.x-k8s.io', claim['name'], '-o', 'json']))
                owner = next(o for o in machine['metadata']['ownerReferences'] if o['kind'] == 'Machine'
                             and o['apiVersion'].startswith('cluster.x-k8s.io/'))
                parent = json.loads(run([*mgmt, '-n', claim['namespace'], 'get', 'machines.cluster.x-k8s.io',
                                         owner['name'], '-o', 'json']))
                node_name = parent['status']['nodeRef']['name']
                node = json.loads(run([*kube, 'get', 'node', node_name, '-o', 'json']))
                if parent['spec']['clusterName'] != plan['workload_cluster']:
                    raise ValueError('CAPI Machine belongs to another cluster')
                evidence.append(validate_binding(host, machine, parent, node, address, identity))
                break
            except (ValueError, KeyError, StopIteration, subprocess.CalledProcessError) as error:
                last_error = type(error).__name__
                time.sleep(5)
        else:
            raise ValueError('Timed out waiting for verified node binding: ' + str(last_error))
    logs = root / 'verification-logs'
    logs.mkdir(mode=0o700, exist_ok=True)
    for i, worker in enumerate(evidence[1:]):
        stamp = str(time.time_ns())
        run_probe(['bash', str(root / 'runtime/pod-check.sh'), str(workload), api,
                   evidence[0]['node'], worker['node']], logs / ('pod-' + str(i) + '-' + stamp + '.log'))
        run_probe(['bash', str(root / 'runtime/service-check.sh'), str(workload), evidence[0]['node'],
                   worker['node']], logs / ('service-' + str(i) + '-' + stamp + '.log'))
    result = {'ready': True, 'checked_at': int(time.time()), 'site': plan['site'],
              'workload_cluster': plan['workload_cluster'], 'nodes': evidence,
              'api': api, 'network': {'pod_http_bidirectional': True, 'service_dns_http': True},
              'workload_kubeconfig': str(workload),
              'plan_digest': hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    temporary = root / 'result.next'
    temporary.write_text(json.dumps(result, indent=2))
    temporary.chmod(0o600)
    temporary.replace(root / 'result.json')


if __name__ == '__main__':
    main(json.loads(Path(sys.argv[1]).read_text()))
