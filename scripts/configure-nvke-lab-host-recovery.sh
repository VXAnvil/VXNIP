#!/bin/bash
set -euo pipefail

fail() { echo "$*" >&2; exit 1; }
if [[ $# -lt 5 || $# -gt 8 ]]; then
    fail "Usage: bash $0 --check|--install|--recover worker|gateway node-ip api-ip spire-ip [worker-route-gateway|gateway-peer-ip] [worker-pod-cidr] [host-network-api]"
fi

action=$1
role=$2
node_ip=$3
api_ip=$4
spire_ip=$5
peer=${6:-}
pod_cidr=${7:-}
host_network=${8:-}
case $action in --check|--install|--recover) ;; *) fail "Invalid action: $action" ;; esac
case $role in
    worker) [[ $node_ip != auto || -z $peer ]] || fail "Automatic worker address requires a direct route." ;;
    gateway) [[ -n $peer && $node_ip == "$spire_ip" ]] || fail "Gateway requires its SPIRE IP and the ARM worker IP." ;;
    *) fail "Invalid role: $role" ;;
esac
[[ -z $host_network || ( $host_network == host-network-api && -n $pod_cidr ) ]] ||
    fail "host-network-api requires a worker pod CIDR."
if [[ -n $pod_cidr ]]; then
    [[ $role == worker && -n $peer ]] || fail "Pod API forwarding requires a worker with an explicit API route gateway."
    python3 -c '
import ipaddress, sys
network = ipaddress.IPv4Network(sys.argv[1])
if not network.is_private or network.is_loopback or network.is_link_local or str(network) != sys.argv[1]:
    sys.exit("Expected a canonical private pod CIDR.")
' "$pod_cidr"
fi
python3 -c '
import ipaddress, sys
for arg in sys.argv[1:]:
    if arg == "auto" and arg == sys.argv[1]:
        continue
    ip = ipaddress.IPv4Address(arg)
    if not ip.is_private or ip.is_unspecified or ip.is_loopback or ip.is_multicast:
        sys.exit(f"Expected a private host IPv4 address: {arg}")
' "$node_ip" "$api_ip" "$spire_ip" ${peer:+"$peer"}

unit="vmetal-nvke-lab-$role-recovery.service"
timer="${unit%.service}.timer"
program=/usr/local/libexec/vmetal-nvke-lab-host-recovery
if [[ $action == --install ]]; then
    [[ $EUID == 0 ]] || fail "Install requires sudo."
    if [[ ! $0 -ef $program ]]; then
        install -D -o root -g root -m 0755 "$0" "$program"
    fi
    cat >"/etc/systemd/system/$unit" <<UNIT
[Unit]
Description=Restore NVKE lab $role networking and services
Wants=network-online.target
After=network-online.target docker.service
StartLimitIntervalSec=0

[Service]
Type=oneshot
ExecStart=$program --recover $role $node_ip $api_ip $spire_ip $peer $pod_cidr $host_network
Restart=on-failure
RestartSec=15s
TimeoutStartSec=90s
RemainAfterExit=yes
UNIT
    cat >"/etc/systemd/system/$timer" <<UNIT
[Unit]
Description=Run NVKE lab $role recovery after boot

[Timer]
OnBootSec=10s

[Install]
WantedBy=timers.target
UNIT
    systemctl daemon-reload
    systemctl enable --now "$timer"
    systemctl --no-block restart "$unit"
    exit
fi

if [[ $node_ip == auto ]]; then
    node_ip=$(ip -4 route get "$api_ip" | awk '{for (i=1; i<NF; i++) if ($i=="src") print $(i+1)}')
fi
iface=$(ip -o -4 addr show | awk -v addr="$node_ip" '{split($4, a, "/"); if (a[1] == addr) print $2}')
[[ $iface =~ ^[a-zA-Z0-9_.-]+$ ]] || fail "Host IP $node_ip must be assigned to exactly one interface."

probe() {
    timeout 4 bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "$1" "$2" || fail "TCP $1:$2 unreachable."
    printf 'TCP %s:%s OK\n' "$1" "$2"
}

if [[ $action == --recover ]]; then
    [[ $EUID == 0 ]] || fail "Recovery requires root."
    if [[ $host_network == host-network-api ]]; then
        # OVN uses bidirectional propagation for this directory.
        install -d -m 0755 /run/netns
        if [[ $(findmnt -n -o PROPAGATION --target /run/netns) != *shared* ]]; then
            mountpoint -q /run/netns || mount --bind /run/netns /run/netns
            mount --make-shared /run/netns
        fi
    fi
fi

if [[ $role == gateway ]]; then
    [[ $(ip -4 route get "$api_ip") == *' dev lxdbr0 '* ]] || fail "Control plane is not routed through lxdbr0."
    [[ $(ip -4 route get "$peer") == *" dev $iface "* ]] || fail "ARM worker is not routed through $iface."
    rule=(-i "$iface" -o lxdbr0 -s "$peer/32" -d "$api_ip/32" -j ACCEPT)
    chain=pstrt.lxdbr0
    if [[ $action == --recover ]]; then
        [[ $(sysctl -n net.ipv4.ip_forward) == 1 ]] || fail "IPv4 forwarding is disabled."
        nft_rules=$(nft list chain inet lxd "$chain")
        iptables -w 5 -C DOCKER-USER "${rule[@]}" ||
            iptables -w 5 -I DOCKER-USER 1 "${rule[@]}"
        [[ $nft_rules == *"ip saddr $api_ip ip daddr $peer return"* ]] ||
            nft insert rule inet lxd "$chain" ip saddr "$api_ip" ip daddr "$peer" return comment "\"vmetal-nvke-lab:$api_ip:$peer\""
        echo "Scoped forwarding and return path OK ($peer ↔ $api_ip)"
    fi
    ip -4 route get "$api_ip"
    ip -4 route get "$peer"
    probe "$api_ip" 6443
    exit
fi

if [[ -n $peer ]]; then
    if [[ $action == --recover ]]; then
        ip -4 route replace "$api_ip/32" via "$peer" dev "$iface" src "$node_ip"
    fi
    [[ $(ip -4 route get "$api_ip") == *" via $peer dev $iface "* ]] ||
        fail "Control-plane route must use $peer on $iface."
fi
ip -4 route get "$api_ip"
api_forwarding() {
    local bridge=$1 source=$2
    local -a to_api from_api
    to_api=(-i "$bridge" -o "$iface" -s "$source" -d "$api_ip/32" -p tcp --dport 6443
        -m conntrack --ctstate NEW,ESTABLISHED -m comment --comment vmetal-nvke-pod-api -j ACCEPT)
    from_api=(-i "$iface" -o "$bridge" -s "$api_ip/32" -d "$source" -p tcp --sport 6443
        -m conntrack --ctstate ESTABLISHED -m comment --comment vmetal-nvke-pod-api -j ACCEPT)
    if [[ $action == --recover ]]; then
        iptables -w 5 -C DOCKER-USER "${to_api[@]}" 2>/dev/null ||
            iptables -w 5 -I DOCKER-USER 1 "${to_api[@]}"
        iptables -w 5 -C DOCKER-USER "${from_api[@]}" 2>/dev/null ||
            iptables -w 5 -I DOCKER-USER 1 "${from_api[@]}"
    fi
    iptables -w 5 -C DOCKER-USER "${to_api[@]}"
    iptables -w 5 -C DOCKER-USER "${from_api[@]}"
    if [[ $bridge == ovn-k8s-mp0 ]]; then
        printf 'Pod API forwarding OK (%s ↔ %s:6443)\n' "$source" "$api_ip"
    else
        printf 'Host-network API forwarding OK (%s ↔ %s:6443)\n' "$source" "$api_ip"
    fi
}

if [[ -n $pod_cidr && $EUID == 0 ]]; then
    api_forwarding ovn-k8s-mp0 "$pod_cidr"
    if [[ $host_network == host-network-api ]]; then
        # OVN local gateway translates host-originated Service traffic through br-ex.
        api_forwarding br-ex 169.254.0.1/32
    fi
fi
if [[ $action == --recover ]]; then
    state=/var/lib/vmetal-kubeadm
    [[ -d $state ]] || exit 0
    exec 9>>"$state/lock"
    flock -w 10 9 || fail "Provider allocation is being updated."
    [[ -f $state/current.json ]] || exit 0
    allocation=$(python3 -c '
import json, sys
with open(sys.argv[1]) as f:
    state = json.load(f)
if state.get("mode") == "nvke" and state.get("phase") == "complete":
    unit = state.get("unit")
    if unit not in ("nke-agent.service", "nvke-agent.service"):
        sys.exit("Unexpected allocation agent unit")
    print(unit, "spire" if state.get("spire") is True else "none")
' "$state/current.json")
    [[ -n $allocation ]] || exit 0
    read -r agent identity <<<"$allocation"
fi

probe "$api_ip" 6443
probe "$spire_ip" 30441
runtime=containerd.service
socket=/run/containerd/containerd.sock
if [[ $node_ip == "$spire_ip" || -f /etc/systemd/system/vmetal-nvke-containerd.service ]]; then
    runtime=vmetal-nvke-containerd.service
    socket=/run/vmetal-nvke-containerd/containerd.sock
fi
if [[ $action == --recover ]]; then
    systemctl start "$runtime"
fi
systemctl is-active "$runtime"
[[ -S $socket ]] || fail "Missing CRI socket: $socket"
if [[ $EUID == 0 ]]; then
    crictl --config=/dev/null --runtime-endpoint "unix://$socket" --timeout 5s info |
        python3 -c '
import json, sys
conditions = json.load(sys.stdin)["status"]["conditions"]
if not any(c["type"] == "RuntimeReady" and c["status"] is True for c in conditions):
    sys.exit("CRI RuntimeReady is false")
'
    echo "CRI RuntimeReady: $socket"
else
    echo "CRI socket present: $socket (use sudo to check RuntimeReady)"
fi

if [[ $action == --recover ]]; then
    if [[ $identity == spire ]]; then
        systemctl start spire-agent.service
    fi
    systemctl start "$agent"
    if [[ -s /etc/kubernetes/kubelet.conf ]]; then
        systemctl start kubelet.service
    fi
else
    systemctl is-active spire-agent.service nvke-agent.service kubelet.service
fi
swapon --show
