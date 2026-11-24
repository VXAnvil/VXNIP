# VXNIP cluster skill v0.3.0-rc.3

This release turns requested NVIDIA GPU workers into verified schedulable GPU nodes, including ARM64 DGX Spark. Set `gpu: true` on the worker connections. CPU-only installations remain CPU-only; the prebuilt runtime release and source pins are unchanged.

- Prepare an isolated CDI runtime before allocation. Preserve shared Docker/containerd, the driver and global NVIDIA policy, including hosts that reject unprivileged `NVIDIA_VISIBLE_DEVICES` requests.
- Install NVIDIA device plugin v0.20.2 with CDI-CRI allocation, the required read-only driver-root mount and per-site CDI output. No global environment-variable policy change or privileged plugin is needed.
- Verify each physical GPU with scheduler-selected CUDA kernel Pods, compare GPU UUIDs, and remove test namespaces. Network acceptance now includes worker-to-worker paths.
- Name pools `worker-1`, `worker-2`, etc., independently of CPU/GPU capability. The pinned NVKE API has no in-place pool rename; existing clusters are never recycled for naming alone.
- Export site-bound `nvkectl` and workload `kubectl` entry points that work without sourcing an environment file. Include management/workload commands and explain `Provisioned`, Node Ready and GPU allocation separately.
- Download each node runtime directly on its target using the same pinned architecture-specific asset, avoiding large archives relayed through the caller or a Tailscale DERP connection.
- Report preflight blockers across targets. Explicit preparation can disable known unused swap files while retaining files/fstab backups, or initialize a clean Ubuntu cloud-init disable-marker case. Release-mode managers need 40 GiB free disk; source builders retain 100 GiB.
- Explicitly allowlisted manager containers remain running, with identity/configuration preservation checks. No automatic cache-container removal or global prune.

New plans use schema 4. Interrupted schema-3 runs must retain their original skill, plan and state; replacing runtime files does not authorize replay or adoption. GPU/host preparation and client phases use the same exact-plan receipts and uncertain-mutation guards as the existing executor.

Validation: offline behavioral regression tests cover GPU planning/order/resume, strict NVIDIA-policy preservation, CDI mounts, physical UUID acceptance, probe cleanup, neutral names, explicit preparation, manager container allowlists and site-bound shell execution. The GPU/CDI mechanism and scheduled CUDA kernel were exercised on two real GB10 hosts while developing the fix. This release does not claim a fresh end-to-end reinstallation with the newly packaged executor, physical-host reboot, long-term SPIRE renewal, NCCL or model acceptance. The existing live cluster is preserved.

Download `vxnip-cluster.zip` and its `.sha256` file. Extract as `vxnip-cluster/` into the skill directory of your agent, preserving older versions needed by unfinished runs. GPU drivers/Toolkit must already be installed; inference/model bundles remain separate.
