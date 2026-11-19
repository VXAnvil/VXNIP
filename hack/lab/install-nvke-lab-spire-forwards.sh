#!/bin/bash
set -euo pipefail

if [[ $# != 2 || $1 != /* || ! $2 =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ || $2 == 0.0.0.0 ]]; then
    echo "Usage: bash $0 /absolute/path/to/lab-management.kubeconfig lab-bind-ipv4" >&2
    exit 2
fi
kubeconfig=$1
bind_address=$2
kubectl=$(command -v kubectl)
context=kind-vmetal-validation
[[ -r $kubeconfig && $kubeconfig != *[\"%\\]* && $kubeconfig != *$'\n'* ]]
[[ $( "$kubectl" --kubeconfig "$kubeconfig" config current-context ) == "$context" ]] ||
    { echo "Expected the isolated lab management context: $context" >&2; exit 1; }
ip -4 -o address show | awk '{print $4}' | cut -d/ -f1 | grep -Fx "$bind_address" >/dev/null
"$kubectl" --kubeconfig "$kubeconfig" --context "$context" -n spire-server \
    get svc spire-server spire-spiffe-oidc-discovery-provider >/dev/null

unit_directory="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
temp=$(mktemp -d)
trap 'rm -rf -- "$temp"' EXIT
units=()
for component in spire oidc; do
    case "$component" in
        spire) service=spire-server; port=30441 ;;
        oidc) service=spire-spiffe-oidc-discovery-provider; port=30444 ;;
    esac
    unit="vmetal-nvke-lab-$component-forward.service"
    units+=("$unit")
    cat >"$temp/$unit" <<UNIT
[Unit]
Description=NVKE VMetal lab $component port forward
StartLimitIntervalSec=0

[Service]
ExecStart=$kubectl --kubeconfig="$kubeconfig" --context=$context --namespace=spire-server port-forward --address=$bind_address svc/$service $port:443
Restart=always
RestartSec=3s

[Install]
WantedBy=default.target
UNIT
    if [[ -e $unit_directory/$unit || -L $unit_directory/$unit ]]; then
        cmp -s "$temp/$unit" "$unit_directory/$unit" ||
            { echo "An existing unit differs: $unit_directory/$unit" >&2; exit 1; }
    fi
done
install -d -m 0755 "$unit_directory"
for unit in "${units[@]}"; do install -m 0644 "$temp/$unit" "$unit_directory/$unit"; done
systemctl --user daemon-reload
systemctl --user enable --now "${units[@]}"
