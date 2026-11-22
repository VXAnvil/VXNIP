#!/usr/bin/env python3
"""Create a dedicated Ubuntu 24.04 VM with a stable Lima user-v2 address."""

import argparse
import ipaddress
import json
import math
import os
from pathlib import Path
import platform
import pwd
import re
import subprocess
import sys
import tempfile

CONFIG = Path("/etc/netplan/50-cloud-init.yaml")
CLOUD_CONFIG = Path("/etc/cloud/cloud.cfg.d/99-vxanvil-preserve-netplan.cfg")
UPLINK = Path("/etc/systemd/network/05-vxanvil-lima-uplink.link")
WAIT_ONLINE = Path("/etc/systemd/system/systemd-networkd-wait-online.service.d/60-vxanvil-lima.conf")
WAIT_FOR_LIMA_UPLINK = """[Service]
ExecStart=
ExecStart=/usr/lib/systemd/systemd-networkd-wait-online --interface=eth0:routable --interface=breth0:routable --any --ipv4
"""
STATE = Path("/var/lib/vxanvil-lima-network.json")
VM_NAME = re.compile(r"[a-z][a-z0-9-]{0,47}")


def run(*args, source=None, capture=True):
    return subprocess.run(args, check=True, text=True, input=source,
                          stdout=subprocess.PIPE if capture else sys.stderr,
                          timeout=300).stdout


def write_file(path, content, mode=0o600):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            temporary.chmod(mode)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def route():
    routes = json.loads(run("ip", "-j", "-4", "route", "show", "default"))
    if len(routes) != 1 or not routes[0].get("gateway"):
        raise ValueError("expected one IPv4 default route through Lima user-v2")
    return routes[0]


def network(expected=None):
    default = route()
    device = default["dev"]
    if (device not in ("eth0", "breth0")
            or Path("/sys/class/net/eth0/device/driver").resolve().name != "virtio_net"):
        raise ValueError("expected the virtio eth0 uplink or its breth0 bridge")
    uplink = json.loads(run("ip", "-j", "link", "show", "dev", "eth0"))[0]
    entries = json.loads(run("ip", "-j", "address", "show", "dev", device))
    addresses = [entry for entry in entries[0]["addr_info"]
                 if entry["family"] == "inet" and entry["scope"] == "global"
                 and (expected is None or entry["local"] == expected)]
    if len(addresses) != 1:
        raise ValueError("expected one IPv4 address on the default route")
    entry = addresses[0]
    return {
        "address": f'{entry["local"]}/{entry["prefixlen"]}',
        "gateway": default["gateway"],
        "metric": default.get("metric", 0),
        "mac": uplink["address"].lower(),
        "device": device,
    }


def configure_guest(plan):
    observed = network()
    if observed != plan or observed["device"] != "eth0":
        raise ValueError("first-boot network differs from the validated Lima lease")
    if list(CONFIG.parent.glob("*.yaml")) != [CONFIG]:
        raise ValueError("unexpected Netplan configuration for a fresh Lima guest")
    if Path("/var/lib/vxanvil-kubeadm/current.json").exists():
        raise ValueError("the VM already has a Provider allocation")
    if any(path.exists() for path in (CLOUD_CONFIG, UPLINK, WAIT_ONLINE, STATE)):
        raise ValueError("the guest already has a managed network configuration")
    dns = run("resolvectl", "dns", "eth0").split(":", 1)[1].split()
    if not dns:
        raise ValueError("the Lima link has no DNS servers")
    dns = [str(ipaddress.ip_address(server)) for server in dns]
    interface = {
        "match": {"driver": "virtio_net", "macaddress": plan["mac"]},
        "set-name": "eth0",
        "dhcp4": False,
        "addresses": [plan["address"]],
        "routes": [{"to": "default", "via": plan["gateway"], "metric": plan["metric"]}],
        "nameservers": {"addresses": dns},
    }
    config = json.dumps({"network": {"version": 2, "ethernets": {"eth0": interface}}}, indent=2) + "\n"
    previous = CONFIG.read_text()
    try:
        pwd.getpwnam("ubuntu")
    except KeyError:
        run("useradd", "-m", "-s", "/bin/bash", "ubuntu")
    write_file(CONFIG, config)
    try:
        run("netplan", "generate")
        # NVKE moves the L3 stanza to breth0. Keep the physical NIC's name
        # tied to its driver even if the agent rewrites the Netplan match.
        write_file(UPLINK, f"[Match]\nDriver=virtio_net\nPermanentMACAddress={plan['mac']}\n\n[Link]\nName=eth0\n", 0o644)
        # Once OVS takes over, eth0 is a layer 2 port without IPv4.
        WAIT_ONLINE.parent.mkdir(parents=True, exist_ok=True)
        write_file(WAIT_ONLINE, WAIT_FOR_LIMA_UPLINK, 0o644)
        write_file(CLOUD_CONFIG, "network: {config: disabled}\n")
        write_file(STATE, json.dumps({**plan, "dns": dns}) + "\n")
    except (OSError, subprocess.SubprocessError):
        write_file(CONFIG, previous)
        UPLINK.unlink(missing_ok=True)
        WAIT_ONLINE.unlink(missing_ok=True)
        CLOUD_CONFIG.unlink(missing_ok=True)
        raise


def guest(action, argument):
    if os.geteuid() != 0 or (platform.system(), platform.machine()) != ("Linux", "aarch64"):
        raise ValueError("expected root in an ARM64 Lima guest")
    if action == "_inspect":
        print(json.dumps(network()))
    elif action == "_configure":
        configure_guest(json.loads(argument))
    elif action == "_verify":
        plan = json.loads(STATE.read_text())
        if (CLOUD_CONFIG.read_text() != "network: {config: disabled}\n" or not UPLINK.is_file()
                or WAIT_ONLINE.read_text() != WAIT_FOR_LIMA_UPLINK):
            raise ValueError("managed boot-time network configuration changed")
        actual = network(str(ipaddress.IPv4Interface(plan["address"]).ip))
        if any(actual[key] != plan[key] for key in ("address", "gateway", "metric", "mac")):
            raise ValueError("guest address, route or MAC changed")
        dns = run("resolvectl", "dns", actual["device"]).split(":", 1)[1].split()
        if any(server not in dns for server in plan["dns"]):
            raise ValueError("guest link lost its configured DNS servers")
        print(json.dumps({**plan, "device": actual["device"]}))


def create():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="new Lima instance name")
    parser.add_argument("--check", action="store_true", help="verify an existing managed VM")
    parser.add_argument("--management-vm", help="existing Lima manager for SSH reachability verification")
    parser.add_argument("--cpus", type=int, default=3)
    parser.add_argument("--memory", type=float, default=3.5, help="guest GiB")
    parser.add_argument("--disk", type=float, default=11, help="guest GiB")
    args = parser.parse_args()
    if (platform.system(), platform.machine()) != ("Darwin", "arm64"):
        parser.error("Apple Silicon macOS is required")
    if any(not VM_NAME.fullmatch(name) for name in (args.name, args.management_vm) if name):
        parser.error("use lowercase Lima instance names")
    if (args.management_vm == args.name or args.cpus < 1
            or any(not math.isfinite(value) or value <= 0 for value in (args.memory, args.disk))):
        parser.error("invalid VM resources or management instance")
    home = Path(os.environ.get("LIMA_HOME", "")).expanduser()
    if not home.is_absolute():
        parser.error("set LIMA_HOME to a dedicated absolute Lima directory")
    manifest = home / args.name / "nvke-network.json"
    if args.check and not manifest.is_file():
        parser.error("no managed network manifest for this VM")
    if not args.check and (home / args.name).exists():
        parser.error("instance already exists; choose a fresh name")
    if args.management_vm and not (home / args.management_vm / "lima.yaml").is_file():
        parser.error("management VM must exist in this LIMA_HOME")

    code = Path(__file__).read_text()
    def in_guest(action, argument=""):
        return run("limactl", "shell", args.name, "sudo", "-n", "python3", "-", action, argument, source=code)

    if args.check:
        plan = json.loads(manifest.read_text())
    else:
        run("limactl", "start", "--tty=false", f"--name={args.name}", "--arch=aarch64",
            "--vm-type=vz", "--network=lima:user-v2", "--mount-none", "--containerd=none",
            f"--cpus={args.cpus}", f"--memory={args.memory}", f"--disk={args.disk}",
            "template:ubuntu-24.04", capture=False)
        plan = json.loads(in_guest("_inspect"))
    address = str(ipaddress.IPv4Interface(plan["address"]).ip)
    leases = json.loads(run("curl", "-fsS", "--max-time", "5", "--unix-socket",
                            str(home / "_networks/user-v2/user-v2_ep.sock"),
                            "http://lima/services/dhcp/leases"))
    if leases.get(address, "").lower() != plan["mac"]:
        raise ValueError("guest IPv4 is not reserved to its MAC in the Lima lease table")
    for saved in home.glob("*/nvke-network.json"):
        if saved != manifest and str(ipaddress.IPv4Interface(json.loads(saved.read_text())["address"]).ip) == address:
            raise ValueError(f"{address} already belongs to {saved.parent.name}")
    if not args.check:
        in_guest("_configure", json.dumps(plan))
        run("limactl", "stop", args.name, capture=False)
        run("limactl", "start", "--tty=false", args.name, capture=False)
    result = in_guest("_verify")
    verified = json.loads(result)
    if any(verified[key] != plan[key] for key in ("address", "gateway", "metric", "mac")):
        raise ValueError("guest network no longer matches the saved Lima address")
    if args.management_vm:
        run("limactl", "shell", args.management_vm, "python3", "-c",
            "import socket,sys; socket.create_connection((sys.argv[1],22),5).close()", address)
    if not args.check:
        write_file(manifest, result)
    print(result, end="")


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] in ("_inspect", "_configure", "_verify"):
            guest(sys.argv[1], sys.argv[2])
        else:
            create()
    except (OSError, ValueError, subprocess.SubprocessError) as err:
        sys.exit(str(err))
