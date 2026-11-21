#!/bin/bash
# Fixed actions only; root state is bound to one local plan digest.
set -euo pipefail
umask 077
[[ $# == 2 && $EUID == 0 ]] || { echo 'Usage: sudo bash runtime.sh phase root' >&2; exit 2; }
phase=$1
root=$2
[[ $root =~ ^/var/lib/vmetal-bootstrap/vmetal-[a-z0-9-]+$ ]]
exec 9>"$root/runtime.lock"
flock -n 9 || { echo 'Another remote bootstrap action is still active' >&2; exit 1; }
python3 "$root/runtime/settings.py" "$root/settings.json" >"$root/settings.env"
WORKER_IPS=''
# This file is rendered locally from JSON using shlex.quote, not supplied shell.
# shellcheck source=/dev/null
source "$root/settings.env"
ROOT=$root
export CLUSTER WORKER_IP
work=$ROOT/work
bundle=$work/management-bundle
if [[ $ARTIFACT_MODE == release ]]; then bundle=$work/management-runtime; fi
provider=$bundle/provider
site_state=$ROOT/site
tenant=$ROOT/tenant
layout=$ROOT/layout.env
export PATH="$bundle/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export KUBECONFIG=$site_state/management.kubeconfig
export XDG_CONFIG_HOME=$site_state/nvkectl-config XDG_CACHE_HOME=$site_state/nvkectl-cache
export NVKE_SITE=$SITE CP_IMAGE=vmetal-control-plane:public-1.36.3
export PROVIDER_PUBLIC_KEY=$site_state/ssh-key.pub PROVIDER_KNOWN_HOSTS=$site_state/known-hosts
export PROVIDER_SSH=$site_state/bin/ssh
k() { kubectl --kubeconfig "$tenant/workload.kubeconfig" --request-timeout=20s "$@"; }
mgmt() { kubectl --kubeconfig "$KUBECONFIG" --request-timeout=20s "$@"; }

case "$phase" in
fetch-manager)
    [[ $ARTIFACT_MODE == release ]]
    python3 "$ROOT/runtime/artifacts.py" "$ROOT/settings.json" manager
    ;;
load-manager)
    python3 "$ROOT/runtime/images.py" "$ROOT/settings.json"
    ;;
prepare-manager)
    bash "$ROOT/runtime/prerequisites.sh" manager
    ;;
fetch-node)
    [[ $ARTIFACT_MODE == release ]]
    python3 "$ROOT/runtime/artifacts.py" "$ROOT/settings.json" node
    cp -al "$work/artifacts" "$ROOT/input/artifacts"
    ;;
build)
    [[ $ARTIFACT_MODE == source ]]
    bash "$ROOT/runtime/public-inputs.sh" "$work"
    ;;
management)
    bash "$provider/scripts/init-nvke-management.sh" "$bundle" "$site_state" "$MANAGER_IP" "$LAN_CIDR" "$SITE"
    ;;
control-plane)
    # This is an explicit part of the reviewed plan on a dedicated management host.
    printf 'net.ipv4.ip_forward=1\n' >"/etc/sysctl.d/90-$SITE-bootstrap-forward.conf"
    sysctl -p "/etc/sysctl.d/90-$SITE-bootstrap-forward.conf"
    mkdir -p "$tenant"
    for variable in CLUSTER MANAGER_IP LAN_CIDR DOCKER_CIDR CP_IP POD_CIDR SERVICE_CIDR WORKER_IP WORKER_GATEWAY NETWORK_MODE; do
        printf '%s=%q\n' "$variable" "${!variable}"
    done >"$layout"
    min_mtu=9000
    read -r -a mtu_peers <<<"$WORKER_IPS"
    for peer in "${mtu_peers[@]}"; do
        mtu=$(python3 "$provider/scripts/vmetal-network.py" --peer "$peer" --mtu-only)
        [[ $mtu =~ ^[0-9]+$ && $mtu -ge 576 ]]
        (( mtu >= min_mtu )) || min_mtu=$mtu
    done
    export OVN_MTU=$min_mtu
    bash "$provider/scripts/install-nvke-docker-gpu.sh" check-control-plane "$layout" "$tenant"
    bash "$provider/scripts/install-nvke-docker-gpu.sh" control-plane "$layout" "$tenant"
    # The existing installer scopes firewall rules to a single worker. Give each
    # additional peer its own immutable recovery unit instead of assuming LAN access.
    read -r -a peers <<<"$WORKER_IPS"
    for ((i=1; i<${#peers[@]}; i++)); do
        (
            CLUSTER=$SITE-peer-$i
            WORKER_IP=${peers[i]}
            peer_layout=$ROOT/layout.peer-$i.env
            for variable in CLUSTER MANAGER_IP LAN_CIDR DOCKER_CIDR CP_IP POD_CIDR SERVICE_CIDR WORKER_IP WORKER_GATEWAY NETWORK_MODE; do
                printf '%s=%q\n' "$variable" "${!variable}"
            done >"$peer_layout"
            bash "$provider/scripts/configure-vmetal-manager-network.sh" --install "$peer_layout"
        )
    done
    ;;
handoff)
    # Only public Provider key and checked runtime assets cross to workers.
    mkdir "$ROOT/handoff"
    cp -a "$provider" "$ROOT/handoff/provider"
    cp "$PROVIDER_PUBLIC_KEY" "$ROOT/handoff/provider.pub"
    ;;
protect)
    python3 "$ROOT/runtime/preserve.py" save "$ROOT/protected.json"
    ;;
check-protected)
    python3 "$ROOT/runtime/preserve.py" check "$ROOT/protected.json"
    ;;
node-assets)
    [[ $ARTIFACT_MODE == source ]]
    VMETAL_BUILD_LABEL=$SITE bash "$ROOT/runtime/public-inputs.sh" "$work" --node-only
    cp -al "$work/artifacts" "$ROOT/input/artifacts"
    ;;
tailscale-worker)
    # Change only route acceptance. Never log or replace the full preferences.
    if ! tailscale debug prefs | python3 -c 'import json,sys; sys.exit(json.load(sys.stdin).get("RouteAll") is not True)'; then
        tailscale set --accept-routes=true
    fi
    # Approval happens in the user's tailnet. Do not bypass it or substitute LAN routes.
    if ! ip -j -4 route get "$CP_IP" | python3 -c 'import json,sys; sys.exit(not any(r.get("dev")=="tailscale0" for r in json.load(sys.stdin)))'; then
        echo 'Approve the advertised API/SPIRE routes and relevant grants in the tailnet.' >&2
        exit 75
    fi
    for endpoint in "$CP_IP:6443" "$MANAGER_IP:30441"; do
        # shellcheck disable=SC2016 # Expansion belongs to the child shell.
        if ! timeout 5 bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "${endpoint%:*}" "${endpoint##*:}"; then
            echo 'Tailscale API/SPIRE path is not yet reachable; worker preparation has not started.' >&2
            exit 75
        fi
    done
    ;;
worker)
    artifacts=$ROOT/input/artifacts
    (cd "$artifacts" && sha256sum --quiet -c SHA256SUMS)
    if [[ $WORKER_KIND == baremetal ]]; then
        prep=$ROOT/input/provider/scripts/prepare-nvke-baremetal-worker.sh
        bash "$prep" --check "$artifacts" "$WORKER_USER" "$ROOT/worker-storage"
        bash "$prep" --apply "$artifacts" "$WORKER_USER" "$ROOT/worker-storage"
        python3 "$ROOT/runtime/authorize.py" "$WORKER_USER" "$ROOT/input/provider.pub"
    else
        bash "$ROOT/input/provider/scripts/prepare-nvke-vm-worker.sh" "$artifacts" "$WORKER_USER" "$ROOT/input/provider.pub"
    fi
    recovery=$ROOT/input/provider/scripts/configure-nvke-lab-host-recovery.sh
    if [[ $NETWORK_MODE == tailscale ]]; then
        bash "$recovery" --install worker "$NODE_IP" "$CP_IP" "$MANAGER_IP"
    else
        bash "$recovery" --install worker "$NODE_IP" "$CP_IP" "$MANAGER_IP" "$MANAGER_IP"
    fi
    systemctl start vmetal-nvke-lab-worker-recovery.service
    ;;
join)
    python3 "$ROOT/runtime/join.py" "$ROOT/settings.json"
    ;;
verify)
    rm -f "$ROOT/result.json"
    k get --raw=/readyz
    python3 "$ROOT/runtime/verify.py" "$ROOT/settings.json"
    ;;
*) echo 'Unknown fixed runtime phase' >&2; exit 2 ;;
esac
# A receipt is written only after the fixed phase returned success. It permits
# read-only reconciliation after an SSH response is lost; it never authorizes replay.
cp "$ROOT/plan.digest" "$ROOT/completed.$phase.next"
mv "$ROOT/completed.$phase.next" "$ROOT/completed.$phase"
