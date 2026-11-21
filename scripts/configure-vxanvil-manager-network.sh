#!/bin/bash
set -euo pipefail
[[ $# == 2 && ( $1 == --install || $1 == --recover ) && $EUID == 0 ]] || {
    echo "Usage: sudo bash $0 --install|--recover trusted-layout.env" >&2
    exit 2
}
mode=$1
layout=$(realpath "$2")
source "$layout"
: "${CLUSTER:?}" "${CP_IP:?}" "${MANAGER_IP:?}" "${WORKER_IP:?}"
[[ $CLUSTER =~ ^(vxanvil|vmetal)-[a-z0-9-]+$ && $CLUSTER != *- ]]
network_mode=${NETWORK_MODE:-lan}
[[ $network_mode == lan || $network_mode == tailscale ]]
python3 - "$CP_IP" "$MANAGER_IP" "$WORKER_IP" <<'PY'
import ipaddress
import sys
for arg in sys.argv[1:]:
    ip = ipaddress.IPv4Address(arg)
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast or not ip.is_global and not (
        ip.is_private or ip in ipaddress.IPv4Network("100.64.0.0/10")
    ):
        sys.exit("Invalid routable IPv4 endpoint")
PY
if [[ $mode == --install ]]; then
    config="/etc/vmetal/network/$CLUSTER.env"
    if [[ -e $config ]]; then
        cmp -s "$layout" "$config" || { echo "Existing network layout differs." >&2; exit 1; }
    fi
fi
[[ $(sysctl -n net.ipv4.ip_forward) == 1 ]]
iface=$(ip -j -4 route get 1.1.1.1 | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["dev"])')
peer_iface=$(ip -j -4 route get "$WORKER_IP" | python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["dev"])')
[[ $iface =~ ^[a-zA-Z0-9_.-]+$ && $peer_iface =~ ^[a-zA-Z0-9_.-]+$ ]]
ensure() {
    local table=$1 chain=$2
    shift 2
    iptables -w 5 -t "$table" -C "$chain" "$@" 2>/dev/null ||
        iptables -w 5 -t "$table" -I "$chain" 1 "$@"
}
if [[ $network_mode == tailscale ]]; then
    [[ $peer_iface == tailscale0 ]]
    routes=$(tailscale debug prefs | python3 -c '
import json,sys
routes=set(json.load(sys.stdin).get("AdvertiseRoutes") or [])
routes.update(ip+"/32" for ip in sys.argv[1:])
print(",".join(sorted(routes)))
' "$CP_IP" "$MANAGER_IP")
    tailscale set --snat-subnet-routes=false --advertise-routes="$routes"
    printf 'Approve these routes in Tailscale if needed: %s/32, %s/32\n' "$CP_IP" "$MANAGER_IP"
fi
# Forward only the requested tenant's traffic. Existing policy is left to the host.
ensure filter DOCKER-USER -i "$peer_iface" -s "$WORKER_IP/32" -d "$CP_IP/32" -p tcp --dport 6443 -j ACCEPT
ensure filter DOCKER-USER -i "$peer_iface" -s "$WORKER_IP/32" -d "$CP_IP/32" -p udp --dport 6081 -j ACCEPT
ensure filter DOCKER-USER -s "$CP_IP/32" -d "$WORKER_IP/32" -p tcp -m multiport --dports 22,10250,15005 -j ACCEPT
ensure filter DOCKER-USER -s "$CP_IP/32" -d "$WORKER_IP/32" -p udp --dport 6081 -j ACCEPT
ensure filter DOCKER-USER -s "$WORKER_IP/32" -d "$CP_IP/32" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ensure filter DOCKER-USER -s "$CP_IP/32" -d "$WORKER_IP/32" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ensure nat POSTROUTING -s "$CP_IP/32" ! -d "$WORKER_IP/32" -o "$iface" -p tcp --dport 443 \
    -m comment --comment "$CLUSTER-egress" -j MASQUERADE

if [[ $mode == --install ]]; then
    config="/etc/vmetal/network/$CLUSTER.env"
    program=/usr/local/libexec/vmetal-manager-network
    unit="$CLUSTER-network.service"
    # A changed endpoint should be applied to a new tenant, avoiding stale rules.
    if [[ -e $config ]]; then
        cmp -s "$layout" "$config" || { echo "Existing network layout differs." >&2; exit 1; }
    else
        install -D -m 0600 "$layout" "$config"
    fi
    [[ $0 -ef $program ]] || install -D -m 0755 "$0" "$program"
    cat >"/etc/systemd/system/$unit" <<UNIT
[Unit]
Description=VXAnvil tenant network recovery for $CLUSTER
Wants=network-online.target tailscaled.service
Requires=docker.service
After=network-online.target docker.service tailscaled.service
PartOf=docker.service
[Service]
Type=oneshot
ExecStart=$program --recover $config
RemainAfterExit=yes
Restart=on-failure
RestartSec=15s
[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable "$unit"
    systemctl --no-block restart "$unit"
fi
