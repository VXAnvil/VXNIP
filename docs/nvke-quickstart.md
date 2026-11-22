# Set up VXNIP with NVKE

VXNIP can run alongside a dedicated NVKE management site using the generic `infra` contract from NVKE revision `2f31480b360cbd7fc9d9db7a6ba274a8d0fd5049`. One Linux management host can also run a Docker workload control plane. The workload cluster can allocate separate bare-metal or virtual workers with verified NVKE artifacts for their architecture.

The addresses and paths below are examples. Choose a routable private management address and non-overlapping LAN, Docker, Pod, and Service networks. Use dedicated workers with unique hostnames and machine IDs. Verify SSH host keys before registration.

Starting with a fresh Ubuntu host and no NVKE assets? First follow [Prepare public inputs](public-inputs.md). It supplies the fixed checkout, tools, node artifacts and four-image bundle used below without private registry access.

## Prepare the management site

If you followed the public-inputs guide, use its verified `management-bundle/` and proceed to the installer command below. The following build and packaging commands are alternatives for users preparing their own compatible inputs.

Use the kubeconfig and tooling of an existing compatible management site, or create a new site below. Prepare a checkout of the pinned NVKE revision, the matching NVKE images, and the Linux tools `kind`, `kubectl`, `clusterctl`, `helm`, and `nvkectl`. The public-inputs guide extracts `vxnip-controller` from the Release image. Alternatively, build it for the management host's architecture into the same tools directory:

```bash
go build -o "$LINUX_TOOL_DIR/vxnip-controller" ./cmd/vxnip-controller
```

For a Docker control plane, build the four-image bundle using the [image workflow](operations.md#build-a-reproducible-control-plane-image). For a management-only bundle:

```bash
bash scripts/package-nvke-management.sh "$NVKE_CHECKOUT" "$LINUX_TOOL_DIR" \
  "$KIND_NODE_IMAGE" "$NVKE_CONTROLLER_IMAGE" "$NVKE_PROFILE_IMAGE" /path/to/management-bundle
```

On the Linux management host, set `bundle` to the output directory and run the bundled installer. It verifies the bundle, creates a Kind cluster, and installs CAPI, NVKE, SPIRE and VXNIP. The state directory contains credentials and generated configuration. Keep it on the management host with its original permissions.

```bash
bundle=/path/to/management-bundle
sudo bash "$bundle/provider/scripts/init-nvke-management.sh" \
  "$bundle" /var/lib/vxnip-nvke \
  192.168.50.10 192.168.50.0/24 vxnip-site
```

<a id="register-a-worker"></a>

## Register an Ubuntu VM worker

Issue an enrollment bundle for an unallocated worker from the management host. The worker needs the matching verified NVKE worker artifacts, and it must be able to reach the site's SPIRE endpoint and enrollment service. Deliver the invitation to the intended host via a trusted channel.

```bash
sudo bash "$bundle/provider/scripts/issue-nvke-enrollment.sh" \
  /var/lib/vxnip-nvke /root/worker01-invitation
```

On the worker, run the enrollment script. On the management host, inspect the registered inventory with the management kubeconfig. A host is ready to allocate when discovery is ready, bootstrapping is allowed, and its claim is empty.

```bash
sudo bash /path/to/worker01-invitation/enroll.sh /path/to/verified-worker-artifacts
```

```bash
sudo "$bundle/bin/kubectl" --kubeconfig /var/lib/vxnip-nvke/management.kubeconfig get vxniphosts
```

Use [the NVKE adapter example](development.md#test-the-nvke-infra-adapter) to allocate a control plane and the first worker. Compare the workload Node's `spec.providerID` with the management cluster Machine's `spec.providerID`. A reboot of the same allocation should preserve this identifier. Releasing and allocating again creates a new Machine.

<a id="use-a-docker-control-plane-with-an-nvidia-gpu-worker"></a>

## Create a Docker control plane

The `check-control-plane` and `control-plane` phases create the same Docker tenant for CPU or GPU workers; these phases do not require a GPU. Copy `examples/nvke-docker-gpu.env.example` to a trusted local `layout.env` and set unused networks and the real manager/worker addresses. Use the verified control-plane image built for this management host. CPU VM users follow the enrollment and CPU section; AMD64 bare-metal GPU users follow the GPU worker section.

Use a root shell on the management host for the root-owned site state. Select the verified `CP_IMAGE` included in the bundle. The installer uses the site's generated Provider SSH wrapper.

```bash
provider_dir=/path/to/management-bundle/provider
MGMT_STATE=/var/lib/vxnip-nvke
export PATH="/path/to/management-bundle/bin:$PATH"
export KUBECONFIG="$MGMT_STATE/management.kubeconfig"
export XDG_CONFIG_HOME="$MGMT_STATE/nvkectl-config"
export XDG_CACHE_HOME="$MGMT_STATE/nvkectl-cache"
export NVKE_SITE=vxnip-site
# Use the verified image included by package-nvke-management.sh.
export CP_IMAGE=vxnip-control-plane:1.36.3
export PROVIDER_PUBLIC_KEY="$MGMT_STATE/ssh-key.pub"
export PROVIDER_KNOWN_HOSTS="$MGMT_STATE/known-hosts"
export PROVIDER_SSH="$MGMT_STATE/bin/ssh"

install_dir="$HOME/vxnip-docker-gpu-install"
bash "$provider_dir/scripts/install-nvke-docker-gpu.sh" check-control-plane layout.env "$install_dir"
bash "$provider_dir/scripts/install-nvke-docker-gpu.sh" control-plane layout.env "$install_dir"
```

## Join a CPU-only Ubuntu VM

Use the [VM enrollment](#register-an-ubuntu-vm-worker) steps once on a fresh VM, with the `artifacts/` produced by the public-inputs guide. The invitation contains a one-time credential; transfer the whole directory privately. A successful check-in creates a verified, unclaimed Host. The enrollment timer subsequently refreshes its address without another invitation.

The worker needs a route to the Docker API through the management host. Copy the same trusted `layout.env` to the worker and use its addresses:

```bash
source /path/to/layout.env
sudo bash /path/to/worker01-invitation/configure-nvke-lab-host-recovery.sh \
  --install worker "$WORKER_IP" "$CP_IP" "$MANAGER_IP" "$WORKER_GATEWAY"
sudo systemctl start vxnip-nvke-lab-worker-recovery.service
ip -4 route get "$CP_IP"
```

Back in the management host's root shell, retain the environment and `install_dir` from the control-plane steps. Read the trusted layout and select the Host by its observed worker address. Check that it is verified and unclaimed before allocating it:

```bash
source layout.env
WORKER_HOST=$(kubectl get vxniphosts -o json | jq -er --arg ip "$WORKER_IP" \
  '[.items[] | select(.spec.nodeIP == $ip)] | select(length == 1) | .[0].metadata.name')
kubectl wait --for='jsonpath={.status.discoveryReady}=true' \
  "vxniphost/$WORKER_HOST" --timeout=120s
kubectl get vxniphost "$WORKER_HOST" -o json | \
  jq -e '(.status.claim.uid // "") == ""'
CLUSTER_ID=$(nvkectl --site "$NVKE_SITE" get clusters -o json | \
  jq -er --arg name "$CLUSTER" \
  '(if type == "array" then . else .clusters end)[] | select(.name == $name) | .id')

cat >"$install_dir/cpu-pool.yaml" <<YAML
clusterID: $CLUSTER_ID
name: cpu
nodeNamePrefix: cpu
desiredNodeCount: 1
systemReservedMemory: "512Mi"
kubeReservedMemory: "256Mi"
nodeLabels:
  vxnip.nvke.dev/gateway: local
autoModeDisabled: true
provisioningMonitorDisabled: true
infra:
  machineTemplate:
    apiVersion: infrastructure.cluster.x-k8s.io/v1alpha1
    kind: VXNIPMachineTemplate
    spec:
      template:
        spec:
          hostRef: $WORKER_HOST
          bootstrapMode: NVKE
YAML
nvkectl --site "$NVKE_SITE" create nodepool -f "$install_dir/cpu-pool.yaml"

kubectl wait --for='jsonpath={.status.claim.name}' \
  "vxniphost/$WORKER_HOST" --timeout=180s
WORKER_NODE=$(kubectl get vxniphost "$WORKER_HOST" -o jsonpath='{.status.claim.name}')
MACHINE_NAMESPACE=$(kubectl get vxniphost "$WORKER_HOST" -o jsonpath='{.status.claim.namespace}')
kubectl --kubeconfig "$install_dir/workload.kubeconfig" wait --for=create \
  "node/$WORKER_NODE" --timeout=300s
kubectl --kubeconfig "$install_dir/workload.kubeconfig" wait --for=condition=Ready \
  "node/$WORKER_NODE" --timeout=300s
kubectl --kubeconfig "$install_dir/workload.kubeconfig" get nodes -o wide

NODE_PROVIDER_ID=$(kubectl --kubeconfig "$install_dir/workload.kubeconfig" \
  get node "$WORKER_NODE" -o jsonpath='{.spec.providerID}')
MACHINE_PROVIDER_ID=$(kubectl -n "$MACHINE_NAMESPACE" \
  get vxnipmachine.infrastructure.cluster.x-k8s.io "$WORKER_NODE" -o jsonpath='{.spec.providerID}')
test -n "$NODE_PROVIDER_ID" && test "$NODE_PROVIDER_ID" = "$MACHINE_PROVIDER_ID"
CP_NODE=$(kubectl --kubeconfig "$install_dir/workload.kubeconfig" get nodes \
  -l node-role.kubernetes.io/control-plane -o jsonpath='{.items[0].metadata.name}')
bash "$provider_dir/scripts/check-nvke-lab-workers.sh" \
  "$install_dir/workload.kubeconfig" "https://$CP_IP:6443" "$CP_NODE" "$WORKER_NODE"
```

The `vxnip.nvke.dev/gateway: local` label selects the worker OVN DaemonSet created during control-plane setup.

Expected result: two Ready workload Nodes, a matching `vxnip://` worker provider ID, and HTTP success in both directions. The HTTP script creates and removes its own temporary namespace. This CPU path does not install a GPU plugin or model service.

## Join an NVIDIA GPU worker

This installer path prepares an unallocated Ubuntu AMD64 NVIDIA GPU worker with an existing working NVIDIA driver and Container Toolkit. It taints the worker `vxnip.nvke.dev/experiment=true:NoSchedule`; workloads need a matching toleration. Use the generic adapter for other layouts. Use this path instead of the VM enrollment path for that worker.

On the worker, copy the bundle's runtime `provider/` directory and the same trusted layout. Copy the installer's public `provider.pub` and workload `ca.crt` from `$install_dir/worker/` into the worker state directory. Set `provider_dir` to the copied directory, then run preflight and preparation:

```bash
provider_dir=/path/to/provider
worker_state=/var/lib/vxnip-gpu-install
sudo env WORKER_USER=ubuntu WORKER_ARTIFACTS=/path/to/verified-worker-artifacts \
  WORKER_STORAGE=/var/lib/vxnip-nvke-worker \
  bash "$provider_dir/scripts/install-nvke-docker-gpu.sh" check-worker layout.env "$worker_state"
sudo env WORKER_USER=ubuntu WORKER_ARTIFACTS=/path/to/verified-worker-artifacts \
  WORKER_STORAGE=/var/lib/vxnip-nvke-worker \
  bash "$provider_dir/scripts/install-nvke-docker-gpu.sh" worker layout.env "$worker_state"
```

Transfer the worker's `worker-report.json` to `$install_dir/worker/report.json` on the management host over a trusted channel. Then join and verify. The verifier checks Node readiness, NVIDIA GPU registration, agent certificates, cross-node Pod HTTP, and in-cluster access to the Kubernetes API.

```bash
bash "$provider_dir/scripts/install-nvke-docker-gpu.sh" join layout.env "$install_dir"
bash "$provider_dir/scripts/install-nvke-docker-gpu.sh" verify layout.env "$install_dir"
kubectl --kubeconfig "$install_dir/workload.kubeconfig" get nodes -o wide -L kubernetes.io/arch
```

For a remote ARM64 worker such as DGX Spark, prepare the verified ARM64 artifacts and use the [generic NVKE adapter workflow](development.md#test-the-nvke-infra-adapter). Validate routing between the workload API, SPIRE, and the worker, and confirm Pod network connectivity before scheduling work.
