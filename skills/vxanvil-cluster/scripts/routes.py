"""Keep only the planned LAN workload API /32 route across NVKE network changes."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import time


def ensure(plan, node, run=None):
    if plan['mode'] != 'create' or plan['network_mode'] != 'lan':
        raise ValueError('LAN route recovery requires a create/LAN plan')
    address = str(ipaddress.IPv4Address(node['address']))
    gateway = str(ipaddress.IPv4Address(plan['manager']['address']))
    subnet = ipaddress.IPv4Network(node['subnet'])
    if ipaddress.IPv4Address(address) not in subnet or ipaddress.IPv4Address(gateway) not in subnet:
        raise ValueError('LAN API gateway must share the selected node subnet')
    api = str(ipaddress.IPv4Network(plan['networks']['docker']).network_address + 20)
    run = run or subprocess.check_output
    interfaces = json.loads(run(['ip','-j','-4','address','show'], text=True))
    matches = [i['ifname'] for i in interfaces if any(a.get('local') == address for a in i.get('addr_info', []))]
    if len(matches) != 1 or not re.fullmatch(r'[A-Za-z0-9_.-]+', matches[0]):
        raise RuntimeError('Waiting for the planned node address on one interface')
    iface = matches[0]
    try:
        routes = json.loads(run(['ip','-j','-4','route','get',api], text=True))
    except subprocess.CalledProcessError:
        routes = []
    if any(r.get('dev') == iface and r.get('gateway') == gateway and r.get('prefsrc') == address for r in routes):
        return False
    run(['ip','-4','route','replace',api+'/32','via',gateway,'dev',iface,'src',address], text=True)
    return True


def watch(settings):
    if os.geteuid() != 0:
        raise ValueError('Run through the root-owned bootstrap service')
    last_error = None
    while True:
        try:
            if ensure(settings['plan'], settings['node']):
                print('Restored the planned LAN workload API /32 route', flush=True)
            last_error = None
        except (RuntimeError, subprocess.CalledProcessError) as error:
            if type(error).__name__ != last_error:
                print('Waiting for the planned LAN interface/route', flush=True)
                last_error = type(error).__name__
        time.sleep(5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('settings')
    args = parser.parse_args()
    watch(json.loads(Path(args.settings).read_text()))
