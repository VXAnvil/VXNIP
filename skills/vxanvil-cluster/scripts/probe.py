"""Read-only facts; unavailable privileged observations remain explicitly unknown."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess


def facts():
    unknown = []

    def command(args, optional=False):
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
            if result.returncode == 0:
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        if not optional:
            unknown.append('command:' + args[0])
        return ''

    def present(path):
        try:
            os.stat(path)
            return True
        except FileNotFoundError:
            return False
        except PermissionError:
            unknown.append('path:' + path)
            return None

    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    mid = Path('/etc/machine-id').read_text().strip()
    if not re.fullmatch('[0-9a-f]{32}', mid) or int(mid, 16) == 0:
        raise ValueError('A unique initialized machine-id is required')
    mem = {line.split(':')[0]: int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.split()[1].isdigit()}
    kernel = os.uname().release
    containers, networks = [], []
    if shutil.which('docker'):
        raw = command(['docker', 'ps', '-aq'])
        containers = raw.split()
        ids = command(['docker', 'network', 'ls', '-q']).split()
        if ids:
            for network in json.loads(command(['docker', 'network', 'inspect', *ids]) or '[]'):
                networks.extend(c['Subnet'] for c in (network['IPAM'].get('Config') or []) if c.get('Subnet') and ':' not in c['Subnet'])
    cloud = command(['cloud-init', 'status', '--format', 'json'], optional=True)
    # cloud-init uses exit 2 for completed boots with deprecation warnings.
    if not cloud and shutil.which('cloud-init'):
        cloud = subprocess.run(['cloud-init', 'status', '--format', 'json'], capture_output=True,
                               text=True, timeout=30, check=False).stdout
    try:
        cloud = json.loads(cloud or '{}')
    except ValueError:
        cloud = {}
        unknown.append('cloud-init')
    blocks = json.loads(command(['lsblk', '-J', '-o', 'NAME,TYPE,LABEL']) or '{"blockdevices":[]}')['blockdevices']

    def seeds(devices):
        return [d['name'] for d in devices if (d.get('label') or '').lower() in ('cidata', 'config-2', 'configdrive')] + [n for d in devices for n in seeds(d.get('children', []))]

    allocations = [present(p) for p in ('/etc/kubernetes/kubelet.conf', '/etc/kubernetes/bootstrap-kubelet.conf',
                   '/var/lib/vxanvil-kubeadm/current.json', '/var/lib/homelab-kubeadm/current.json', '/etc/vxanvil-nvke-enrollment')]
    try:
        cni = any(p.is_file() for p in Path('/etc/cni/net.d').iterdir() if p.name != '.kubernetes-cni-keep')
    except FileNotFoundError:
        cni = False
    except PermissionError:
        cni = None
        unknown.append('cni')
    preferences = command(['tailscale', 'debug', 'prefs'], optional=True) if shutil.which('tailscale') else ''
    try:
        prefs = json.loads(preferences) if preferences else None
    except ValueError:
        prefs = None
    tailscale = None if prefs is None else {k: prefs.get(k) for k in ('AdvertiseRoutes', 'NoSNAT', 'RouteAll')}
    raw_status = command(['tailscale', 'status', '--json'], optional=True) if shutil.which('tailscale') else ''
    try:
        tailscale_state = json.loads(raw_status).get('BackendState') if raw_status else None
    except ValueError:
        tailscale_state = None
    services = {unit: command(['systemctl', 'show', unit, '-p', 'MainPID', '-p', 'ActiveState', '-p', 'NRestarts'], optional=True)
                for unit in ('docker.service', 'containerd.service', 'spire-agent.service', 'nvke-agent.service', 'kubelet.service')}
    driver = Path('/proc/driver/nvidia/version')
    return {'machine_id': hashlib.sha256(('vxanvil:' + mid).encode()).hexdigest(), 'hostname': os.uname().nodename,
            'os': release.get('ID', '').strip('"'), 'version': release.get('VERSION_ID', '').strip('"'),
            'arch': command(['dpkg', '--print-architecture']), 'cpus': os.cpu_count(), 'memory': mem['MemTotal'],
            'available_memory': mem['MemAvailable'], 'free_disk': shutil.disk_usage('/var/lib').free,
            'swap': bool(Path('/proc/swaps').read_text().splitlines()[1:]),
            'virtualization': command(['systemd-detect-virt', '--vm'], optional=True),
            'container': command(['systemd-detect-virt', '--container'], optional=True),
            'addresses': json.loads(command(['ip', '-j', '-4', 'address', 'show']) or '[]'),
            'routes': json.loads(command(['ip', '-j', '-4', 'route', 'show', 'table', 'all']) or '[]'),
            'default_route': json.loads(command(['ip', '-j', '-4', 'route', 'get', '1.1.1.1']) or '[]'),
            'allocated': True if True in allocations else (None if None in allocations else False), 'existing_cni': cni,
            'seed_media': seeds(blocks), 'host_key': ' '.join(Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().split()[:2]),
            'users': [u.pw_name for u in pwd.getpwall() if u.pw_dir.startswith('/home/')],
            'kernel': kernel, 'kernel_config': present('/boot/config-' + kernel), 'cloud_init': cloud,
            'containers': containers, 'docker_networks': networks, 'tailscale': tailscale,
            'tailscale_state': tailscale_state,
            'driver': hashlib.sha256(driver.read_bytes()).hexdigest() if driver.exists() else None,
            'services': services, 'containerd_version': command(['/usr/bin/containerd', '--version'], optional=True),
            'shared_cri': present('/run/containerd/containerd.sock'), 'privileged': os.geteuid() == 0,
            'missing_baremetal_tools': [tool for tool in ('ctr', 'visudo', 'ss') if not shutil.which(tool)],
            'prepared_runtime': present('/etc/systemd/system/vxanvil-nvke-containerd.service'),
            'unknown': unknown}


if __name__ == '__main__':
    print(json.dumps(facts(), sort_keys=True))
