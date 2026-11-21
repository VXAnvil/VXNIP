"""Render fixed shell values from a reviewed plan; never source user-supplied shell."""
import ipaddress
import json
from pathlib import Path
import shlex
import sys


def values(settings):
    plan = settings['plan']
    root = settings['root']
    networks = plan['networks']
    return {'ROOT': root, 'SITE': plan['site'], 'CLUSTER': plan['workload_cluster'],
            'MANAGER_IP': plan['manager']['address'], 'LAN_CIDR': plan['manager']['subnet'],
            'DOCKER_CIDR': networks['docker'], 'POD_CIDR': networks['pod'],
            'SERVICE_CIDR': networks['service'],
            'CP_IP': str(ipaddress.ip_network(networks['docker']).network_address + 20),
            'WORKER_IP': plan['workers'][0]['address'], 'NODE_IP': settings['node']['address'],
            'WORKER_IPS': ' '.join(w['address'] for w in plan['workers']),
            'NETWORK_MODE': plan.get('network_mode', 'lan'), 'WORKER_KIND': settings['node'].get('kind', 'vm'),
            'WORKER_GATEWAY': plan['manager']['address'], 'WORKER_USER': settings['node']['user']}


if __name__ == '__main__':
    for key, value in values(json.loads(Path(sys.argv[1]).read_text())).items():
        print(f'{key}={shlex.quote(value)}')
