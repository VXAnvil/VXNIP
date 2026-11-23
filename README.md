# VXNIP

**Virtualized eXecution Node Infrastructure Provider**

[![CI](https://github.com/VXAnvil/cluster-api-provider-vxnip/actions/workflows/ci.yml/badge.svg?branch=main&event=push)](https://github.com/VXAnvil/cluster-api-provider-vxnip/actions/workflows/ci.yml?query=branch%3Amain)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
[![Release: v0.0.3-rc.1](https://img.shields.io/badge/release-v0.0.3--rc.1-orange)](https://github.com/VXAnvil/cluster-api-provider-vxnip/releases/tag/v0.0.3-rc.1)

A Cluster API infrastructure provider for existing Linux machines. VXNIP discovers hosts over SSH, allocates them to Kubernetes clusters, and supports kubeadm bootstrap and NVIDIA NVKE's generic `infra` contract.

**Status:** experimental NVKE integration, tested against revision `2f31480`. Current focus is stability, compatibility and reproducible setup.

## Capabilities

- Discover CPU, memory, network and NVIDIA GPU information over SSH.
- Verify host identity and manage exclusive host claims.
- Bootstrap and release machines through kubeadm or the NVKE adapter.
- Provide preparation, network recovery and diagnostic tooling for Ubuntu AMD64 and ARM64 workers, including bare-metal and virtual hosts.

The core Provider manages host lifecycle. GPU runtime configuration and model deployment are separate concerns; the NVKE guide includes an NVIDIA GPU worker example.

## Try host discovery

Build on macOS or Linux with Go 1.24+ and OpenSSH. The target needs Ubuntu, Python 3, a unique `/etc/machine-id`, and key-based SSH with a verified host key.

```sh
go build -o vxnip .
./vxnip register desktop ubuntu@desktop.local
./vxnip list
./vxnip inspect desktop
```

These commands record local inventory and read host information; they do not join a machine to Kubernetes. For cluster provisioning, follow the guides below. Local CLI reservations and Kubernetes host claims are separate; keep each host under one allocation system.

## Documentation

| Guide | Contents |
| --- | --- |
| [Cluster setup skill](docs/cluster-skill.md) | [Download preview](https://github.com/VXAnvil/cluster-api-provider-vxnip/releases/tag/skill-v0.3.0-rc.2): Codex/Claude Code setup from prebuilt runtimes, mixed architectures, LAN/optional Tailscale and read-only cluster inspection |
| [Prepare public inputs](docs/public-inputs.md) | Fresh Ubuntu: fixed source, verified tools, node artifacts and management bundle |
| [NVKE setup](docs/nvke-quickstart.md) | Management site, worker enrollment, Docker control plane and GPU worker example |
| [Operations](docs/operations.md) | Images and bundles, terminal configuration, diagnostics and recovery |
| [Development and examples](docs/development.md) | CLI details, host allocation, kubeadm and NVKE adapter examples, test commands |

## Compatibility

Use a fresh site and unallocated hosts for this API release. See [installation and allocation identity](docs/compatibility.md#fresh-install-requirement).

The inventory API is `vxnip.infrastructure.cluster.x-k8s.io/v1alpha1`, with `VXNIPHost`, `VXNIPCluster`, `VXNIPMachine` and `VXNIPMachineTemplate`. The NVKE adapter exposes the three infrastructure kinds in `infrastructure.cluster.x-k8s.io/v1alpha1` and shares the host inventory.

Provider IDs use `vxnip://<machine-uid>`. A reboot preserves the current allocation's ID; releasing and allocating again creates a new Machine and ID.

For new installations, start with a fresh management site and hosts that are not allocated to another cluster.

## License

[Apache License 2.0](LICENSE).
