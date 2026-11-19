# Set up VMetal with NVKE

VMetal can run alongside a dedicated NVKE management site using the generic `infra` contract from NVKE revision `2f31480b360cbd7fc9d9db7a6ba274a8d0fd5049`. One Linux management host can also run a Docker workload control plane. The workload cluster can allocate separate bare-metal or virtual workers with verified NVKE artifacts for their architecture.

The addresses and paths below are examples. Choose a routable private management address and non-overlapping LAN, Docker, Pod, and Service networks. Use dedicated workers with unique hostnames and machine IDs. Verify SSH host keys before registration.

## Prepare the management site

Use the kubeconfig and tooling of an existing compatible management site, or create a new site below. Prepare a checkout of the pinned NVKE revision, the matching NVKE images, and the Linux tools `kind`, `kubectl`, `clusterctl`, `helm`, and `nvkectl`. Build `vmetal-controller` for the management host's architecture into the same tools directory:

```bash
go build -o "$LINUX_TOOL_DIR/vmetal-controller" ./cmd/vmetal-controller
```

For a Docker control plane, build the four-image bundle using the [image workflow](operations.md#build-a-reproducible-control-plane-image). For a management-only bundle:

```bash
bash scripts/package-nvke-management.sh "$NVKE_CHECKOUT" "$LINUX_TOOL_DIR" \
  "$KIND_NODE_IMAGE" "$NVKE_CONTROLLER_IMAGE" "$NVKE_PROFILE_IMAGE" /path/to/management-bundle
```

On the Linux management host, set `bundle` to the output directory and run the bundled installer. It verifies the bundle, creates a Kind cluster, and installs CAPI, NVKE, SPIRE and VMetal. The state directory contains credentials and generated configuration. Keep it on the management host with its original permissions.

```bash
bundle=/path/to/management-bundle
sudo bash "$bundle/provider/scripts/init-nvke-management.sh" \
  "$bundle" /var/lib/vmetal-nvke \
  192.168.50.10 192.168.50.0/24 vmetal-site
```

## Register a worker

Issue an enrollment bundle for an unallocated worker from the management host. The worker needs the matching verified NVKE worker artifacts, and it must be able to reach the site's SPIRE endpoint and enrollment service. Deliver the invitation to the intended host via a trusted channel.

```bash
sudo bash "$bundle/provider/scripts/issue-nvke-enrollment.sh" \
  /var/lib/vmetal-nvke /root/worker01-invitation
```

On the worker, run the enrollment script. On the management host, inspect the registered inventory with the management kubeconfig. A host is ready to allocate when discovery is ready, bootstrapping is allowed, and its claim is empty.

```bash
sudo bash /path/to/worker01-invitation/enroll.sh /path/to/verified-worker-artifacts
```

```bash
sudo "$bundle/bin/kubectl" --kubeconfig /var/lib/vmetal-nvke/management.kubeconfig get vmetalhosts
```

Use [the NVKE adapter example](development.md#test-the-nvke-infra-adapter) to allocate a control plane and the first worker. Compare the workload Node's `spec.providerID` with the management cluster Machine's `spec.providerID`. A reboot of the same allocation should preserve this identifier. Releasing and allocating again creates a new Machine.

## Use a Docker control plane with an NVIDIA GPU worker

This example uses a management host and an unallocated Ubuntu AMD64 NVIDIA GPU worker. The worker is tainted `vmetal.nvke.dev/experiment=true:NoSchedule`; workloads need a matching toleration. Use the generic adapter for other worker layouts. The worker requires a working NVIDIA driver and Container Toolkit. The management host requires the NVKE node base image and the existing management tools. Copy `examples/nvke-docker-gpu.env.example` to a trusted local `layout.env` on both systems and set the addresses for your layout.

Use a root shell on the management host for the root-owned site state. Select the verified `CP_IMAGE` included in the bundle. The installer uses the site's generated Provider SSH wrapper.

```bash
provider_dir=/path/to/management-bundle/provider
MGMT_STATE=/var/lib/vmetal-nvke
export PATH="/path/to/management-bundle/bin:$PATH"
export KUBECONFIG="$MGMT_STATE/management.kubeconfig"
export XDG_CONFIG_HOME="$MGMT_STATE/nvkectl-config"
export XDG_CACHE_HOME="$MGMT_STATE/nvkectl-cache"
export NVKE_SITE=vmetal-site
# Use the verified image included by package-nvke-management.sh.
export CP_IMAGE=vmetal-control-plane:1.36.3
export PROVIDER_PUBLIC_KEY="$MGMT_STATE/ssh-key.pub"
export PROVIDER_KNOWN_HOSTS="$MGMT_STATE/known-hosts"
export PROVIDER_SSH="$MGMT_STATE/bin/ssh"

install_dir="$HOME/vmetal-docker-gpu-install"
bash "$provider_dir/scripts/install-nvke-docker-gpu.sh" check-control-plane layout.env "$install_dir"
bash "$provider_dir/scripts/install-nvke-docker-gpu.sh" control-plane layout.env "$install_dir"
```

On the worker, copy the bundle's runtime `provider/` directory and the same trusted layout. Copy the installer's public `provider.pub` and workload `ca.crt` from `$install_dir/worker/` into the worker state directory. Set `provider_dir` to the copied directory, then run preflight and preparation:

```bash
provider_dir=/path/to/provider
worker_state=/var/lib/vmetal-gpu-install
sudo env WORKER_USER=ubuntu WORKER_ARTIFACTS=/path/to/verified-worker-artifacts \
  WORKER_STORAGE=/var/lib/vmetal-nvke-worker \
  bash "$provider_dir/scripts/install-nvke-docker-gpu.sh" check-worker layout.env "$worker_state"
sudo env WORKER_USER=ubuntu WORKER_ARTIFACTS=/path/to/verified-worker-artifacts \
  WORKER_STORAGE=/var/lib/vmetal-nvke-worker \
  bash "$provider_dir/scripts/install-nvke-docker-gpu.sh" worker layout.env "$worker_state"
```

Transfer the worker's `worker-report.json` to `$install_dir/worker/report.json` on the management host over a trusted channel. Then join and verify. The verifier checks Node readiness, NVIDIA GPU registration, agent certificates, cross-node Pod HTTP, and in-cluster access to the Kubernetes API.

```bash
bash "$provider_dir/scripts/install-nvke-docker-gpu.sh" join layout.env "$install_dir"
bash "$provider_dir/scripts/install-nvke-docker-gpu.sh" verify layout.env "$install_dir"
kubectl --kubeconfig "$install_dir/workload.kubeconfig" get nodes -o wide -L kubernetes.io/arch
```

For a remote ARM64 worker such as DGX Spark, prepare the verified ARM64 artifacts and use the [generic NVKE adapter workflow](development.md#test-the-nvke-infra-adapter). Validate routing between the workload API, SPIRE, and the worker, and confirm Pod network connectivity before scheduling work.
