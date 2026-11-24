# Hand off a usable site client

Run commands on the manager. Replace `/home/operator`, the site and workload names with the actual client paths printed by the executor. Do not assume that a bare `--site` flag loads a private configuration directory.

The exported executable binds its own configuration, cache and default site, including from a fresh SSH shell:

```bash
/home/operator/.config/vxnip/sites/vxnip-lab/bin/nvkectl get clusters
/home/operator/.config/vxnip/sites/vxnip-lab/bin/nvkectl get nodepools --cluster vxnip-w-lab
/home/operator/.config/vxnip/sites/vxnip-lab/bin/workload-kubectl get nodes -o wide
```

For conventional command names, source the generated `env.sh` in that manager shell. Workload queries still select the workload kubeconfig explicitly:

```bash
source /home/operator/.config/vxnip/sites/vxnip-lab/env.sh
nvkectl --site vxnip-lab get clusters
export WK=/home/operator/.config/vxnip/sites/vxnip-lab/workload.kubeconfig
kubectl --kubeconfig "$WK" get nodes -o wide
kubectl --kubeconfig "$WK" get nodes -o 'custom-columns=NODE:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu'
kubectl --kubeconfig "$WK" get pods -A -o wide
kubectl --kubeconfig "$WK" -n kube-system get ds nvidia-device-plugin-daemonset
kubectl --kubeconfig "$WK" -n kube-system logs -l app.kubernetes.io/name=nvidia-device-plugin --tail=50 --prefix
kubectl --kubeconfig "$WK" get events -A --sort-by=.lastTimestamp
```

GPU Allocatable is the total scheduling capacity, not the idle count. Inspect `describe node NODE` and its allocated resources for current use.

VXNIP inventory uses the **management** kubeconfig:

```bash
kubectl --kubeconfig /home/operator/.config/vxnip/sites/vxnip-lab/management.kubeconfig get vxniphosts
kubectl --kubeconfig /home/operator/.config/vxnip/sites/vxnip-lab/management.kubeconfig get machines.cluster.x-k8s.io -A
```

The pinned NVKE `get providers` command reports built-in providers; it is not the VXNIP inventory/readiness check. `Provisioned` is NVKE's provisioning status; report Node Ready, actual GPU capacity and fresh network/CUDA probes separately. Pool names are `worker-1`, `worker-2`, etc.; they do not identify CPU versus GPU capability. Keep existing names unless the user explicitly requests a disruptive lifecycle change.

For Pod egress faults, compare host HTTPS with an ordinary Pod first, then distinguish cluster DNS, external DNS, numeric-IP TCP and verified HTTPS. Node Ready or a successful image pull is not Pod egress evidence. See [egress.md](egress.md) for scoped firewall inspection and recovery.
