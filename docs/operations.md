# Install, diagnose and migrate VMetal

## Build a reproducible control-plane image

Build on a Linux host whose native architecture matches the verified NVKE artifacts. Supply a dependency image with systemd and the pinned NVKE node dependencies. The builder installs Kubernetes 1.36.3, OVN 2.4.2, NVKE agent, SPIRE agent and their services from the checksum-verified artifacts. SSH, Python, sudo and cloud-init are installed at build time if missing. The published bundle carries the resulting image.

```bash
bash scripts/build-nvke-control-plane.sh \
  cwe-capd-kindest-node:local /path/to/verified-amd64-artifacts \
  vmetal-control-plane:1.36.3 /boot/config-$(uname -r)

VMETAL_CONTROL_PLANE_IMAGE=vmetal-control-plane:1.36.3 \
  bash scripts/package-nvke-management.sh \
    "$NVKE_CHECKOUT" "$LINUX_TOOL_DIR" \
    "$KIND_NODE_IMAGE" "$NVKE_CONTROLLER_IMAGE" "$NVKE_PROFILE_IMAGE" \
    /path/to/new-management-bundle
```

The bundle contains provider scripts, manifests, the discovery probe, tools, image IDs and a SHA-256 manifest. Provider files are selected explicitly by [`config/management-bundle-files.txt`](../config/management-bundle-files.txt), preserving their source paths and executable modes. Tests, CRD generators, build/packaging scripts, Go source and the [archived lab helpers](../hack/lab/README.md) remain in the checkout and are excluded from the runtime bundle. Build and package from the source checkout; install, enroll and recover using the bundled entry points. It verifies the image's helper hash and Kubernetes version before packaging. Management initialization checks image IDs against the bundle after loading. Existing three-image management bundles remain accepted.

Management initialization also persists minimum inotify limits of 2048 instances and 1048576 watches, while preserving larger administrator settings. Kind, OVN and additional systemd containers consume the same host quota; exhaustion can otherwise stop a container before SSH is available.

On the target host, run the bundled installer. It uses the provider files covered by the bundle's manifest.

```bash
sudo bash /path/to/new-management-bundle/provider/scripts/init-nvke-management.sh \
  /path/to/new-management-bundle /var/lib/vmetal-site \
  192.168.50.10 192.168.50.0/24 vmetal-site
```

For a Docker tenant, set `CP_IMAGE=vmetal-control-plane:1.36.3` when running `install-nvke-docker-gpu.sh`. The installer verifies its labels and helper hash, imports its bundled Kubernetes and OVN image archives, and starts the tenant. The `CP_BASE_IMAGE` build path remains available for development.

The kernel config included in the image must match the target Docker host. Build a new image for a different kernel. Node dependency packages and images must match the pinned NVKE revision and host architecture.

## Load a management site in a terminal

The client export includes the management kubeconfig and grants its privileges to the directory's owner. Export it only for an intended management operator.

```bash
sudo bash scripts/configure-vmetal-client.sh /var/lib/vmetal-site \
  /home/ubuntu/.config/vmetal/sites/lab /path/to/workload.kubeconfig

source /home/ubuntu/.config/vmetal/sites/lab/env.sh
nvkectl --site "$NVKE_SITE" get clusters
kubectl --kubeconfig /home/ubuntu/.config/vmetal/sites/lab/management.kubeconfig get vmetalhosts
kubectl --kubeconfig /home/ubuntu/.config/vmetal/sites/lab/workload.kubeconfig get nodes
```

The script copies the matching `nvkectl` and `kubectl`, creates a private CLI configuration, and exports only environment variables. Commands use the full `nvkectl` and `kubectl` names, with explicit kubeconfig paths for management and workload operations. Source that file in each terminal, or source it from the intended user's shell startup file. Existing sites created before bundle-path recording can supply `VMETAL_MANAGEMENT_BUNDLE=/path/to/bundle` to the export command.

If a terminal already loaded shortcut functions from an earlier generated environment, open a fresh terminal and source the regenerated file.

## Inspect networking and recovery

Run the doctor from the host whose connectivity you are diagnosing. Choose the real peer and endpoint CA.

```bash
python3 scripts/vmetal-network.py \
  --peer 100.100.0.20 --spire 192.168.50.10 \
  --api-url https://172.30.91.10:6443 --ca /path/to/public-ca.crt --agent

sudo python3 scripts/vmetal-network.py --peer 172.30.91.10 \
  --spire 192.168.50.10 --api-url https://172.30.91.10:6443 \
  --ca /path/to/public-ca.crt \
  --cri unix:///run/vmetal-nvke-containerd/containerd.sock
```

The JSON report distinguishes route/MTU, TCP SPIRE, CA and hostname-verified API readiness, CRI, and the presented agent certificate's expiry. An expiry check reads the public certificate. UDP reachability is reported as requiring a workload check. Run `check-nvke-lab-workers.sh` on two Ready nodes for bidirectional Pod traffic over OVN.

The Docker tenant installer invokes `configure-vmetal-manager-network.sh`. Its systemd unit restores scoped API, agent, kubelet, UDP 6081 and return-traffic rules. HTTPS egress follows the actual default-route interface. The generated configuration is immutable for that installation. Use a new tenant/layout for changed endpoints.

For Tailscale, add `NETWORK_MODE=tailscale` to the trusted layout. The manager advertises the API and SPIRE `/32` routes while preserving other advertisements and disables subnet SNAT to retain the worker's source address. Approve the routes and relevant grants in the tailnet. On the remote worker, enable route acceptance. Route approval remains an administrator action.

`OVN_MTU` can be set explicitly in the layout. By default, the installer derives it from the manager-to-worker route's interface MTU with 100 bytes reserved for the IPv4 overlay. A Tailscale MTU of 1280 gives a Pod MTU of 1180. The OVN updater lowers an existing larger MTU and restarts the affected control-plane components. The worker recovery script accepts private IPv4 and the Tailscale shared address range.

After installation, validate both directions using the workload kubeconfig and exact node names.

```bash
bash scripts/check-nvke-lab-workers.sh /path/to/workload.kubeconfig \
  https://172.30.91.10:6443 control-plane-node worker-node
```

## Read failures in Kubernetes

```bash
kubectl get vmetalhosts
kubectl get vmetalhost HOST -o jsonpath='{.status.discoveryMessage}'
kubectl get vmetalhost HOST -o jsonpath='{.status.conditions}'
kubectl -n nvke-system get vmetalmachine.infrastructure.cluster.x-k8s.io MACHINE \
  -o jsonpath='{.status.reason}{"\n"}{.status.message}{"\n"}'
```

Host status distinguishes SSH authentication, host key, timeout, routing, remote prerequisites and invalid inventory. The public explanation excludes remote stderr. Conditions record the observed generation and transition time. Bootstrap and reset failures retain allocation ownership and expose the failed operation. Detailed node logs remain on the node because bootstrap output can contain credentials.

## Migrate released legacy hosts

Migration uses the same trusted SSH identities as the Provider. Keep the legacy controller running until its Clusters, Machines and host claims have been released through their original management workflow. Install the VMetal CRDs and controller on the target management site.

Generate a private migration plan, inspect it, then apply it.

```bash
python3 scripts/migrate-vmetal-hosts.py \
  --kubeconfig /path/to/legacy-management.kubeconfig \
  --target-kubeconfig /path/to/vmetal-management.kubeconfig \
  --plan /private/path/host-migration.json

python3 scripts/migrate-vmetal-hosts.py \
  --kubeconfig /path/to/legacy-management.kubeconfig \
  --target-kubeconfig /path/to/vmetal-management.kubeconfig \
  --plan /private/path/host-migration.json --apply
```

Omit `--target-kubeconfig` when both inventories share a management API. The planner verifies the Linux machine ID over authenticated SSH, checks its old hash, computes the VMetal hash and records both management-site UIDs. The apply step rechecks the physical identity, uses UID and resourceVersion delete preconditions, waits for the legacy finalizer, and creates one VMetalHost with migration provenance. A durable journal supports retry after a failed target create. A new claim or a changed mapping stops migration.

The migration operates on released host inventory. Workloads, active provider IDs, bootstrap Secrets and node-side allocation state are released through the original controllers. Re-enroll a managed-address host with a fresh invitation to establish its new enrollment record. The `vmetal:` identity namespace is protocol v1 and remains fixed across future display-name changes.

## Verification

```bash
KUBEBUILDER_ASSETS=/path/to/envtest go test -race ./...
python3 scripts/test_vmetal_kubeadm.py
python3 scripts/test_management_tools.py
# Run on Linux (the bundle build target).
python3 scripts/test_management_bundle.py
```

The Linux bundle tests exercise both three-image and four-image packaging with fixture Docker metadata and a tiny upstream Git archive. They verify runtime dependency closure, file contents/modes, checksums, exclusion of local files and atomic failure when a recovery dependency is missing; they do not start a cluster.

The migration integration test needs `kubectl` and Python 3 on PATH in addition to the envtest assets. It runs planning, migration and replay against a real isolated API server. Python regression checks cover interrupted migration, a newly claimed source, a changed mapping and Tailscale MTU derivation.

See [the hardening validation record](validation-hardening.md) for the executed installation, network recovery and existing-environment checks, including their limits.
