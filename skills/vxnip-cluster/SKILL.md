---
name: vxnip-cluster
description: Prepare or inspect NVKE Kubernetes clusters using VXNIP on existing machines, including LANs with multiple AMD64/ARM64 machines, optional Tailscale and isolated bare-metal runtimes. Discover SSH targets, obtain native artifacts and verify results while preserving selected existing clusters. GPU workloads and allocation migration are separate tasks.
---

# VXNIP cluster setup

Turn explicitly supplied machine connections into a verified VXNIP/NVKE cluster. Use the bundled executor instead of asking users to assemble source checkouts, packages, images and commands manually. Communicate in the user's language.

## Inputs and supported path

Obtain one management SSH alias and one or more worker SSH aliases. Default to the home LAN when the machines share a subnet. Do not require installing or authenticating Tailscale for LAN setup; only choose Tailscale when the user selects it for remote machines. Existing trusted SSH configuration supplies usernames, ports, keys and jump hosts; never request private keys or tokens in chat. If the user asks you to find existing connections, inspect SSH configuration/file paths and relevant project connection records without reading private key contents. Existing authenticated ControlMaster sockets can be supplied as `control_path`; do not silently create new trust or close those sessions. Ask only for missing connections or choices that materially change the installation.

Two explicit modes are available:

- `mode: existing`: read-only examination of the selected management/workload cluster and physical hosts. It permits mixed architectures, Tailscale and already allocated bare-metal workers without sudo. It never uploads an executor, builds assets, changes routes, creates Pods or installs components. Use this mode when the user's machines already belong to the cluster they want to preserve.
- `mode: create`: a dedicated Ubuntu 24.04 AMD64/ARM64 manager with unallocated Ubuntu 24.04 VM or bare-metal workers. AMD64 and ARM64 can be mixed. Download pinned prebuilt runtime assets for each architecture. The default `network_mode: lan` supports multiple workers on the same subnet without Tailscale, a tailnet account or route approval. Explicit `network_mode: tailscale` supports remote workers on 100.64.0.0/10 with an existing authenticated Tailscale interface. Routed VLANs are not yet validated by the LAN executor; do not silently switch networks or install Tailscale.

New managers need 6 CPUs, 16 GiB total/12 GiB available RAM, 100 GiB free disk and a matching kernel config. Workers need 2 CPUs, 4 GiB RAM and 20 GiB free disk. Creation requires trusted SSH, Python 3 and non-interactive sudo; the caller needs Python 3.10+ and OpenSSH on macOS/Linux.

Bare-metal creation uses an isolated containerd socket/root and OVN local gateway, preserving existing Docker, shared containerd 2.x, driver, default route and containers. Unknown allocation/CNI state is a blocker, never evidence that a host is free. Active Kubernetes allocations cannot be adopted, reset or migrated by choosing `create`. Existing cluster inspection does not add workers or perform upgrades. GPU configuration and model deployment remain separate tasks.

If the host lacks terminal/SSH tools or cannot reach the targets, finish the connection configuration and explain how to run the executor from a machine that can. Installing a skill does not itself grant network access or execute code.

## Workflow

Read [references/executor.md](references/executor.md) for commands, source pins, side effects, state and recovery. Runtime scripts are relative to this skill directory, not to the user's repository.

1. First decide whether to inspect an existing cluster or create a new one. When preserving an existing installation, select `existing`, discover its real kubeconfig/API/SPIRE endpoints from trusted local records or current configuration, and retain all existing identities. Create a private connection JSON outside the repository. Usually SSH aliases alone are sufficient. Derive the manager address, worker addresses, architecture and unused networks through discovery. `address` overrides are only for explicit multihomed selection; `worker_user` selects a /home account when SSH logs in as root.
2. Run `scripts/cluster.py plan` to discover the explicitly named machines. It performs remote read-only checks and writes a local private plan. Review the generated targets, machine identities, pinned revisions, networks and changes. Resolve discovery blockers before installation.
3. Confirm that the plan has the expected mode: existing plans have `changes: []` and never install anything. Within the user's authorized setup request, execute `scripts/cluster.py apply` using that exact plan digest. A request to create/download this skill alone does not authorize installation. Do not add a redundant approval ceremony when the user already authorized these concrete changes; follow actual tool permission requirements.
4. Follow progress through the current executor process and private state. The default release path downloads and verifies runtime assets; it must not compile sources or build images on user machines. Missing assets or interrupted downloads must stop with an actionable message, never silently switch to source building. Source building is an explicit developer option (`artifact_mode: source`). Never start a duplicate installation because output is quiet. Summarize progress without exposing raw logs, credentials or generated kubeconfigs.
5. In create mode, treat the cluster as ready only after fresh final verification and bare-metal preservation checks succeed. Report the actual machines and Node/provider identities, Pod bidirectional HTTP, Service DNS/HTTP and the kubeconfig location on the manager. A completed build, Host registration or journal checkpoint is not cluster readiness. Existing mode reports `checks_passed` and `ready: null`: it verifies current identities, API TLS, routes/MTU, endpoints and unchanged workload snapshots, but deliberately leaves fresh Pod/Service probes and SPIRE authentication unverified. Do not present this as a fresh-install end-to-end test.

Once fresh Node readiness, Pod connectivity and Service DNS/HTTP checks pass, report the cluster connection details and stop. Users can subsequently obtain their own model bundle, usually an acir-produced Helm chart; model discovery, packaging and deployment are outside this skill.

## Failure handling

The executor binds state to the exact plan, source pins, runtime files and physical machine identities. Reuse the same private state directory. Completed install steps may be skipped on a subsequent apply, but final verification runs again; old results cannot announce current health.

In Tailscale creation, preserve existing route advertisements and change only the planned route/SNAT/route-acceptance preferences. Tailnet route/grant approval remains external: report a resumable `blocked` route gate, never bypass approval or fall back to static LAN routes.

Interrupted or failed mutations become `unknown`, not safe-to-repeat failures. Use the read-only reconciliation helper for lost replies only when a matching remote success receipt exists and the remote phase lock is free. It never reruns an installer. Without that evidence, preserve state and inspect the phase; do not delete journals or create a new plan to bypass the guard. Staging interruptions and partially completed installers currently require operator investigation.

Source/checksum verification confirms the fixed upstream inputs and downloaded bytes. Source-built packages and image bytes may vary with upstream package repositories; preserve generated manifests rather than promising bit-for-bit reproducibility. Do not claim an unexecuted real-machine installation has been validated.
