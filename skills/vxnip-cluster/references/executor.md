# Executor usage and contract

The caller needs Python 3.10+ and OpenSSH on macOS/Linux. Runtime paths below are relative to the installed skill. No VXNIP checkout or node artifacts are required on the caller. Keep real connections, plans, logs and snapshots in a private directory outside Git.

## Select the mode before execution

- `mode: existing` only reads the named machines and selected cluster. It works without sudo, permits mixed AMD64/ARM64 and allocated VM/bare-metal hosts, and never calls the installer or changes Tailscale preferences. Provide the real kubeconfig/API/SPIRE endpoints; do not derive SPIRE from an old example or equate it with the SSH address.
- `mode: create` prepares a new dedicated management site and unallocated workers. It requires non-interactive sudo and complete privileged preflight facts. It cannot adopt an existing allocation or add workers to an existing site. Keep existing clusters in `existing` mode until an explicit add-node workflow is implemented.

## Default: several machines on the home LAN

For a new cluster, machine connections are enough. Select one dedicated manager and any number of unallocated workers within their resource limits; architectures and VM/bare-metal type are discovered:

```json
{
  "manager": {"ssh": "home-manager"},
  "workers": [
    {"ssh": "home-amd64-1"},
    {"ssh": "home-arm64-1"},
    {"ssh": "home-amd64-2"}
  ]
}
```

Omitted `mode`, `network_mode` and `artifact_mode` mean `create`, `lan` and `release`. LAN setup does not require the Tailscale binary, account, authentication, subnet advertisements or tailnet route approval. Existing Tailscale preferences, if present, are left alone. This is online installation: the runtime downloads and required package repositories still need outbound access.

LAN workers must share the manager's directly connected IPv4 subnet. The executor plans a route to the workload API through the manager and configures scoped forwarding/firewall rules for each worker. SSH reachability alone does not prove that the cluster data plane works: final verification checks every worker, bidirectional Pod HTTP and Service DNS/HTTP. Routed VLANs are not currently supported by this LAN preflight; report the limitation instead of enabling Tailscale automatically.

NVKE may reapply netplan while joining a node, which can replace its interface or discard a manually added route. LAN workers install a site-specific API route service that follows the interface holding the planned node IP and restores only the planned workload API `/32` via the manager. It leaves default routes and other destinations alone; no Tailscale service or route approval is required.

The endpoint is a provisioned NVKE management/workload cluster with fresh readiness and network checks and a kubeconfig location. Model bundles and acir-produced Helm charts belong to the user's subsequent deployment workflow.

## Optional: Tailscale

Only select `network_mode: tailscale` when requested for remote machines. Example for preserving an existing mixed-architecture cluster over Tailscale:

```json
{
  "mode": "existing",
  "network_mode": "tailscale",
  "site": "vxnip-lab",
  "manager": {"ssh": "lab-manager", "address": "192.168.50.10"},
  "workers": [{"ssh": "lab-spark", "address": "100.101.10.20", "kind": "baremetal"}],
  "existing": {
    "management_kubeconfig": "/home/operator/sites/lab/management.kubeconfig",
    "workload_kubeconfig": "/home/operator/sites/lab/workload.kubeconfig",
    "api": "https://172.30.91.20:6443",
    "cluster": "vxnip-lab",
    "spire_address": "192.168.50.10"
  }
}
```

All paths in `existing` are on the manager, not the caller. Existing kubeconfigs must validate TLS. Only the public workload CA is extracted for a direct worker-side API check; no kubeconfig, token, private key or container environment is exported.

For a new mixed cluster, omit `existing`, set `mode: create` and specify the same manager/worker connections. Bare-metal vs VM is discovered and checked against optional `kind`. The manager address must be locally assigned RFC1918 IPv4; Tailscale workers use their locally assigned 100.64.0.0/10 address on `tailscale0`. For Tailscale management, the default-route source address is selected when unambiguous, or use `manager.address` to choose the address advertised for SPIRE. `network_mode: lan` instead requires a directly connected common subnet.

New plans keep the management site name and explicitly show a distinct `workload_cluster`: site `vxnip-lab` creates workload cluster `vxnip-w-lab`. Their Docker control-plane names must differ. Registration and final verification use the workload name; NVKE CLI connections still select the management site.

SSH aliases supply usernames, keys, ports and ProxyJump. `control_path` can reference an already authenticated, current-user-owned ControlMaster socket. Regular files, symlinks and other users' sockets are rejected. The executor does not read private key contents or close the user's master sessions. For root SSH on a create-mode worker, set `worker_user` to an existing /home account. Host keys must already be trusted; BatchMode/StrictHostKeyChecking are enforced and agent forwarding is disabled.

```bash
python3 scripts/cluster.py plan --connections /private/path/connections.json \
  --output /private/path/plan.json
python3 scripts/cluster.py apply --plan /private/path/plan.json \
  --digest DIGEST_FROM_PLAN_OUTPUT --state-directory /private/path/run
python3 scripts/cluster.py status --state-directory /private/path/run
```

Existing-mode plans have `changes: []`. Apply rechecks physical identities and performs only read-only API/host/network queries. It compares Host claims, Machine identities, Node boot/provider IDs, controller specification digests, Pod UIDs/container IDs/restarts, host service PIDs, Tailscale preferences and available Docker observations before/after. Unknown Docker or privileged facts remain explicitly uncovered rather than being treated as empty/healthy.

Existing results use `checks_passed` and `ready: null`. They cover current-generation Host/claim/owner/nodeRef identity, API TLS/readyz, worker API/SPIRE TCP paths, manager agent/kubelet TCP paths, Tailscale interface selection, route acceptance/NoSNAT and OVN MTU budget. They deliberately do not create Pod/Service tests or claim SPIRE authentication, full data-plane correctness, a new installation or long-term health. Snapshot changes are reported, not automatically attributed to this executor.

## New-install resources and preservation

The manager must be a dedicated Ubuntu 24.04 AMD64/ARM64 host without existing containers/allocations: 6 CPUs, 16 GiB total and 12 GiB available RAM, 100 GiB free disk, matching kernel config and outbound upstream access. Workers require Ubuntu 24.04, 2 CPUs, 4 GiB RAM, 20 GiB free disk, completed cloud-init and no swap/allocation/CNI. Containerized workers are rejected. Detach NoCloud/config-drive seed media before planning a privileged workload control plane.

A VM uses the dedicated VM preparation script. A bare-metal worker requires a healthy existing Docker/shared containerd 2.x installation; preparation extracts the pinned node tools into an isolated runtime/socket/root, uses OVN local gateway without taking over the physical NIC, appends a restricted Provider public key, and preserves Docker, shared containerd, existing containers, the default route and the NVIDIA driver. The pinned bare-metal helper keeps its `vxnip.nvke.dev/experiment=true:NoSchedule` taint; ordinary workloads need the corresponding toleration, and this executor does not silently remove that scheduling restriction. Shared resources are snapshotted before building/preparation and checked both before allocation and after final verification. Changes stop the run; there is no automatic rollback or reinstall.

Default release mode downloads SHA-256-pinned manager and node runtimes for AMD64/ARM64, selects each worker's native architecture and shares the verified node assets only with workers of that architecture. No compiler, source checkout or image build runs on user machines. Download failure is a resumable gate and does not trigger a source-build fallback.

Developer-only `artifact_mode: source` compiles on the targets. In that mode, manager-native workers receive the manager's verified artifacts. Each other architecture builds natively on its first suitable worker (6 GiB available RAM and 80 GiB free disk required), and shares that verified artifact tree only with the same architecture. No AMD64 package is reused for ARM64 and no QEMU/binfmt changes are made. The native node-only builder rejects apt removals/upgrades, uses a unique build image name and does not restart shared Docker/containerd or build an extra management stack. An incompatible dependency transaction blocks and requires a separately prepared native build environment; it is not forced through.

The new manager installs runtime prerequisites and Docker, loads Open vSwitch, verifies downloaded runtime assets, initializes Kind/CAPI/NVKE/SPIRE/VXNIP, persists inotify/IPv4 forwarding and starts a privileged Docker workload control plane. The topology is an evaluation path, not HA/production-qualified. GPU drivers, GPU Operator and models are outside scope.

## Tailscale create mode

These requirements apply only to explicit Tailscale mode. Tailscale must already be installed/authenticated on every target. The manager merges the API/SPIRE /32 routes with existing advertisements and disables subnet SNAT to preserve worker source identity. Each worker enables route acceptance without replacing other preferences. Scoped per-worker manager firewall recovery units are persisted; OVN MTU uses the minimum peer-route interface budget with 100 bytes reserved for the IPv4 overlay.

Routes and grants may require approval by the tailnet administrator. The worker route gate verifies the API actually routes through `tailscale0` and API/SPIRE are reachable before preparation. Missing approval/connectivity returns a known resumable `blocked` gate (exit 75 from that fixed phase), not a generic retry permission. Resume the same plan/state after approval. No approval API, static-LAN fallback, global firewall flush, reset or overwrite of existing routes is used. Existing mode never changes these preferences.

## Public inputs

- VXNIP: `https://github.com/VXAnvil/VXNIP.git`, revision `eb563fdd1f66093c036925a653e223019945d72c`.
- NVKE: `https://github.com/dsx-ai-factory/nvke.git`, revision `2f31480b360cbd7fc9d9db7a6ba274a8d0fd5049`.
- Publisher/developer build tools: Go 1.27.4, Kind 0.32.0, kubectl 1.36.3, clusterctl 1.13.6 and Helm 4.2.4 from architecture-specific upstream downloads, verified against HTTPS publisher metadata/checksums before execution. Git checkouts are checked against full commit IDs.
- Native node packages, OVN/SPIRE agents, unit files and images are built once by the native publisher through the fixed public-source procedure; NVKE package builders use committed upstream locks. SHA256SUMS and image IDs are retained and checked before packaging/installation.

The default installer uses the published runtime manifest; a missing manifest blocks before remote installation. Only explicit developer source builds can download several GB and take hours. They require public apt, GitHub, Go, Kubernetes, Helm and registry access. Package repository changes can alter source-built bytes: preserve generated manifests; do not claim byte-for-byte reproducibility. No private bundle service or registry login is required. An offline path is not implemented.

## Portable image loading

Docker versions/stores can rewrite an image config during `docker load`, so publisher `.Id` strings alone cannot verify loaded content portably. Release mode first verifies the immutable downloaded bundle and archive checksums, loads its images, and compares OS, architecture, ordered root filesystem layer digests and non-default runtime configuration against the archive's own configs. Changed layers, environment, labels or platform stop installation. It then derives a hard-linked `work/management-runtime` view containing this daemon's IDs and a new checksum manifest. The original `work/management-bundle` remains unchanged. No Dockerfile, compiler or image build is invoked. The fixed management helper checks the derived IDs before use.

## State and recovery

New-install remote state is root-owned `/var/lib/vxnip-bootstrap/<site>/`, with runtime, sources/builds, site credentials, tenant configuration and handoff data. Existing mode writes no remote state. The caller uses a 0700 directory with private plan/journal/result/log/snapshot files. Logs can contain installer output; never paste them wholesale into chat or commit them.

Plans bind source pins, runtime hashes, machine identities and settings. The first create-mode start requires a plan less than one hour old; preconditions are checked again. Reuse the same plan/state. Completed installation phases are skipped but final verification runs afresh. Changed runtime files or plan versions require a newly reviewed plan, not silent adoption of prior installation state.

Unknown/failed mutations do not replay automatically. Lost replies can be reconciled only using a matching root-owned success receipt and a free remote phase lock:

```bash
python3 scripts/reconcile.py --plan /private/path/plan.json \
  --state-directory /private/path/run --step build-arch-arm64
```

Receipts cover management/build/control-plane/handoff/join, native architecture builds, shared-host baselines, Tailscale worker gates and worker preparation. An active process, missing receipt or incomplete staging requires inspection. Do not delete state to bypass the guard. The Tailscale approval gate is separately classified as safely resumable; arbitrary installer failures are not.

New-install verification checks the exact TLS API, current Host generation/claim, VXNIPMachine and CAPI owner/infraRef/nodeRef, Node Ready/address/providerID, public agent certificate expiry, ordinary Pod HTTP both ways and Service DNS/HTTP, followed by bare-metal preservation checks. Temporary namespaces are trap-cleaned; interruption can leave a namespace requiring exact-name inspection/cleanup. This is not a full SPIFFE audit or a health guarantee.

CAPI v1beta2 may publish a name-only `status.nodeRef`. The verifier checks a supplied Node UID strictly; if omitted it reports `node_ref_uid_verified: false`, retains the observed Node UID for preservation comparisons, and still requires the exact allocation-derived providerID, physical Host identity, expected address and Node Ready. It does not invent a UID or accept a mismatched providerID.
