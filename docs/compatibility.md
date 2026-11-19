# Existing installations

For a new site, follow the [NVKE setup guide](nvke-quickstart.md). These steps apply only to an existing inventory. Preserve each host's Linux `/etc/machine-id` and release allocations through their original controllers.

## Released-host migration

`scripts/migrate-vmetal-hosts.py` imports released inventory from `homelab.infrastructure.cluster.x-k8s.io` into the VMetal inventory. It does not convert running clusters or active allocations.

1. Release the old Clusters, Machines and host claims through their original controllers and helpers. Keep the original controller running and its helper installed until release completes.
2. Install the VMetal CRDs and controller in the target management site. Use the trusted SSH configuration that can reach the source hosts.
3. Generate and review a private plan, then apply it:

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

Omit `--target-kubeconfig` if both inventories use the same management API. The tool verifies the physical identity and ownership before changing inventory. Reuse the same plan with `--apply` to resume an interrupted migration. A new claim or changed mapping stops the operation.

Re-enroll a migrated host with a fresh invitation if it uses managed-address enrollment. Active provider IDs, bootstrap Secrets and node-side state remain the responsibility of the original allocation until it is released. The `vmetal:` host identity namespace is fixed protocol data.

## Client export for an older site

If a site's state directory does not record its bundle path, pass `VMETAL_MANAGEMENT_BUNDLE=/path/to/bundle` when running `configure-vmetal-client.sh`. This selects the existing bundle without changing its allocation state. Use a fresh terminal after regenerating the client environment.
