# Provider ownership and lifecycle boundaries

**Status:** proposed
**Date:** 2026-11-19

## Context

VMetal allocates existing Linux machines to Cluster API workloads. The repository provides standalone kubeadm bootstrap and an adapter for NVKE's generic infrastructure contract. An installation can combine a Docker control plane with physical or virtual workers. Contributors need explicit ownership boundaries when changing bootstrap, recovery or GPU support.

The current API has a shared `VMetalHost` inventory and separate standalone and NVKE infrastructure API groups. Host claims and node-side helper state protect an active allocation. The setup guide also includes GPU runtime preparation, which can otherwise be mistaken for controller-owned GPU management.

## Decision

We will keep VMetal responsible for host discovery, stable physical identity, exclusive claims, controlled bootstrap handoff, release and the recovery state belonging to its allocation. The selected bootstrap mode determines the handoff: kubeadm configuration for standalone CAPI, or agent/SPIRE configuration for NVKE. NVKE drives its own kubeadm and CNI operations.

We will retain machine IDs, API identities and allocation ownership as protocol data. A Node's `vmetal://<machine-uid>` provider ID remains stable when that allocation restarts; a released host receives a new ID when a new Machine allocates it. Interrupted cleanup retains the claim until reset succeeds.

We will keep GPU driver/runtime installation, GPU resource advertisement and model deployment outside the core reconciliation contract. Preparation scripts can configure required host prerequisites. GPU Operator or a device plugin manages Kubernetes GPU advertisement when selected by the operator; recipe tooling and Helm manage applications. A GPU preparation example does not make the Provider responsible for the model lifecycle.

## Consequences

The same allocation protocol can support CPU and GPU hosts and both architectures without coupling reconciliation to a particular model or GPU management stack. Controller tests can exercise ownership and recovery without GPU hardware.

Installation tooling still needs platform-specific prerequisites and network checks. A change to identity, claims or helper state must preserve active allocations or provide an explicit migration path. Provider CI does not establish driver, model or physical reboot compatibility; those need scoped live validation.

## Options considered

- **Provider lifecycle with delegated runtime/application management:** matches the existing API boundaries and permits different GPU and inference stacks.
- **Provider manages GPU operators and models directly:** centralizes setup but couples machine allocation to application releases and GPU-specific reconciliation.
- **Inventory-only provider:** keeps reconciliation small but leaves bootstrap/release coordination and restart recovery to unrelated manual procedures.
