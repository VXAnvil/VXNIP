#!/bin/bash
set -euo pipefail
[[ $# == 3 ]] || exit 2
k=(kubectl --kubeconfig "$1" --request-timeout=20s)
ns=
# shellcheck disable=SC2329 # Invoked by the EXIT trap, including failed probes.
cleanup() { [[ -z $ns ]] || "${k[@]}" delete namespace "$ns" --wait=false >/dev/null; }
trap 'cleanup' EXIT
ns=$("${k[@]}" create -f - -o jsonpath='{.metadata.name}' <<'YAML'
apiVersion: v1
kind: Namespace
metadata:
  generateName: vxnip-service-check-
YAML
)
"${k[@]}" -n "$ns" create -f - <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: server
  labels: {app: http}
spec:
  nodeName: $2
  automountServiceAccountToken: false
  restartPolicy: Never
  tolerations:
    - {operator: Exists}
  containers:
    - name: http
      image: docker.io/library/busybox:1.36.1
      command: [sh, -ec, "mkdir -p /tmp/http; echo vxnip-ok >/tmp/http/index.html; exec httpd -f -p 8080 -h /tmp/http"]
      resources:
        requests: {cpu: 5m, memory: 2Mi}
        limits: {cpu: 100m, memory: 16Mi}
---
apiVersion: v1
kind: Service
metadata: {name: http}
spec:
  selector: {app: http}
  ports: [{port: 8080, targetPort: 8080}]
---
apiVersion: v1
kind: Pod
metadata: {name: client}
spec:
  nodeName: $3
  automountServiceAccountToken: false
  restartPolicy: Never
  tolerations:
    - {operator: Exists}
  containers:
    - name: client
      image: docker.io/library/busybox:1.36.1
      command: [sh, -ec, "sleep 600"]
      resources:
        requests: {cpu: 5m, memory: 2Mi}
        limits: {cpu: 100m, memory: 16Mi}
YAML
"${k[@]}" -n "$ns" wait --for=condition=Ready pods --all --timeout=120s
# EndpointSlice propagation and DNS programming can lag behind Pod Ready.
for attempt in $(seq 1 20); do
    if reply=$("${k[@]}" -n "$ns" exec client -- wget -T 5 -qO- "http://http.$ns.svc.cluster.local:8080/") && [[ $reply == vxnip-ok ]]; then
        printf 'Service DNS and HTTP: OK\n'
        exit 0
    fi
    printf 'Waiting for Service DNS/HTTP (%s/20)\n' "$attempt" >&2
    sleep 3
done
exit 1
