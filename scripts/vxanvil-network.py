#!/usr/bin/env python3
"""Report routes, MTU, API TLS, SPIRE, agent expiry and CRI as JSON."""
import argparse
import ipaddress
import json
import socket
import ssl
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, build_opener


def path_mtu(peer):
    ipaddress.IPv4Address(peer)
    route = json.loads(subprocess.check_output(["ip", "-j", "-4", "route", "get", peer], timeout=8))[0]
    link = json.loads(subprocess.check_output(["ip", "-j", "link", "show", "dev", route["dev"]], timeout=8))[0]
    mtu = min(link["mtu"], route.get("mtu", link["mtu"]))
    return {"interface": route["dev"], "source": route.get("prefsrc", route.get("src")),
            "gateway": route.get("gateway"), "underlayMTU": mtu, "podMTU": mtu - 100}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--peer", required=True)
    parser.add_argument("--mtu-only", action="store_true")
    parser.add_argument("--api-url")
    parser.add_argument("--ca")
    parser.add_argument("--spire")
    parser.add_argument("--cri")
    parser.add_argument("--agent", action="store_true")
    parser.add_argument("--cidrs", nargs="*", default=[])
    args = parser.parse_args()
    if args.mtu_only:
        mtu = path_mtu(args.peer)["podMTU"]
        if mtu < 576:
            raise ValueError("Underlay MTU is too small for the IPv4 overlay")
        print(mtu)
        return 0
    checks = []

    def check(name, operation):
        try:
            detail = operation()
            checks.append({"check": name, "status": "pass", "detail": detail})
        except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError) as error:
            checks.append({"check": name, "status": "fail", "detail": str(error)[:300]})

    check("route-and-mtu", lambda: path_mtu(args.peer))

    def cidrs():
        nets = [ipaddress.IPv4Network(cidr) for cidr in args.cidrs]
        for index, net in enumerate(nets):
            if any(net.overlaps(other) for other in nets[index + 1:]):
                raise ValueError("Requested LAN, Docker, Pod or Service networks overlap")
        return [str(net) for net in nets]
    check("cidr-overlap", cidrs)

    if args.spire:
        def spire():
            with socket.create_connection((args.spire, 30441), timeout=6):
                return "TCP 30441 reachable"
        check("spire-tcp", spire)
    if args.api_url:
        def api():
            parsed = urlsplit(args.api_url)
            if parsed.scheme != "https" or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                raise ValueError("Use an HTTPS API origin without credentials or a path")
            if not args.ca:
                raise ValueError("--ca is required for API TLS verification")
            context = ssl.create_default_context(cafile=args.ca)
            opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context))
            with opener.open(args.api_url.rstrip("/") + "/readyz", timeout=8) as response:
                if response.status != 200 or response.read(128).strip() != b"ok":
                    raise ValueError("API /readyz did not return HTTP 200 and ok")
                return "CA, hostname and /readyz HTTP 200 verified"
        check("api-tls-readyz", api)
    if args.cri:
        def cri():
            result = subprocess.run(
                ["crictl", "--config=/dev/null", "--runtime-endpoint", args.cri, "--timeout", "5s", "info"],
                capture_output=True, text=True, check=True, timeout=10,
            )
            data = json.loads(result.stdout)
            if not any(c["type"] == "RuntimeReady" and c["status"] is True for c in data["status"]["conditions"]):
                raise ValueError("CRI RuntimeReady is false")
            return "CRI RuntimeReady"
        check("cri", cri)
    if args.agent:
        def agent():
            result = subprocess.run(
                ["openssl", "s_client", "-connect", f"{args.peer}:15005", "-showcerts"],
                input="", capture_output=True, text=True, timeout=8,
            )
            cert = subprocess.run(
                ["openssl", "x509", "-noout", "-checkend", "900"],
                input=result.stdout, capture_output=True, text=True, timeout=5,
            )
            if cert.returncode:
                raise ValueError("Agent certificate missing or expires within 15 minutes")
            return "Presented certificate expiry exceeds 15 minutes"
        check("agent-public-certificate-expiry", agent)
    checks.append({"check": "udp-6081", "status": "requires-workload",
                   "detail": "Use check-nvke-lab-workers.sh for real bidirectional OVN Pod traffic. TCP checks do not verify UDP."})
    print(json.dumps({"checks": checks}, indent=2))
    return int(any(check["status"] == "fail" for check in checks))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
