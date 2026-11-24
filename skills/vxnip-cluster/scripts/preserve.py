"""Snapshot shared host resources without exporting container configuration."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def run(args):
    return subprocess.check_output(args, text=True, timeout=30).strip()


def snapshot():
    ids = run(['docker', 'ps', '-aq']).split()
    containers = []
    if ids:
        for item in json.loads(run(['docker', 'inspect', *ids])):
            containers.append({'id': item['Id'], 'image': item['Image'], 'running': item['State']['Running'],
                               'pid': item['State']['Pid'], 'started': item['State']['StartedAt'],
                               'restarts': item['RestartCount'], 'config_digest': hashlib.sha256(json.dumps(
                                   {'config': item['Config'], 'host': item['HostConfig']}, sort_keys=True).encode()).hexdigest()})
    socket = os.stat('/run/containerd/containerd.sock')
    driver = Path('/proc/driver/nvidia/version')
    return {'containers': sorted(containers, key=lambda item: item['id']),
            'services': {name: run(['systemctl', 'show', name, '-p', 'MainPID', '-p', 'ActiveState', '-p', 'NRestarts'])
                         for name in ('docker.service', 'containerd.service')},
            'socket': [socket.st_dev, socket.st_ino], 'default_route': run(['ip', '-4', 'route', 'show', 'default']),
            'driver': hashlib.sha256(driver.read_bytes()).hexdigest() if driver.exists() else None,
            'shared_configuration': {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() if Path(name).is_file() else None
                                     for name in ('/etc/containerd/config.toml', '/etc/nvidia-container-runtime/config.toml')}}


def preserved(before, after):
    # New ephemeral build containers may already have been removed; original ones must be identical.
    current = {c['id']: c for c in after['containers']}
    return all(current.get(c['id']) == c for c in before['containers']) and all(
        before[key] == after.get(key) for key in before if key != 'containers')


if __name__ == '__main__':
    mode, path = sys.argv[1], Path(sys.argv[2])
    value = snapshot()
    if mode == 'save':
        if path.exists():
            raise SystemExit('A shared-host baseline already exists')
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    elif mode == 'check':
        if not preserved(json.loads(path.read_text()), value):
            raise SystemExit('Protected Docker/runtime/driver/default route changed; stop and inspect')
    else:
        raise SystemExit('Unknown preservation action')
