# Prepare or inspect VXAnvil clusters from SSH connections

The portable [`vxanvil-cluster` skill](../skills/vxanvil-cluster/SKILL.md) includes a standard-library Python SSH executor and a pinned prebuilt-runtime downloader (plus explicit developer source builders). It supports AMD64/ARM64 mixtures, VM and bare-metal workers, and direct LAN or Tailscale networking.

Choose the mode explicitly. `existing` inspects and preserves the named cluster: no remote uploads, sudo, builds, installations, route updates or new probe Pods. `create` prepares a new dedicated management site and unallocated workers. It cannot adopt existing allocations, add nodes to an existing site or upgrade an active cluster. This prevents an existing machine connection from implicitly authorizing reinstallation.

A fresh manager needs Ubuntu 24.04, 6 CPUs, 16 GiB total/12 GiB available RAM and 100 GiB free disk. Workers need Ubuntu 24.04, 2 CPUs, 4 GiB RAM and 20 GiB free disk. Creation requires trusted SSH and non-interactive sudo. Existing mode can inspect mixed allocated hosts without those build resources or sudo.

Bare-metal creation retains the shared Docker/containerd 2.x runtime, driver, existing containers and default route while preparing an isolated NVKE runtime and OVN local gateway. Unknown allocation/CNI state blocks creation. Architecture-specific runtime assets are built on native publisher machines and downloaded with pinned SHA-256 checksums. User machines do not need Go, a compiler or source/image builds. Developer-only `artifact_mode: source` retains the source builders and their additional resource requirements. GPU configuration and model deployment are separate.

LAN is the default: machines on the same directly connected subnet do not need Tailscale installed, an account or route approval. One manager can prepare multiple AMD64/ARM64 workers. Cross-VLAN routing is not yet supported by the LAN preflight.

Only when explicitly selecting `network_mode: tailscale` must Tailscale already be authenticated. Creation merges explicit route advertisements, retains other preferences, enables worker route acceptance, preserves source addresses and derives OVN MTU from peer paths. Missing tailnet route/grant approval becomes a resumable gate before worker preparation. Existing mode only observes routes, preferences, endpoints and MTU; it never attempts a repair.

## Install and use

The current source skill is named `vxanvil-cluster`. Previously published [skill-v0.1.0-rc.4](https://github.com/VXAnvil/cluster-api-provider-vxanvil/releases/tag/skill-v0.1.0-rc.4) retains its historical asset and installed folder name, `vmetal-cluster`. To install that exact published version:

```bash
release=skill-v0.1.0-rc.4
base="https://github.com/VXAnvil/cluster-api-provider-vxanvil/releases/download/$release"
curl --proto '=https' --proto-redir '=https' -fL "$base/vmetal-cluster.zip" -o vmetal-cluster.zip
curl --proto '=https' --proto-redir '=https' -fL "$base/vmetal-cluster.zip.sha256" -o vmetal-cluster.zip.sha256
python3 - <<'PY_VERIFY'
from pathlib import Path
import hashlib
expected = Path('vmetal-cluster.zip.sha256').read_text().split()[0]
assert hashlib.sha256(Path('vmetal-cluster.zip').read_bytes()).hexdigest() == expected, 'Checksum mismatch'
print('ZIP checksum verified')
PY_VERIFY
```

Extract into one agent skill directory. Back up its existing folder before upgrading.

| Version | Codex installation command | Invoke |
| --- | --- | --- |
| Historical rc.4 ZIP above | `python3 -m zipfile -e vmetal-cluster.zip "$HOME/.agents/skills"` | `$vmetal-cluster` |
| Package built from the current source below | `python3 -m zipfile -e dist/vxanvil-cluster.zip "$HOME/.agents/skills"` | `$vxanvil-cluster` |

For Claude Code, extract into `$HOME/.claude/skills` and invoke `/vmetal-cluster` or `/vxanvil-cluster` to match the actual installed folder. Do not install a new folder next to an older skill and assume its interrupted plans can resume with different code. See [name compatibility](compatibility.md#vxanvil-name-compatibility).

The current source skill pins the same published native runtimes and still uses `vmetal-*` site names. A newly named native-runtime release has not been published by this source rename.

Then ask, for example:

> Use vxanvil-cluster to prepare a new NVKE cluster with SSH aliases `lab-manager` and `lab-worker`. Preserve other clusters and workloads.

For a home LAN, ask:

> Prepare NVKE using `home-manager` and workers `home-pc-1`, `home-spark`, `home-pc-2` over the LAN. Do not use Tailscale. Preserve other clusters and workloads. Stop when the cluster is provisioned; I will deploy my own acir Helm chart later.

The corresponding private connection file is:

```json
{
  "manager": {"ssh": "home-manager"},
  "workers": [{"ssh": "home-pc-1"}, {"ssh": "home-spark"}, {"ssh": "home-pc-2"}]
}
```

Worker architecture and addresses are discovered; omitted `network_mode` means `lan`. Final acceptance still checks node identities, readiness, Pod connectivity and Service DNS/HTTP.

The caller needs macOS/Linux, Python 3.10+, OpenSSH and network access to those machines. A skill upload alone does not supply terminal/SSH access. Trusted SSH configuration supplies authentication; do not paste private keys/passwords into chat. For optional Tailscale mode, device authentication and tailnet route/grant approval use the user's account. The default release path downloads prebuilt runtimes and installs runtime prerequisites. If a download fails, resume the same plan after fixing connectivity; no silent source-build fallback runs. Only explicit developer source builds can take hours.

To build the same portable package from a checkout:

```bash
mkdir -p dist
python3 scripts/package-cluster-skill.py dist/vxanvil-cluster.zip
```

The ZIP includes instructions, references and runtime scripts. Site configuration, logs, credentials and private test evidence are excluded. This is a skill/executor package; a manifest pins the separately published AMD64/ARM64 manager and node runtimes downloaded during setup. Packaging a distributable skill fails unless that manifest is included. An offline installer is not yet available.

For connection JSON examples, exact source pins, safety boundaries, creation and read-only result semantics, state and recovery, see the [executor reference](../skills/vxanvil-cluster/references/executor.md).

The release installer stops once the NVKE cluster is provisioned and network verification passes. Model bundles and acir-produced Helm charts are obtained and deployed separately by the user.

## Validation boundary

```bash
python3 scripts/test_cluster_bootstrap.py
```

Offline tests cover mixed-architecture artifact selection, Tailscale addresses and approval gates, VM/bare-metal selection, existing-mode dispatch without installers, protected resource drift, unknown inventory, shared-runtime compatibility, SSH trust, interrupted actions, allocation identity, packaging and probe cleanup. ShellCheck and Python/Bash parsing cover runtime files.

Historical source-path validation completed with an ARM64 management VM and AMD64 worker VM, including native architecture-specific builds, approved Tailscale routes, two Ready Nodes, strict Host/Machine/provider identity, fresh bidirectional Pod HTTP and Service DNS/HTTP. The existing ThinkPad/Spark cluster passed separate read-only preservation checks: 4 Hosts, 4 Machines, 2 Nodes, 60 controllers and 76 Pods retained the same protected snapshot; 35/35 Deployments were available. The run exposed and fixed management/workload name collisions and early HTTP probing under TCG. The completed stages were retained through audited plan updates. A new physical bare-metal installation remains untested; GPU workloads and migration are outside this release.

CAPI v1beta2 may publish a name-only `status.nodeRef`. The verifier checks a supplied Node UID strictly; if omitted it reports `node_ref_uid_verified: false`, retains the observed Node UID for preservation comparisons, and still requires the exact allocation-derived providerID, physical Host identity, expected address and Node Ready. It does not invent a UID or accept a mismatched providerID.

See the [mixed-architecture preservation validation record](cluster-skill-validation.md) for completed checks, audited recovery and remaining coverage limits.
