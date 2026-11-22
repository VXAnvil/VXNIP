#!/bin/bash
set -euo pipefail

if [[ $# != 4 || $2 != https://* || $3 == "$4" ]]; then
    echo "Usage: bash $0 workload.kubeconfig expected-api-url worker-a worker-b" >&2
    exit 2
fi
kubeconfig=$1
expected_api=$2
workers=("$3" "$4")
k=(kubectl --kubeconfig "$kubeconfig" --request-timeout=15s)
api=$("${k[@]}" config view --minify -o jsonpath='{.clusters[0].cluster.server}')
[[ $api == "$expected_api" ]] || { echo "Unexpected workload API: $api" >&2; exit 1; }
"${k[@]}" wait --for=condition=Ready "node/$3" "node/$4" --timeout=20s

dir=$(mktemp -d)
ns=
# shellcheck disable=SC2329 # Invoked indirectly by the EXIT trap.
cleanup() {
    if [[ -n $ns ]]; then
        "${k[@]}" delete namespace "$ns" --wait=false >/dev/null || true
    fi
    rm -rf -- "$dir"
}
trap cleanup EXIT

for node in "${workers[@]}"; do
    ip=$("${k[@]}" get node "$node" -o json | jq -er \
        '[.status.addresses[] | select(.type == "InternalIP") | .address | select(test("^[0-9.]+$"))][0]')
    # Read only the public certificate. The server can require a client certificate
    # to finish the handshake, but still presents its own certificate first.
    timeout 5 openssl s_client -connect "$ip:15005" -showcerts </dev/null >"$dir/tls" 2>/dev/null || true
    if ! openssl x509 -in "$dir/tls" -noout -checkend 900 >/dev/null 2>&1; then
        echo "$node: NVKE agent TLS certificate is unavailable or expires within 15 minutes." >&2
        exit 1
    fi
    printf '%s: NVKE agent certificate ' "$node"
    openssl x509 -in "$dir/tls" -noout -enddate
done

ns=$("${k[@]}" create -f - -o jsonpath='{.metadata.name}' <<'YAML'
apiVersion: v1
kind: Namespace
metadata:
  generateName: vxnip-worker-check-
YAML
)
for i in 0 1; do
    node=${workers[i]}
    name="worker-$i"
    "${k[@]}" -n "$ns" apply -f - <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $name
spec:
  nodeName: $node
  automountServiceAccountToken: false
  restartPolicy: Never
  terminationGracePeriodSeconds: 1
  tolerations:
    - {key: vxnip.nvke.dev/experiment, operator: Equal, value: "true", effect: NoSchedule}
  containers:
    - name: http
      image: docker.io/library/busybox:1.36.1
      command: ["sh", "-ec", "mkdir -p /tmp/http; echo $name > /tmp/http/index.html; exec httpd -f -p 8080 -h /tmp/http"]
      readinessProbe:
        httpGet: {path: /, port: 8080}
        periodSeconds: 1
        timeoutSeconds: 5
        failureThreshold: 90
      resources:
        requests: {cpu: 5m, memory: 1Mi}
        limits: {cpu: 100m, memory: 16Mi}
YAML
done
if ! "${k[@]}" -n "$ns" wait --for=condition=Ready pods --all --timeout=120s; then
    "${k[@]}" -n "$ns" get pods -o wide
    "${k[@]}" -n "$ns" get events --sort-by=.lastTimestamp | tail -20
    exit 1
fi
failed=0
for i in 0 1; do
    src="worker-$i"
    dst="worker-$((1 - i))"
    ip=$("${k[@]}" -n "$ns" get pod "$dst" -o jsonpath='{.status.podIP}')
    if reply=$("${k[@]}" -n "$ns" exec "$src" -- wget -T 5 -qO- "http://$ip:8080/") && [[ $reply == "$dst" ]]; then
        printf '%s → %s (%s): HTTP OK\n' "${workers[i]}" "${workers[1-i]}" "$ip"
    else
        printf '%s → %s (%s): HTTP failed\n' "${workers[i]}" "${workers[1-i]}" "$ip" >&2
        failed=1
    fi
done
exit "$failed"
