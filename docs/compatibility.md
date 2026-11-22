# Installation and allocation identity

## Fresh-install requirement

This release uses the native VXAnvil APIs, provider IDs and node-side paths. Install a fresh management site and use unallocated hosts. It does not adopt an existing allocation created by a different infrastructure API. Release such an allocation with its original controller before preparing the host again.

The inventory API is `vxanvil.infrastructure.cluster.x-k8s.io/v1alpha1`. Its kinds are `VXAnvilHost`, `VXAnvilCluster`, `VXAnvilMachine` and `VXAnvilMachineTemplate`. The NVKE adapter exposes the three infrastructure kinds in `infrastructure.cluster.x-k8s.io/v1alpha1` and shares the same host inventory.

A Node's provider ID is `vxanvil://<Machine UID>`. It survives a reboot of that allocation; release and reallocation creates a new Machine UID and provider ID. Preserve the host's unique Linux machine ID. Host discovery, enrollment and the helper use the same `vxanvil:` hash namespace.

## Host preparation

Unknown allocation, CNI or runtime state blocks installation. An absent VXAnvil directory alone does not prove a host is unallocated. Inspect existing kubelet/agent services and runtime state before preparing it; do not bypass preflight by deleting an inventory record or finalizer.

The bare-metal preparation path uses its own containerd root and socket. Shared Docker/containerd services, NVIDIA drivers, existing containers and model files remain outside Provider reset. Each machine template selects one explicit host; replica scaling does not select an arbitrary available host.

## Existing local CLI state

The CLI stores inventory in `vxanvil/inventory.json` under the operating system configuration directory. Use `-state` to select a specific inventory explicitly. Local reservations and Kubernetes Host claims remain separate allocation systems; do not allocate the same physical host through both.

## Released legacy hosts

The optional `scripts/migrate-vxanvil-hosts.py` handles released inventory from the historical Homelab API. It does not convert running clusters or allocated Machines. Its private plan records identity and ownership and can resume only after those checks still pass. Keep source controllers/helpers available until release is complete.
