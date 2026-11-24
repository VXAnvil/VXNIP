"""Restore narrowly scoped Docker forwarding for OVN local-gateway Pod egress."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time


def run(args):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, timeout=15).strip()


def discover(plan, call=run, which=shutil.which):
    network = ipaddress.IPv4Network(plan['networks']['pod'])
    addresses = json.loads(call(['ip', '-j', '-4', 'address', 'show', 'dev', 'ovn-k8s-mp0']))
    subnets = {str(ipaddress.IPv4Interface(a['local'] + '/' + str(a['prefixlen'])).network)
               for interface in addresses for a in interface.get('addr_info', [])
               if a.get('family') == 'inet' and ipaddress.IPv4Address(a['local']) in network}
    if len(subnets) != 1:
        raise RuntimeError('Waiting for one OVN management-port subnet within the planned Pod network')
    subnet = subnets.pop()
    if not ipaddress.IPv4Network(subnet).subnet_of(network):
        raise ValueError('OVN management subnet exceeds the planned Pod network')
    routes = json.loads(call(['ip', '-j', '-4', 'route', 'get', '1.1.1.1']))
    if len(routes) != 1:
        raise RuntimeError('Ambiguous host egress route')
    uplink = routes[0].get('dev', '')
    if (not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', uplink)
            or uplink in ('lo', 'br-ex', 'ovn-k8s-mp0') or routes[0].get('type') in ('blackhole', 'unreachable', 'prohibit')):
        raise RuntimeError('Host egress route must leave the OVN local gateway')
    backends = {}
    for name in ('iptables', 'iptables-nft', 'iptables-legacy'):
        binary = which(name)
        if not binary:
            continue
        try:
            forward = call([binary, '-w', '5', '-S', 'FORWARD'])
            if '-j DOCKER-USER' not in forward:
                continue
            rules = call([binary, '-w', '5', '-S', 'DOCKER-USER'])
            # Do not put an allow rule ahead of an administrator's deny/custom chain.
            for line in rules.splitlines():
                if line.startswith('-A ') and not re.search(r' -j (ACCEPT|RETURN)$', line):
                    raise ValueError('DOCKER-USER contains a deny/custom policy; inspect before permitting Pod egress')
            backends.setdefault(call([binary, '--version']), binary)
        except subprocess.CalledProcessError:
            continue
    if len(backends) != 1:
        raise RuntimeError('Expected exactly one active Docker iptables backend')
    backend = next(iter(backends.values()))
    comment = plan['site'] + '-pod-egress'
    rules = [
        ['-i', 'ovn-k8s-mp0', '-o', uplink, '-s', subnet, '-m', 'conntrack',
         '--ctstate', 'NEW,ESTABLISHED,RELATED', '-m', 'comment', '--comment', comment, '-j', 'ACCEPT'],
        ['-i', uplink, '-o', 'ovn-k8s-mp0', '-d', subnet, '-m', 'conntrack',
         '--ctstate', 'ESTABLISHED,RELATED', '-m', 'comment', '--comment', comment, '-j', 'ACCEPT']]
    return {'backend': backend, 'rules': rules, 'pod_subnet': subnet, 'uplink': uplink}


def ensure(plan, previous=None, check=False, call=run, which=shutil.which):
    current = discover(plan, call, which)
    binary = current['backend']
    for rule in current['rules']:
        try:
            call([binary, '-w', '5', '-C', 'DOCKER-USER', *rule])
        except subprocess.CalledProcessError as error:
            if check or error.returncode != 1:
                raise
            call([binary, '-w', '5', '-I', 'DOCKER-USER', '1', *rule])
            call([binary, '-w', '5', '-C', 'DOCKER-USER', *rule])
    # After a local subnet/uplink change, remove only this process's exact old rules.
    if previous and previous != current and not check:
        for rule in previous['rules']:
            if previous['backend'] == binary and rule in current['rules']:
                continue
            try:
                call([previous['backend'], '-w', '5', '-C', 'DOCKER-USER', *rule])
            except subprocess.CalledProcessError as error:
                if error.returncode != 1:
                    raise
            else:
                call([previous['backend'], '-w', '5', '-D', 'DOCKER-USER', *rule])
    return current


def validate(settings):
    plan, node = settings['plan'], settings['node']
    if os.geteuid() != 0 or plan['mode'] != 'create' or node['kind'] != 'baremetal':
        raise ValueError('Egress recovery requires a planned bare-metal worker as root')
    identity = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if (Path(settings['root']) / 'plan.digest').read_text() != identity:
        raise ValueError('Egress recovery plan identity differs')
    machine = hashlib.sha256(('vxnip:' + Path('/etc/machine-id').read_text().strip()).encode()).hexdigest()
    if machine != node['facts']['machine_id']:
        raise ValueError('Egress recovery physical host identity differs')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('watch', 'check'))
    parser.add_argument('settings')
    args = parser.parse_args()
    settings = json.loads(Path(args.settings).read_text())
    validate(settings)
    state = Path(settings['root']) / 'egress-rules.json'
    previous = json.loads(state.read_text()) if state.exists() else None
    last_error = None
    deadline = time.monotonic() + 180
    while True:
        try:
            current = ensure(settings['plan'], previous, check=args.action == 'check')
            if args.action == 'check':
                print(json.dumps(current)); return
            if current != previous:
                temporary = state.with_suffix('.next')
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w') as output:
                    json.dump(current, output)
                os.replace(temporary, state)
                print('Scoped OVN Pod egress forwarding ready: ' + current['pod_subnet'] + ' via ' + current['uplink'], flush=True)
            previous, last_error = current, None
        except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
            if args.action == 'check' and time.monotonic() >= deadline:
                raise RuntimeError('Scoped Pod forwarding is not ready; inspect the egress recovery service') from error
            message = str(error) if isinstance(error, (RuntimeError, ValueError)) else 'Network interface/firewall not ready'
            if message != last_error:
                print(message, flush=True)
            last_error = message
        time.sleep(5)


if __name__ == '__main__':
    main()
