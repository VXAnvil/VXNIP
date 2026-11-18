#!/bin/bash
set -euo pipefail
umask 077

config=/etc/homelab-nvke-enrollment
if [[ ${1:-} == --check-in && $# == 1 ]]; then
    exec python3 - "$config" <<'PY'
import hashlib
import json
from pathlib import Path
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

config = Path(sys.argv[1])
settings = json.loads((config / "settings.json").read_text())
ticket = config / "invitation"
host_key = Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text().split()
identity = Path("/etc/machine-id").read_text().strip()
payload = json.dumps({
    "machineID": hashlib.sha256(("nvke-homelab:" + identity).encode()).hexdigest(),
    "user": settings["user"],
    "hostKey": " ".join(host_key[:2]),
    "credential": (config / "credential").read_text().strip(),
}).encode()
headers = {"Content-Type": "application/json"}
if ticket.exists():
    headers["Authorization"] = "Bearer " + ticket.read_text().strip()
request = Request(settings["url"] + "/v1/hosts/check-in", data=payload, headers=headers)
try:
    with urlopen(request, context=ssl.create_default_context(cafile=str(config / "ca.crt")), timeout=30) as response:
        result = json.load(response)
except HTTPError as err:
    sys.exit(f"Enrollment HTTP {err.code}: {err.read(1024).decode(errors='replace')}")
except URLError as err:
    sys.exit(f"Enrollment endpoint unavailable: {err.reason}")
ticket.unlink(missing_ok=True)
print(result["host"])
PY
fi

if [[ $# != 5 || $EUID != 0 ]]; then
    echo "Usage: sudo bash $0 https://enrollment-server:18443 artifact-dir bundle-dir api-ip|- spire-ip" >&2
    exit 2
fi

url=${1%/}
artifacts=$(realpath "$2")
bundle=$(realpath "$3")
api_ip=$4
spire_ip=$5
scripts=$(dirname "$(realpath "$0")")
[[ $url =~ ^https://[a-zA-Z0-9.-]+:[0-9]+$ ]] || exit 2
test -s "$bundle/ca.crt"
test -s "$bundle/provider.pub"
if [[ ! -f $config/credential ]]; then
    test -s "$bundle/invitation"
fi

bash "$scripts/prepare-nvke-vm-worker.sh" "$artifacts" ubuntu "$bundle/provider.pub"
endpoints=("$spire_ip:30441")
if [[ $api_ip != - ]]; then
    endpoints+=("$api_ip:6443")
fi
for endpoint in "${endpoints[@]}"; do
    timeout 5 bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "${endpoint%:*}" "${endpoint##*:}"
done
if [[ $api_ip != - ]]; then
    bash "$scripts/configure-nvke-lab-host-recovery.sh" --install worker auto "$api_ip" "$spire_ip"
fi

install -d -m 0700 "$config"
install -o root -g root -m 0600 "$bundle/ca.crt" "$config/ca.crt"
if [[ ! -f $config/credential ]]; then
    install -o root -g root -m 0600 "$bundle/invitation" "$config/invitation"
    python3 -c 'import secrets; print(secrets.token_hex(32))' >"$config/credential"
fi
printf '{"url":"%s","user":"ubuntu"}\n' "$url" >"$config/settings.json"
program=/usr/local/libexec/homelab-nvke-enrollment
install -D -o root -g root -m 0755 "$0" "$program"
cat >/etc/systemd/system/homelab-nvke-enrollment.service <<UNIT
[Unit]
Description=Check in with the NVKE lab enrollment service
Wants=network-online.target
After=network-online.target ssh.service

[Service]
Type=oneshot
ExecStart=$program --check-in
UNIT
cat >/etc/systemd/system/homelab-nvke-enrollment.timer <<'UNIT'
[Unit]
Description=Refresh the NVKE lab host address

[Timer]
OnBootSec=15s
OnUnitActiveSec=30s
AccuracySec=2s

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now homelab-nvke-enrollment.timer
systemctl start homelab-nvke-enrollment.service
journalctl -u homelab-nvke-enrollment.service -n 3 -o cat --no-pager
