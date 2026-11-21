## VXAnvil cluster skill: prebuilt runtime preview

Download **vxanvil-cluster.zip** and its **.sha256** file, verify the checksum and extract into your agent's skills directory. See the [installation guide](https://github.com/VXAnvil/cluster-api-provider-vxanvil/blob/skill-v0.1.0-rc.4/docs/cluster-skill.md).

- Provide one manager and one or more worker SSH connections. Runtime assets are selected automatically for AMD64/ARM64 and verified against the included manifest. No source compilation or image build runs on user machines; failed downloads do not silently fall back to source builds.
- Portable image content verification handles Docker store/version differences while preserving the original downloaded bundle.
- LAN API route recovery follows the selected node address across NVKE netplan/bridge changes, restoring only the planned API `/32` and retaining the default route.
- Home LAN is the default. Several mixed-architecture workers can share the manager's directly connected subnet without Tailscale installed, device authentication or route approval. Tailscale is optional for remote hosts.
- Existing-mode inspection remains read-only. Creation uses a dedicated manager and unallocated workers; it does not adopt/reset allocated machines. Bare-metal preparation uses an isolated runtime while preserving shared services.
- Success requires current Node/provider identities, Node Ready, bidirectional Pod HTTP and Service DNS/HTTP for every worker. The skill stops there; model bundles and acir-produced Helm charts are separate.

Caller: macOS/Linux, Python 3.10+, OpenSSH and trusted connections. Targets: Ubuntu 24.04 with non-interactive sudo. Manager: 6 CPUs, 16 GiB RAM (12 GiB available), 100 GiB free disk and matching host kernel config. Workers: 2 CPUs, 4 GiB RAM, 20 GiB free disk. LAN currently requires a shared subnet. Optional Tailscale mode requires authenticated devices and any necessary administrator route/grant approvals. Runtime/package downloads require internet access.

64 bootstrap behavioral tests and exact-commit CI passed before native publishing. Fresh public-release LAN VM acceptance is being recorded separately; historical source-path acceptance is not evidence that this prebuilt release has passed. Physical bare-metal fresh installation remains untested. This is an evaluation setup; HA/production qualification, GPU configuration and model deployment are outside scope.
