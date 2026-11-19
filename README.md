# VMetal

A Cluster API infrastructure provider for existing Linux machines. VMetal discovers hosts over SSH, allocates them to Kubernetes clusters, and supports kubeadm bootstrap and NVIDIA NVKE's generic `infra` contract.

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
go build -o vmetal .
./vmetal register desktop ubuntu@desktop.local
./vmetal list
./vmetal inspect desktop
```

These commands record local inventory and read host information; they do not join a machine to Kubernetes. For cluster provisioning, follow the guides below. Local CLI reservations and Kubernetes host claims are separate; keep each host under one allocation system.

## Documentation

| Guide | Contents |
| --- | --- |
| [NVKE setup](docs/nvke-quickstart.md) | Management site, worker enrollment, Docker control plane and GPU worker example |
| [Operations](docs/operations.md) | Verified images and bundles, terminal configuration, diagnostics, network recovery and migration |
| [Development and examples](docs/development.md) | CLI details, host allocation, kubeadm and NVKE adapter examples, test commands |
| [Validation record](docs/validation-hardening.md) | Executed checks, recovery results and test limitations |

## Compatibility

The inventory API is `vmetal.infrastructure.cluster.x-k8s.io/v1alpha1`, with `VMetalHost`, `VMetalCluster`, `VMetalMachine` and `VMetalMachineTemplate`. The NVKE adapter exposes the three infrastructure kinds in `infrastructure.cluster.x-k8s.io/v1alpha1` and shares the host inventory.

Provider IDs use `vmetal://<machine-uid>`. A reboot preserves the current allocation's ID; releasing and allocating again creates a new Machine and ID.

For new installations, use a fresh site and unallocated hosts. The Homelab-to-VMetal naming transition changed API identities, host hashes and node-side state paths. Existing Homelab allocations need their original controller and helper until released; see [released-host migration](docs/operations.md#migrate-released-legacy-hosts).

## License

[Apache License 2.0](LICENSE).
