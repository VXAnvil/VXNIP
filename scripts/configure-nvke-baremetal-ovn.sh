#!/bin/bash
set -euo pipefail

if [[ $# != 3 || ( $1 != --check && $1 != --apply ) || $3 != https://* ]]; then
    echo "Usage: bash $0 --check|--apply workload-kubeconfig expected-https-api-url" >&2
    exit 2
fi
mode=$1
kubeconfig=$2
expected_api=$3
kubectl_args=(--kubeconfig "$kubeconfig")
actual_api=$(kubectl "${kubectl_args[@]}" config view --minify -o jsonpath='{.clusters[0].cluster.server}')
[[ $actual_api == "$expected_api" ]] || { echo "Workload API mismatch: $actual_api" >&2; exit 1; }
kubectl_args+=(-n ovn-kubernetes)

dir=$(mktemp -d)
trap 'rm -rf -- "$dir"' EXIT
kubectl "${kubectl_args[@]}" get ds ovnkube-node -o json >"$dir/original.json"
jq -e '(.spec.updateStrategy.type == "OnDelete" or .spec.updateStrategy.type == "RollingUpdate") and
    .spec.selector.matchLabels == {app:"ovnkube-node"} and
    (.spec.template.spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms | length) == 1 and
    any(.spec.template.spec.containers[]; .name == "ovnkube-controller" and
        any(.env[]; .name=="OVN_GATEWAY_MODE" and .value=="shared"))' "$dir/original.json" >/dev/null ||
    { echo "Expected the OVN 2.4.2 shared-gateway DaemonSet." >&2; exit 1; }

jq --arg key 'homelab.nvke.dev/gateway' '
    {apiVersion, kind, metadata:{name:"ovnkube-node-homelab-baremetal", namespace:.metadata.namespace}, spec}
    | .spec.selector.matchLabels.app = .metadata.name
    | .spec.template.metadata.labels.app = .metadata.name
    | .spec.template.spec.nodeSelector[$key] = "local"
    | .spec.template.spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions
        |= map(select(.key != $key))
    | (.spec.template.spec.containers[] | select(.name == "ovnkube-controller")) |= (
        .command = ["/bin/bash","-ec","until ovs-vsctl --timeout=15 --may-exist add-br br-ex; do sleep 1; done; ip address replace 10.44.0.1/32 dev br-ex; ip link set br-ex up; exec /root/ovnkube.sh ovnkube-controller-with-node"]
        | .env |= (map(select(.name != "OVN_GATEWAY_MODE" and .name != "OVN_GATEWAY_OPTS" and .name != "OVNKUBE_POD_HOST_PORT_ENABLE"))
          + [{name:"OVN_GATEWAY_MODE",value:"local"},
             {name:"OVN_GATEWAY_OPTS",value:"--gateway-interface=br-ex --allow-no-uplink"},
             {name:"OVNKUBE_POD_HOST_PORT_ENABLE",value:"false"}])
    )
' "$dir/original.json" >"$dir/baremetal.json"

patch=$(jq -c '
    (if .spec.updateStrategy.type == "OnDelete" then [] else [
        {op:"test", path:"/spec/updateStrategy", value:.spec.updateStrategy},
        {op:"replace", path:"/spec/updateStrategy", value:{type:"OnDelete"}}
    ] end) as $strategy
    | .spec.template.spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions as $expressions
    | (if any($expressions[]; .key == "homelab.nvke.dev/gateway") then
        if any($expressions[]; .key=="homelab.nvke.dev/gateway" and .operator=="NotIn" and .values==["local"])
        then [] else error("Unexpected existing Homelab gateway selector") end
      else [
        {op:"test", path:"/spec/template/spec/affinity/nodeAffinity/requiredDuringSchedulingIgnoredDuringExecution/nodeSelectorTerms/0/matchExpressions", value:$expressions},
        {op:"add", path:"/spec/template/spec/affinity/nodeAffinity/requiredDuringSchedulingIgnoredDuringExecution/nodeSelectorTerms/0/matchExpressions/-", value:{key:"homelab.nvke.dev/gateway",operator:"NotIn",values:["local"]}}
      ] end) as $affinity
    | $strategy + $affinity
' "$dir/original.json")
if [[ $patch != '[]' ]]; then
    kubectl "${kubectl_args[@]}" patch ds ovnkube-node --type=json -p "$patch" --dry-run=server
fi
kubectl "${kubectl_args[@]}" apply --dry-run=server -f "$dir/baremetal.json"
[[ $mode == --apply ]] || exit 0

if [[ $patch != '[]' ]]; then
    kubectl "${kubectl_args[@]}" patch ds ovnkube-node --type=json -p "$patch"
fi
kubectl "${kubectl_args[@]}" apply -f "$dir/baremetal.json"
kubectl "${kubectl_args[@]}" get ds ovnkube-node ovnkube-node-homelab-baremetal
