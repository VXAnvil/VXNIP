#!/bin/bash
set -euo pipefail
umask 077
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
[[ $# == 3 ]] || fail "Usage: $0 check-control-plane|control-plane|check-worker|worker|join|verify trusted-layout.env state-dir"
phase=$1
layout=$(realpath "$2")
state=$(realpath -m "$3")
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# Both operators supply the same trusted, local shell configuration.
source "$layout"
: "${CLUSTER:?}" "${MANAGER_IP:?}" "${LAN_CIDR:?}" "${DOCKER_CIDR:?}" "${CP_IP:?}"
: "${POD_CIDR:?}" "${SERVICE_CIDR:?}" "${WORKER_IP:?}" "${WORKER_GATEWAY:?}"
[[ $CLUSTER =~ ^(vxanvil|vmetal)-[a-z0-9]([a-z0-9-]{0,20}[a-z0-9])?$ ]] || fail "Choose a unique vxanvil-* name."
network="$CLUSTER-routed"
container="$CLUSTER-control-plane"
volume="$container-var"
api="https://$CP_IP:6443"
ns=vmetal-gtx-gpu
marker=vmetal.nvke.dev/installer
privilege() { if [[ $EUID == 0 ]]; then "$@"; else sudo "$@"; fi; }
tcp() { timeout 5 bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "$@"; }
address_interface() {
    ip -j -4 address show | jq -er --arg ip "$1" '[.[] | select(any(.addr_info[]; .local == $ip)) | .ifname] | select(length==1) | .[0]'
}
kube() { kubectl --kubeconfig "$state/workload.kubeconfig" --request-timeout=15s "$@"; }
mgmt() { kubectl --kubeconfig "$KUBECONFIG" --request-timeout=15s "$@"; }
nvke() { nvkectl --site "$NVKE_SITE" "$@"; }
wait_for() {
    local attempts=$1 i
    shift
    for ((i=0;i<attempts;i++)); do
        if "$@"; then return; fi
        sleep 4
    done
    return 1
}
disk() {
    python3 - "$@" <<'PY'
import os
from pathlib import Path
import sys

where, percent, reserve = sys.argv[1:]
path = Path(where)
while not path.exists():
    path = path.parent
fs = os.statvfs(path)
free = fs.f_bavail * fs.f_frsize
required = fs.f_blocks * fs.f_frsize * int(percent) // 100 + int(reserve) * 2**20
print(f"{where}: {free / 2**30:.2f} GiB available, {required / 2**30:.2f} GiB required")
if free < required:
    sys.exit(f"Insufficient space at {where}")
PY
}

check_network() {
    python3 - "$1" "$2" "$MANAGER_IP" "$LAN_CIDR" "$DOCKER_CIDR" "$CP_IP" \
        "$POD_CIDR" "$SERVICE_CIDR" "$WORKER_IP" "$WORKER_GATEWAY" <<'PY'
from ipaddress import IPv4Address, IPv4Network
from itertools import combinations
import json
import subprocess
import sys

role, bridge, manager, lan, docker, cp, pod, service, worker, gateway = sys.argv[1:]
manager, cp, worker, gateway = map(IPv4Address, (manager, cp, worker, gateway))
networks = [IPv4Network(cidr) for cidr in (lan, docker, pod, service)]
lan, docker, pod, service = networks
private = [IPv4Network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
if any(not any(net.subnet_of(scope) for scope in private) for net in networks):
    sys.exit("The LAN, Docker, Pod and Service networks must use private IPv4 ranges.")
if any(a.overlaps(b) for a, b in combinations(networks, 2)):
    sys.exit("LAN, Docker, Pod and Service CIDRs overlap.")
if manager not in lan or cp not in docker or cp <= docker.network_address + 1 or cp == docker.broadcast_address:
    sys.exit("Manager/CP addresses do not match the LAN and Docker subnets.")
if worker == manager or any(addr in net for addr in (worker, gateway) for net in (docker, pod, service)):
    sys.exit("Worker address or gateway conflicts with the layout.")
routes = json.loads(subprocess.check_output(["ip", "-j", "-4", "route", "show", "table", "main"]))
for route in routes:
    if route.get("dst") in (None, "default"):
        continue
    existing = IPv4Network(route["dst"])
    if role == "manager" and existing == docker and route.get("dev") == bridge and bridge:
        continue
    if role == "worker" and existing == IPv4Network(f"{cp}/32") and route.get("gateway") == str(gateway):
        continue
    for wanted in (docker, pod, service):
        if existing.overlaps(wanted):
            sys.exit(f"Existing route {existing} ({route.get('dev')}) overlaps {wanted}.")
if role == "manager":
    networks = subprocess.check_output(["docker", "network", "ls", "-q"], text=True).split()
    for network in json.loads(subprocess.check_output(["docker", "network", "inspect", *networks])):
        if bridge and network["Id"].startswith(bridge[3:]):
            continue
        for address in network["IPAM"].get("Config") or []:
            if ":" not in address.get("Subnet", ":"):
                existing = IPv4Network(address["Subnet"])
                if any(existing.overlaps(wanted) for wanted in (docker, pod, service)):
                    sys.exit(f"Docker network {network['Name']} overlaps the requested layout ({existing}).")
PY
}
lock_state() {
    install -d -m 0700 "$state"
    exec 9>"$state/install.lock"
    flock -n 9 || fail "Another installer is using $state."
    local fingerprint
    fingerprint=$(sha256sum "$layout" | cut -d' ' -f1)
    if [[ -f $state/layout.sha256 ]]; then
        [[ $(cat "$state/layout.sha256") == "$fingerprint" ]] || fail "Use the original layout for this state directory."
    else
        printf '%s\n' "$fingerprint" >"$state/layout.sha256"
    fi
}

manager_inputs() {
    : "${KUBECONFIG:?Management kubeconfig required}" "${NVKE_SITE:?}"
    [[ -n ${CP_IMAGE:-${CP_BASE_IMAGE:-}} ]] || fail "Set CP_IMAGE or CP_BASE_IMAGE."
    : "${PROVIDER_PUBLIC_KEY:?}" "${PROVIDER_KNOWN_HOSTS:?}" "${PROVIDER_SSH:?}"
    [[ -r $PROVIDER_PUBLIC_KEY && -f $PROVIDER_KNOWN_HOSTS && -x $PROVIDER_SSH ]] || fail "Check Provider SSH paths."
    ssh-keygen -l -f "$PROVIDER_PUBLIC_KEY" >/dev/null
    [[ $(awk 'NR==1 {print $1}' "$PROVIDER_PUBLIC_KEY") == ssh-ed25519 ]] || fail "Use a Provider Ed25519 key."
    address_interface "$MANAGER_IP" >/dev/null || fail "Manager IP is not assigned locally."
    iface=$(ip -j -4 route get 1.1.1.1 | jq -er '.[0].dev')
    [[ $iface =~ ^[a-zA-Z0-9_.-]+$ ]] || fail "Unexpected interface name."
    [[ $(sysctl -n net.ipv4.ip_forward) == 1 ]] || fail "Enable IPv4 forwarding on the Docker host."
    mgmt get crd vmetalhosts.vmetal.infrastructure.cluster.x-k8s.io >/dev/null
    nvke get clusters >/dev/null
    tcp "$MANAGER_IP" 30441 || fail "SPIRE unavailable at $MANAGER_IP:30441."
}

remember_manager() {
    local identity
    identity=$( { mgmt get namespace kube-system -o jsonpath='{.metadata.uid}'; printf '\n%s\n' "$NVKE_SITE";
        awk 'NR==1 {print $1, $2}' "$PROVIDER_PUBLIC_KEY"; } | sha256sum | cut -d' ' -f1)
    if [[ -e $state/manager.sha256 ]]; then
        [[ $(cat "$state/manager.sha256") == "$identity" ]] ||
            fail "This installation belongs to a different NVKE site or Provider key."
    else
        printf '%s\n' "$identity" >"$state/manager.sha256"
    fi
}

owned_docker() {
    docker "$2" inspect "$1" --format "{{index .Labels \"$marker\"}}" 2>/dev/null | grep -Fxq "$CLUSTER"
}

check_manager() {
    manager_inputs
    local bridge= id= reserve=3072
    docker image inspect "${CP_IMAGE:-$CP_BASE_IMAGE}" >/dev/null || fail "Build or load the pinned NVKE node image first."
    if [[ -n ${CP_IMAGE:-} ]]; then
        local kernel_mode
        kernel_mode=$(docker image inspect "$CP_IMAGE" -f '{{index .Config.Labels "vmetal.nvke.dev/kernel"}}')
        if [[ $kernel_mode == host-bind-v1 ]]; then
            [[ -r /boot/config-$(uname -r) ]] || fail "The running host kernel config is required."
        else
            [[ $kernel_mode == $(uname -r) ]] || fail "CP_IMAGE kernel config differs from this Docker host."
        fi
        [[ $(docker image inspect "$CP_IMAGE" -f '{{index .Config.Labels "vmetal.nvke.dev/kubernetes"}}') == v1.36.3 ]] || fail "CP_IMAGE must be a verified VXAnvil Kubernetes 1.36.3 image."
        [[ $(docker image inspect "$CP_IMAGE" -f '{{index .Config.Labels "vmetal.nvke.dev/helper-sha256"}}') == $(sha256sum "$repo/scripts/vxanvil-kubeadm" | cut -d' ' -f1) ]] || fail "CP_IMAGE helper differs from this release."
    fi
    if docker network inspect "$network" >/dev/null 2>&1; then
        owned_docker "$network" network || fail "Existing Docker network is not owned by this installation."
        id=$(docker network inspect "$network" --format '{{.Id}}')
        bridge="br-${id:0:12}"
        [[ $(docker network inspect "$network" --format '{{(index .IPAM.Config 0).Subnet}}') == "$DOCKER_CIDR" ]] ||
            fail "Existing network subnet differs."
    fi
    if docker container inspect "$container" >/dev/null 2>&1; then
        [[ $(docker inspect "$container" --format "{{index .Config.Labels \"$marker\"}}") == "$CLUSTER" ]] ||
            fail "Existing container is not owned by this installation."
        reserve=0
    fi
    check_network manager "$bridge"
    disk / 15 0
    disk "$(docker info --format '{{.DockerRootDir}}')" 15 "$reserve"
}

remember_key() {
    local addr=$1 key=$2
    [[ $key =~ ^ssh-ed25519\ [A-Za-z0-9+/]+={0,3}$ ]] || fail "Invalid host key for $addr."
    if ssh-keygen -F "$addr" -f "$PROVIDER_KNOWN_HOSTS" >"$state/known-host-match"; then
        awk '!/^#/ {print $2 " " $3}' "$state/known-host-match" | grep -Fxq "$key" ||
            fail "Existing SSH trust differs for $addr."
    else
        printf '\n%s %s\n' "$addr" "$key" >>"$PROVIDER_KNOWN_HOSTS"
    fi
}
register_host() {
    local hash=$1 addr=$2 user=$3 name existing
    [[ $hash =~ ^[0-9a-f]{64}$ && $user =~ ^[a-z_][a-z0-9_-]*$ ]] || fail "Invalid enrollment identity."
    name="host-${hash:0:32}"
    existing=$(mgmt get vmetalhost "$name" --ignore-not-found -o json)
    if [[ -n $existing ]]; then
        jq -e --arg cluster "$CLUSTER" --arg addr "$addr" --arg target "$user@$addr" --arg hash "$hash" '
            .metadata.labels["vmetal.nvke.dev/installer"] == $cluster and
            .spec.machineID == $hash and .spec.nodeIP == $addr and .spec.sshTarget == $target and
            (.status.claim == null or (.status.claim.name | startswith($cluster + "-")))
        ' <<<"$existing" >/dev/null || fail "Host $name already belongs to another installation."
    else
        jq -cn --arg name "$name" --arg cluster "$CLUSTER" --arg addr "$addr" --arg target "$user@$addr" --arg hash "$hash" '
            {apiVersion:"vmetal.infrastructure.cluster.x-k8s.io/v1alpha1",kind:"VMetalHost",
             metadata:{name:$name,labels:{"vmetal.nvke.dev/installer":$cluster}},
             spec:{machineID:$hash,nodeIP:$addr,sshTarget:$target,allowBootstrap:true}}
        ' | mgmt create -f - >&2
    fi
    mgmt wait --for=jsonpath='{.status.discoveryReady}'=true "vmetalhost/$name" --timeout=90s >&2
    printf '%s\n' "$name"
}
get_cluster_id() {
    nvke get clusters -o json | jq -r --arg cluster "$CLUSTER" '
        (if type=="array" then . else .clusters end)[] | select(.name==$cluster) | .id'
}
control_plane() {
    lock_state
    check_manager
    remember_manager
    local current context hash key host image="${CP_IMAGE:-$container:local}"
    current=$(get_cluster_id)
    if [[ -n $current ]]; then
        [[ -e $state/cluster-request && $(cat "$state/cluster-request") == "$CLUSTER" ]] ||
            fail "This cluster name belongs to an existing deployment."
    fi
    if ! docker network inspect "$network" >/dev/null 2>&1; then
        docker network create --label "$marker=$CLUSTER" --subnet "$DOCKER_CIDR" \
            -o com.docker.network.bridge.gateway_mode_ipv4=routed "$network"
    fi
    privilege bash "$repo/scripts/configure-vxanvil-manager-network.sh" --install "$layout"
    if ! docker container inspect "$container" >/dev/null 2>&1; then
        if [[ -z ${CP_IMAGE:-} ]]; then
        context=$(mktemp -d "$state/build.XXXXXXXX")
        mkdir "$context/scripts"
        cp "$repo/scripts/vxanvil-kubeadm" "$context/scripts/"
        cp "$repo/examples/nvke-docker-control-plane.Dockerfile" "$context/Dockerfile"
        if ! docker build --build-arg "NVKE_NODE_IMAGE=$CP_BASE_IMAGE" -t "$image" "$context"; then
            rm -rf -- "$context"
            fail "Docker control plane image build failed."
        fi
        rm -rf -- "$context"
        fi
        if docker volume inspect "$volume" >/dev/null 2>&1; then
            owned_docker "$volume" volume || fail "Existing Docker volume is not owned by this installation."
        else
            docker volume create --label "$marker=$CLUSTER" "$volume" >/dev/null
        fi
        docker run -d --name "$container" --hostname "$container" --label "$marker=$CLUSTER" \
            --restart unless-stopped --privileged --cgroupns private --network "$network" --ip "$CP_IP" \
            --cpus 3 --memory 4g --memory-swap 4g \
            --mount "type=volume,source=$volume,target=/var" \
            --mount type=bind,source=/lib/modules,target=/lib/modules,readonly \
            --mount "type=bind,source=/boot/config-$(uname -r),target=/boot/config-$(uname -r),readonly" \
            --tmpfs /run --tmpfs /tmp -p '0.0.0.0::6443/tcp' -p '0.0.0.0::15005/tcp' -p '0.0.0.0::6081/udp' "$image"
    fi
    if [[ -n ${CP_IMAGE:-} ]]; then
        wait_for 30 docker exec "$container" ctr --namespace k8s.io version >/dev/null ||
            fail "Control-plane containerd did not start."
        docker exec "$container" bash -ec '
            set -o pipefail
            if [[ ! -f /var/lib/vmetal-images-imported ]]; then
                for archive in /usr/lib/nvke/k8s-matrix/v1.36.3/*/*.tgz /usr/lib/nvke/cni-ovn/2.4.2/*.tgz; do
                    test -f "$archive"
                    gzip -dc "$archive" | ctr --namespace k8s.io images import --all-platforms -
                done
                touch /var/lib/vmetal-images-imported
            fi'
    fi
    wait_for 20 docker exec "$container" systemctl is-active --quiet ssh.service || fail "Control-plane SSH did not start."
    cloud_ready() { docker exec "$container" /usr/bin/cloud-init status | grep -Fx 'status: done'; }
    wait_for 25 cloud_ready || fail "Control-plane cloud-init did not report done."
    docker exec "$container" timeout 5 bash -c 'exec 3<>"/dev/tcp/$1/30441"' _ "$MANAGER_IP" ||
        fail "Control plane cannot reach SPIRE."
    key=$(docker exec "$container" awk '{print $1, $2}' /etc/ssh/ssh_host_ed25519_key.pub)
    remember_key "$CP_IP" "$key"
    awk 'NR==1 {print "restrict", $1, $2}' "$PROVIDER_PUBLIC_KEY" |
        docker exec -i "$container" sh -ec 'umask 077; cat >/home/vmetal/.ssh/authorized_keys; chown vmetal:vmetal /home/vmetal/.ssh/authorized_keys'
    hash=$(docker exec -i "$container" python3 - <"$repo/internal/discovery/probe.py" | jq -er '.machine_id_hash')
    host=$(register_host "$hash" "$CP_IP" vmetal)
    if [[ -z $current ]]; then
        printf '%s\n' "$CLUSTER" >"$state/cluster-request"
        cat >"$state/cluster.yaml" <<YAML
name: $CLUSTER
controlPlaneKubernetesVersion: "1.36.3"
cniPlugin: ovn-kubernetes
etcdEncrypted: false
network:
  podCIDR: "$POD_CIDR"
  serviceCIDR: "$SERVICE_CIDR"
controlPlaneConfig:
  desiredCount: 1
  systemReservedMemory: "1Gi"
  provisioningMonitorDisabled: true
  infra:
    machineTemplate:
      apiVersion: infrastructure.cluster.x-k8s.io/v1alpha1
      kind: VMetalMachineTemplate
      spec:
        template:
          spec:
            hostRef: $host
            bootstrapMode: NVKE
infra:
  cluster:
    apiVersion: infrastructure.cluster.x-k8s.io/v1alpha1
    kind: VMetalCluster
    metadata:
      name: $CLUSTER
    spec:
      controlPlaneEndpoint:
        host: "$CP_IP"
        port: 6443
YAML
        nvke create cluster -f "$state/cluster.yaml"
    fi
    if [[ ! -s $state/workload.kubeconfig ]]; then
        wait_for 45 nvke get kubeconfig "$CLUSTER" --output-file "$state/workload.kubeconfig" ||
            fail "NVKE did not publish the kubeconfig."
    fi
    [[ $(kube config view --minify -o jsonpath='{.clusters[0].cluster.server}') == "$api" ]] ||
        fail "NVKE published an unexpected workload API endpoint."
    wait_for 30 kube get --raw=/readyz
    kube wait --for=condition=Ready node -l node-role.kubernetes.io/control-plane --timeout=130s
    OVN_MTU=${OVN_MTU:-$(python3 "$repo/scripts/vxanvil-network.py" --peer "$WORKER_IP" --mtu-only)} \
      bash "$repo/scripts/configure-nvke-baremetal-ovn.sh" --apply "$state/workload.kubeconfig" "$api"
    install -d -m 0700 "$state/worker"
    install -m 0644 "$PROVIDER_PUBLIC_KEY" "$state/worker/provider.pub"
    kube config view --raw --flatten --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' |
        base64 -d >"$state/worker/ca.crt"
    test -s "$state/worker/ca.crt"
    printf 'Transfer %s/worker (public CA and Provider key) to the worker over a trusted channel.\n' "$state"
}


check_worker() {
    [[ $EUID == 0 ]] || fail "Run this phase as root on the designated worker."
    : "${WORKER_USER:?}" "${WORKER_ARTIFACTS:?}" "${WORKER_STORAGE:?}"
    [[ $WORKER_USER =~ ^[a-z_][a-z0-9_-]*$ && $WORKER_STORAGE =~ ^/[a-zA-Z0-9_./-]+$ ]] || fail "Invalid worker settings."
    [[ $(realpath -m "$WORKER_STORAGE") == "$WORKER_STORAGE" ]] || fail "Use a canonical worker storage path."
    if [[ -e /etc/kubernetes/kubelet.conf || -e /var/lib/vmetal-kubeadm/current.json ]]; then
        [[ -e $state/prepared && -s /etc/kubernetes/kubelet.conf &&
           $(awk '$1=="server:" {print $2}' /etc/kubernetes/kubelet.conf) == "$api" ]] ||
            fail "The worker has an existing allocation. Release it before installing a separate cluster."
    else
        check_network worker ""
    fi
    iface=$(address_interface "$WORKER_IP") || fail "Worker IP is not assigned locally."
    [[ $(ip -j -4 route get "$WORKER_GATEWAY" | jq -r '.[0].dev') == "$iface" ]] ||
        fail "Worker route gateway must be reachable via $iface."
    tcp "$MANAGER_IP" 30441 || fail "Worker cannot reach SPIRE."
    local reserve=6144
    if [[ -e $state/prepared ]]; then reserve=0; fi
    disk / 10 256
    disk "$WORKER_STORAGE" 15 "$reserve"
    [[ -s $state/provider.pub && -s $state/ca.crt ]] || fail "Copy the manager's worker handoff into $state."
    ssh-keygen -l -f "$state/provider.pub" >/dev/null
    [[ $(awk 'NR==1 {print $1}' "$state/provider.pub") == ssh-ed25519 ]] || fail "Use a Provider Ed25519 key."
    if [[ $(ip -j -4 route get "$CP_IP" | jq -r '.[0].gateway // empty') == "$WORKER_GATEWAY" ]]; then
        [[ $(curl --noproxy '*' --cacert "$state/ca.crt" -fsS --max-time 8 "$api/readyz") == ok ]] ||
            fail "Worker cannot verify the workload API through the configured gateway."
    else
        printf 'Workload API TLS will be checked after adding the /32 route through %s.\n' "$WORKER_GATEWAY"
    fi
    python3 "$repo/scripts/vxanvil-network.py" --peer "$CP_IP" --spire "$MANAGER_IP" \
      --cidrs "$LAN_CIDR" "$DOCKER_CIDR" "$POD_CIDR" "$SERVICE_CIDR"
    [[ -x /usr/bin/nvidia-container-runtime ]] || fail "Install NVIDIA Container Toolkit."
    nvidia-smi --query-gpu=name,memory.free --format=csv,noheader
    if [[ ! -e $state/prepared ]]; then
        python3 - <<'PY'
import ctypes
import os

libc = ctypes.CDLL(None, use_errno=True)
fd = libc.inotify_init1(os.O_CLOEXEC)
if fd < 0:
    errno = ctypes.get_errno()
    raise SystemExit(f"Cannot start a CRI CNI watcher: {os.strerror(errno)}. Check fs.inotify.max_user_instances and open-file limits.")
os.close(fd)
PY
        NVKE_NVIDIA_RUNTIME=/usr/bin/nvidia-container-runtime bash "$repo/scripts/prepare-nvke-baremetal-worker.sh" \
            --check "$WORKER_ARTIFACTS" "$WORKER_USER" "$WORKER_STORAGE"
    fi
}

authorize_worker_key() {
    local login_home authkey
    login_home=$(getent passwd "$WORKER_USER" | cut -d: -f6)
    [[ $login_home == /* && -d $login_home ]] || fail "The worker login needs a home directory."
    authkey="$login_home/.ssh/authorized_keys"
    [[ ! -L $login_home/.ssh && ! -L $authkey ]] || fail "Refusing a symlink in the worker SSH trust path."
    local key
    key=$(awk 'NR==1 {print "restrict", $1, $2}' "$state/provider.pub")
    runuser -u "$WORKER_USER" -- bash -ec '
        umask 077
        mkdir -p "$HOME/.ssh"
        chmod 0700 "$HOME/.ssh"
        touch "$1"
        chmod 0600 "$1"
        if ! grep -Fxq -- "$2" "$1"; then printf "\n%s\n" "$2" >>"$1"; fi
    ' _ "$authkey" "$key"
}

install_worker() {
    check_worker
    exec 8>/run/lock/vmetal-nvke-gpu-worker.lock
    flock -n 8 || fail "Another installer is preparing this worker."
    lock_state
    ip -4 route replace "$CP_IP/32" via "$WORKER_GATEWAY" dev "$iface" src "$WORKER_IP"
    [[ $(curl --noproxy '*' --cacert "$state/ca.crt" -fsS --max-time 8 "$api/readyz") == ok ]] ||
        fail "Worker cannot verify the published workload API."
    if [[ ! -e $state/prepared ]]; then
        NVKE_NVIDIA_RUNTIME=/usr/bin/nvidia-container-runtime bash "$repo/scripts/prepare-nvke-baremetal-worker.sh" \
            --apply "$WORKER_ARTIFACTS" "$WORKER_USER" "$WORKER_STORAGE"
        bash "$repo/scripts/prepare-nvke-baremetal-cloud-init.sh"
        authorize_worker_key
        touch "$state/prepared"
    else
        crictl --config=/dev/null --runtime-endpoint unix:///run/vmetal-nvke-containerd/containerd.sock --timeout 5s info |
            jq -e 'any(.status.conditions[]; .type=="RuntimeReady" and .status==true)' >/dev/null
    fi
    # The CNI interface exists only after join. The scoped rules can be installed before it.
    bash "$repo/scripts/configure-nvke-lab-host-recovery.sh" --install worker \
        "$WORKER_IP" "$CP_IP" "$MANAGER_IP" "$WORKER_GATEWAY" "$POD_CIDR" host-network-api
    wait_for 20 systemctl is-active --quiet vmetal-nvke-lab-worker-recovery.service ||
        fail "Worker network recovery did not become active. Check its journal."
    local hash key
    hash=$(python3 "$repo/internal/discovery/probe.py" | jq -er '.machine_id_hash')
    key=$(awk 'NR==1 {print $1, $2}' /etc/ssh/ssh_host_ed25519_key.pub)
    jq -cn --arg cluster "$CLUSTER" --arg addr "$WORKER_IP" --arg hash "$hash" --arg user "$WORKER_USER" --arg key "$key" '
        {cluster:$cluster,nodeIP:$addr,machineID:$hash,sshUser:$user,publicKey:$key}
    ' >"$state/worker-report.json"
    chmod 0644 "$state/worker-report.json"
    printf 'Return %s/worker-report.json to the manager as worker/report.json over a trusted channel.\n' "$state"
}

control_node() {
    kube get node -l node-role.kubernetes.io/control-plane -o json |
        jq -er --arg ip "$CP_IP" '.items | select(length==1) | .[0] | select(any(.status.addresses[]; .type=="InternalIP" and .address==$ip)) | .metadata.name'
}

finish_cluster() {
    lock_state
    manager_inputs
    remember_manager
    [[ -f $state/cluster-request && $(cat "$state/cluster-request") == "$CLUSTER" ]] || fail "Run control-plane first."
    [[ $(kube config view --minify -o jsonpath='{.clusters[0].cluster.server}') == "$api" ]] || fail "Workload kubeconfig endpoint differs."
    kube get --raw=/readyz >/dev/null
    local report="$state/worker/report.json" hash user key host id pool node cp claim
    jq -e --arg cluster "$CLUSTER" --arg ip "$WORKER_IP" '.cluster==$cluster and .nodeIP==$ip' "$report" >/dev/null
    hash=$(jq -er .machineID "$report")
    user=$(jq -er .sshUser "$report")
    key=$(jq -er .publicKey "$report")
    [[ $hash =~ ^[0-9a-f]{64}$ && $user =~ ^[a-z_][a-z0-9_-]*$ ]] || fail "Invalid worker report."
    claim=$(mgmt get vmetalhost "host-${hash:0:32}" --ignore-not-found -o json)
    if [[ -n $claim ]]; then
        jq -e --arg cluster "$CLUSTER" '.metadata.labels["vmetal.nvke.dev/installer"]==$cluster' <<<"$claim" >/dev/null ||
            fail "Worker is already in another inventory."
    fi
    remember_key "$WORKER_IP" "$key"
    "$PROVIDER_SSH" -n -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5 \
        "$user@$WORKER_IP" 'sudo -n -l /usr/local/sbin/vmetal-kubeadm' >/dev/null
    host=$(register_host "$hash" "$WORKER_IP" "$user")
    id=$(get_cluster_id)
    [[ $id =~ ^[a-f0-9-]{36}$ ]] || fail "Cannot resolve NVKE cluster ID."
    pool=$(nvke get nodepools --cluster "$CLUSTER" -o json | jq -r '
        (if type=="array" then . else .nodePools end)[] | select(.name=="gtx-1080-ti") | .infra.machineTemplate.spec.template.spec.hostRef')
    if [[ -z $pool ]]; then
        cat >"$state/pool.yaml" <<YAML
clusterID: $id
name: gtx-1080-ti
nodeNamePrefix: gtx
systemReservedMemory: "2Gi"
kubeReservedMemory: "512Mi"
desiredNodeCount: 1
nodeLabels:
  vmetal.nvke.dev/gateway: local
nodeTaints:
  - key: vmetal.nvke.dev/experiment
    value: "true"
    effect: NoSchedule
autoModeDisabled: true
provisioningMonitorDisabled: true
infra:
  machineTemplate:
    apiVersion: infrastructure.cluster.x-k8s.io/v1alpha1
    kind: VMetalMachineTemplate
    spec:
      template:
        spec:
          hostRef: $host
          bootstrapMode: NVKE
YAML
        nvke create nodepool -f "$state/pool.yaml"
    else
        [[ $pool == "$host" ]] || fail "Existing node pool refers to a different host."
    fi
    worker_node() {
        kube get nodes -l vmetal.nvke.dev/gateway=local -o json |
            jq -er --arg ip "$WORKER_IP" '[.items[] | select(any(.status.addresses[]; .type=="InternalIP" and .address==$ip))] | select(length==1) | .[0].metadata.name'
    }
    node=$(wait_for 60 worker_node) || fail "GTX worker did not register."
    [[ $(mgmt get vmetalhost "$host" -o jsonpath='{.status.claim.name}') == "$node" ]] || fail "Worker claim differs."
    cp=$(control_node)
    kube wait --for=condition=Ready "node/$cp" "node/$node" --timeout=180s
    printf '%s\n' "$node" >"$state/worker-node"
    deploy_gpu "$node"
    verify_cluster
}

deploy_gpu() {
    local node=$1
    kube create namespace "$ns" --dry-run=client -o yaml | kube apply -f -
    kube apply -f - <<YAML
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: vmetal-nvidia
handler: nvidia
scheduling:
  nodeSelector:
    kubernetes.io/hostname: $node
  tolerations:
    - {key: vmetal.nvke.dev/experiment, operator: Equal, value: "true", effect: NoSchedule}
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: nvidia-device-plugin
  namespace: $ns
spec:
  selector:
    matchLabels: {app: vmetal-nvidia-device-plugin}
  template:
    metadata:
      labels: {app: vmetal-nvidia-device-plugin}
    spec:
      runtimeClassName: vmetal-nvidia
      automountServiceAccountToken: false
      nodeSelector:
        kubernetes.io/hostname: $node
      tolerations:
        - {key: vmetal.nvke.dev/experiment, operator: Equal, value: "true", effect: NoSchedule}
      containers:
      - name: plugin
        image: nvcr.io/nvidia/k8s-device-plugin:v0.17.1
        env:
        - {name: NVIDIA_VISIBLE_DEVICES, value: all}
        - {name: NVIDIA_DRIVER_CAPABILITIES, value: compute,utility}
        resources:
          requests: {cpu: 20m, memory: 48Mi}
          limits: {cpu: 250m, memory: 192Mi}
        volumeMounts:
        - {name: plugins, mountPath: /var/lib/kubelet/device-plugins}
      volumes:
      - name: plugins
        hostPath: {path: /var/lib/kubelet/device-plugins, type: Directory}
YAML
    kube wait --for='jsonpath={.status.allocatable.nvidia\.com/gpu}=1' "node/$node" --timeout=145s
}

verify_cluster() (
    [[ $(kube config view --minify -o jsonpath='{.clusters[0].cluster.server}') == "$api" ]] || fail "Workload kubeconfig endpoint differs."
    local node cp probe
    node=$(cat "$state/worker-node")
    cp=$(control_node)
    kube wait --for=condition=Ready "node/$cp" "node/$node" --timeout=130s
    kube wait --for='jsonpath={.status.allocatable.nvidia\.com/gpu}=1' "node/$node" --timeout=65s
    bash "$repo/scripts/check-nvke-lab-workers.sh" "$state/workload.kubeconfig" "$api" "$cp" "$node"
    probe=$(kube -n "$ns" create -o jsonpath='{.metadata.name}' -f - <<YAML
apiVersion: v1
kind: Pod
metadata:
  generateName: api-check-
spec:
  nodeName: $cp
  automountServiceAccountToken: true
  restartPolicy: Never
  activeDeadlineSeconds: 180
  terminationGracePeriodSeconds: 1
  containers:
  - name: curl
    image: docker.io/curlimages/curl:8.12.1
    command: ["sleep", "160"]
YAML
    )
    trap 'kube -n "$ns" delete pod "$probe" --ignore-not-found --wait=false >/dev/null' EXIT
    kube -n "$ns" wait --for=condition=Ready "pod/$probe" --timeout=90s
    kube -n "$ns" exec "$probe" -- curl -fsS --max-time 12 --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
        https://kubernetes.default.svc.cluster.local/readyz
)
case "$phase" in
    check-control-plane) check_manager ;;
    control-plane) control_plane ;;
    check-worker) check_worker ;;
    worker) install_worker ;;
    join) finish_cluster ;;
    verify) verify_cluster ;;
    *) fail "Unknown phase: $phase" ;;
esac
