# Existing installations

For a new site, follow the [NVKE setup guide](nvke-quickstart.md). These steps apply only to an existing inventory. Preserve each host's Linux `/etc/machine-id` and release allocations through their original controllers.

## VXAnvil name compatibility

VXAnvil is the new project name. Existing installations keep their API objects and node identities; this rename does not migrate or recreate clusters.

| New source and distribution names | Preserved compatibility contract |
| --- | --- |
| `github.com/VXAnvil/cluster-api-provider-vxanvil` | Existing release commits and source pins |
| `vxanvil` and `cmd/vxanvil-controller` | CLI inventory at the OS configuration directory's `vmetal/inventory.json`; controller leader Lease `vmetal-controller` |
| Go names such as `VXAnvilHost` and `VXAnvilMachine` | Serialized `VMetalHost`, `VMetalCluster`, `VMetalMachine`, `VMetalMachineTemplate`; their existing API groups and CRDs |
| `scripts/vxanvil-kubeadm` and `scripts/configure-vxanvil-*.sh` | Existing installed helper `/usr/local/sbin/vmetal-kubeadm`, sudoers rules, systemd units, runtime sockets, node state and recovery paths |
| `VXANVIL_*` options | Existing `VMETAL_*` options remain accepted by updated shell entrypoints; the explicitly set new option takes precedence |
| New releases use VXAnvil names | Previously published assets retain their filenames, checksums and source versions |

The provider ID remains `vmetal://<Machine UID>`, the physical machine hash keeps the `vmetal:` namespace, and existing finalizers and `vmetal.nvke.dev` labels/annotations remain unchanged. These strings are protocol identifiers, not a second inventory. New allocations use the same contract; the project does not introduce duplicate APIs under the new spelling. The exported Go aliases have the same underlying wire types. Old `VMetal*` Go names remain available under the new module path.

The node helper's bytes are intentionally unchanged: existing Docker control-plane images pin its checksum, and node-side interrupted-operation records belong to that helper. Source-level compatibility copies of the former script names are included in management bundles; their contents match the corresponding new entrypoints. Existing allocations and node-side state must never be reset just to update the display name.

When upgrading an existing controller, keep the original systemd unit, SSH wrapper, kubeconfig, environment and leader Lease. Build `./cmd/vxanvil-controller`, compare installed CRDs against `config/crd`, back up the executable and unit configuration, then change only the unit's executable path to the verified new binary and restart that one Provider service. `VMETAL_ENABLE_NVKE_INFRA=1` continues to work. Do not run a second Provider with a different Lease, delete/recreate CRDs, alter CAPI references/provider IDs, or rerun a new-site installer against an allocated site. Roll back to the saved executable/unit if acceptance fails. This documents the procedure; it does not assert a physical cluster upgrade was performed for this rename.

Acceptance requires the same Host/Machine/Node UIDs, claims and provider IDs, successful fresh host probes and Ready nodes. Check the affected workloads and network paths. Complete interrupted executor runs with the exact original skill and private state: the new skill must not bypass the old runtime digest guard.

### Historical downloads

Published `skill-v0.1.0-rc.4` still downloads as `vmetal-cluster.zip` and installs `vmetal-cluster/`. The source package now produces `vxanvil-cluster.zip` with `vxanvil-cluster/`; do not claim that a renamed release has already been published. The pinned native runtime catalog keeps historical `vmetal-manager-*` and `vmetal-node-*` filenames and unchanged digests at the new repository address. Its installer still requires `vmetal-*` site names. The source skill retains that default and restriction until a new runtime release is verified. Existing site/cluster names are opaque identifiers and need not change with the project name.

GitHub repository links use the new address. GHCR is a separate namespace: historical images are not renamed by a repository redirect. The public-input guide builds its fixed controller from source instead of assuming an old registry address is available. New publish/tag workflows derive `ghcr.io/vxanvil/cluster-api-provider-vxanvil` from the repository. Do not retag or overwrite old versions to hide the rename; check package visibility and anonymous pulls for a new image before documenting it as available.

## Released-host migration

`scripts/migrate-vxanvil-hosts.py` imports released inventory from `homelab.infrastructure.cluster.x-k8s.io` into the VXAnvil inventory. It does not convert running clusters or active allocations.

1. Release the old Clusters, Machines and host claims through their original controllers and helpers. Keep the original controller running and its helper installed until release completes.
2. Install the VXAnvil CRDs and controller in the target management site. Use the trusted SSH configuration that can reach the source hosts.
3. Generate and review a private plan, then apply it:

```bash
python3 scripts/migrate-vxanvil-hosts.py \
  --kubeconfig /path/to/legacy-management.kubeconfig \
  --target-kubeconfig /path/to/vxanvil-management.kubeconfig \
  --plan /private/path/host-migration.json

python3 scripts/migrate-vxanvil-hosts.py \
  --kubeconfig /path/to/legacy-management.kubeconfig \
  --target-kubeconfig /path/to/vxanvil-management.kubeconfig \
  --plan /private/path/host-migration.json --apply
```

Omit `--target-kubeconfig` if both inventories use the same management API. The tool verifies the physical identity and ownership before changing inventory. Reuse the same plan with `--apply` to resume an interrupted migration. A new claim or changed mapping stops the operation.

Re-enroll a migrated host with a fresh invitation if it uses managed-address enrollment. Active provider IDs, bootstrap Secrets and node-side state remain the responsibility of the original allocation until it is released. The `vmetal:` host identity namespace is fixed protocol data.

## Client export for an older site

If a site's state directory does not record its bundle path, pass `VXANVIL_MANAGEMENT_BUNDLE=/path/to/bundle` when running `configure-vxanvil-client.sh`. This selects the existing bundle without changing its allocation state. Use a fresh terminal after regenerating the client environment.
