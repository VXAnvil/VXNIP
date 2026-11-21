## VMetal cluster setup skill — preview

Download **vmetal-cluster.zip** and **vmetal-cluster.zip.sha256**. Verify the checksum, then extract the skill folder into your Codex or Claude Code skills directory. See the [installation guide](https://github.com/vmetal-lab/cluster-api-provider-vmetal/blob/skill-v0.1.0-rc.1/docs/cluster-skill.md) for commands and examples.

### Included

- Portable Agent Skills instructions plus a Python/OpenSSH installer; no repository checkout is needed on the caller.
- Per-architecture public-source acquisition/builds for AMD64/ARM64, LAN or Tailscale networking, and isolated bare-metal runtime support.
- Read-only `existing` mode for allocated clusters, explicit `create` mode for dedicated managers and unallocated workers, strict identity/state binding, and resumable Tailscale route gates.
- Distinct management/workload names, HTTP-ready Pod probes, Service DNS/HTTP verification, private failure logs and credential-free packaging.

### Verified

48 behavioral tests passed. Live ARM64 management + AMD64 worker VM installation finished with both Nodes Ready, bidirectional Pod HTTP and Service DNS/HTTP passing, and test namespaces cleaned. The existing cluster's protected snapshot remained unchanged (4 Hosts, 4 Machines, 2 Nodes, 60 controllers, 76 Pods; 35/35 Deployments available).

The live run included documented, audited repairs for the naming collision and probe startup race. Physical bare-metal fresh installation is not yet live-tested. This preview is an evaluation setup, with GPU configuration, allocation migration and HA/production qualification outside scope. See the [validation record](https://github.com/vmetal-lab/cluster-api-provider-vmetal/blob/skill-v0.1.0-rc.1/docs/cluster-skill-validation.md).

### Requirements

Caller: macOS/Linux, Python 3.10+, OpenSSH and trusted machine connections. New targets: Ubuntu 24.04 with non-interactive sudo. Manager: 6 CPUs, 16 GiB RAM and 100 GiB free disk; workers: 2 CPUs, 4 GiB RAM and 20 GiB disk (foreign-architecture builder: 6 GiB available RAM and 80 GiB free disk). Tailscale targets must be authenticated; routes/grants may need administrator approval. Builds may take hours and download several GB.

This release distributes the skill and executor. Node artifacts are obtained/built during installation from fixed public revisions; a prebuilt/offline NVKE bundle and SSH credentials are not included. The existing controller release remains independently versioned.
