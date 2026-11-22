#!/usr/bin/env python3
"""Enroll dedicated NVKE lab hosts over TLS using single-use invitations."""

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

RESOURCE = "vxanvilhosts.vxanvil.infrastructure.cluster.x-k8s.io"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def run(*argv, body=None):
    return subprocess.run(argv, input=body, capture_output=True, text=True, check=True, timeout=12).stdout


def write_json(path, data):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        json.dump(data, output)
    os.replace(output.name, path)


class Reject(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class Registry:
    def __init__(self, args):
        self.args = args
        self.network = ipaddress.IPv4Network(args.network)
        self.state = Path(args.state)
        self.known_hosts = Path(args.known_hosts)

    def kubectl(self, *args, body=None):
        return run("kubectl", "--kubeconfig", self.args.kubeconfig, "--request-timeout=8s", *args, body=body)

    def check_in(self, peer, token, data):
        if ipaddress.IPv4Address(peer) not in self.network:
            raise Reject(403, "source address outside enrollment network")
        machine = data.get("machineID", "")
        user = data.get("user", "")
        key = data.get("hostKey", "")
        credential = data.get("credential", "")
        for value, pattern in (
            (machine, r"[0-9a-f]{64}"), (user, r"[a-z_][a-z0-9_-]{0,31}"),
            (key, r"ssh-ed25519 [A-Za-z0-9+/=]{68}"), (credential, r"[a-f0-9]{64}"),
        ):
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                raise Reject(400, "invalid enrollment fields")
        name = "host-" + machine[:32]
        record = self.state / (name + ".json")
        identity = {"machineID": machine, "user": user, "hostKey": key, "credential": digest(credential)}
        invite = None
        if record.exists():
            if not hmac.compare_digest(record.read_text(), json.dumps(identity)):
                raise Reject(403, "host credential or identity mismatch")
        else:
            if not re.fullmatch(r"[a-f0-9]{64}", token):
                raise Reject(403, "invitation required")
            invite = self.state / (digest(token) + ".invite")
            if not invite.is_file():
                raise Reject(403, "invalid or already used invitation")
            invitation = json.loads(invite.read_text())
            if invitation["expires"] < time.time():
                raise Reject(403, "invitation expired")
            if invitation.get("holder", identity) != identity:
                raise Reject(403, "invitation already bound to another host")
            if "holder" not in invitation:
                # Bind before kubectl writes so a timed-out request can be retried
                # only by the same host.
                invitation["holder"] = identity
                write_json(invite, invitation)

        spec = {"machineID": machine, "sshTarget": f"{user}@{peer}", "nodeIP": peer,
                "allowBootstrap": True, "managedAddress": True}
        raw = self.kubectl("get", f"{RESOURCE}/{name}", "--ignore-not-found", "-o", "json")
        host = json.loads(raw) if raw else None
        if host:
            old = host["spec"]
            if host["metadata"].get("deletionTimestamp") or not old.get("managedAddress") or old["machineID"] != machine:
                raise Reject(409, "existing host is not managed by enrollment")
            if old == spec:
                if not record.exists():
                    self.verify_ssh(peer, user, key, machine)
                    write_json(record, identity)
                    invite.unlink()
                return name
            if host.get("status", {}).get("claim"):
                raise Reject(409, "address changed while claimed; release the host before changing addresses")
        self.verify_ssh(peer, user, key, machine)
        if host:
            patch = [
                {"op": "test", "path": "/metadata/resourceVersion", "value": host["metadata"]["resourceVersion"]},
                {"op": "replace", "path": "/spec", "value": spec},
            ]
            self.kubectl("patch", f"{RESOURCE}/{name}", "--type=json", "-p", json.dumps(patch))
        else:
            obj = {"apiVersion": "vxanvil.infrastructure.cluster.x-k8s.io/v1alpha1",
                   "kind": "VXAnvilHost", "metadata": {"name": name}, "spec": spec}
            self.kubectl("create", "-f", "-", body=json.dumps(obj))
        if invite:
            write_json(record, identity)
            invite.unlink()
        print(f"{name} registered at {peer}", flush=True)
        return name

    def verify_ssh(self, peer, user, key, machine):
        line = f"{peer} {key}"
        old = self.known_hosts.read_text() if self.known_hosts.exists() else ""
        if old:
            result = subprocess.run(["ssh-keygen", "-F", peer, "-f", str(self.known_hosts)],
                                    capture_output=True, text=True, timeout=5)
            for row in result.stdout.splitlines():
                if not row.startswith("#") and row.split()[1:3] != key.split():
                    raise Reject(409, "address already has a different trusted SSH key")
        with tempfile.TemporaryDirectory() as directory:
            probe_hosts = Path(directory) / "known_hosts"
            probe_hosts.write_text(line + "\n")
            try:
                observed = run("/usr/bin/ssh", "-F", "/dev/null", "-i", self.args.ssh_key, "-T",
                               "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                               "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5",
                               "-o", f"UserKnownHostsFile={probe_hosts}",
                               "-o", "GlobalKnownHostsFile=/dev/null", "--", f"{user}@{peer}",
                               "cat /etc/machine-id && sudo -n -l /usr/local/sbin/vxanvil-kubeadm").splitlines()
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                raise Reject(409, "management SSH preflight failed") from None
            if not observed or digest("vxanvil:" + observed[0].strip()) != machine:
                raise Reject(409, "management SSH identity does not match")
        if line not in old.splitlines():
            # Append without replacing the trust entries used by other hosts.
            with self.known_hosts.open("a") as output:
                output.write(("" if not old or old.endswith("\n") else "\n") + line + "\n")


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def do_POST(self):
        try:
            if self.path != "/v1/hosts/check-in":
                raise Reject(404, "unknown endpoint")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise Reject(400, "invalid request length")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise Reject(400, "expected an object")
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            host = self.server.registry.check_in(self.client_address[0], token, data)
            self.reply(200, {"host": host})
        except Reject as error:
            self.reply(error.status, {"error": str(error)})
        except (ValueError, TypeError):
            self.reply(400, {"error": "invalid request"})
        except (OSError, subprocess.SubprocessError) as error:
            print(f"enrollment dependency failed: {type(error).__name__}", flush=True)
            self.reply(503, {"error": "management dependency failed; retry"})

    def reply(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("invite")
    serve = commands.add_parser("serve")
    for flag in ("bind", "network", "cert", "key", "kubeconfig", "ssh-key", "known-hosts"):
        serve.add_argument("--" + flag, required=True)
    serve.add_argument("--port", type=int, default=18443)
    args = parser.parse_args()
    state = Path(args.state)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.action == "invite":
        token = secrets.token_hex(32)
        write_json(state / (digest(token) + ".invite"), {"expires": time.time() + 900})
        print(token)
        return
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.cert, args.key)
    with HTTPServer((args.bind, args.port), Handler) as server:
        server.registry = Registry(args)
        server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        server.serve_forever()


if __name__ == "__main__":
    main()
