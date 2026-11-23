# VXNIP cluster skill v0.3.0-rc.2

This prerelease updates the portable skill for the canonical repository, [`VXAnvil/cluster-api-provider-vxnip`](https://github.com/VXAnvil/cluster-api-provider-vxnip). Runtime download URLs, source checkout URLs and publisher metadata use that address. Developer source builds validate the native controller/API without rejecting the historical module declaration in the pinned source commit.

It continues to use `runtime-v0.3.0-rc.1`: the runtime source pins, archive bytes and SHA-256 checksums are unchanged. Previous skill releases and their attachments remain available. This release does not upgrade existing allocations or rewrite an installed skill's private plans; finish interrupted runs with their original skill and state before upgrading.

Download `vxnip-cluster.zip` and `vxnip-cluster.zip.sha256`, verify the checksum, and follow the [installation guide](https://github.com/VXAnvil/cluster-api-provider-vxnip/blob/skill-v0.3.0-rc.2/docs/cluster-skill.md).

VXNIP means Virtualized eXecution Node Infrastructure Provider. This release uses native VXNIP APIs and node state. Start a fresh site with unallocated Ubuntu 24.04 hosts. The skill downloads pinned AMD64/ARM64 native runtimes and verifies their size, checksum and source metadata before installation.

The Provider core, native runtime and skill are versioned separately. The bundled catalog identifies the exact core commit and NVKE source used by the downloaded runtime. CI requires tests, generated CRDs and both architecture builds before publishing.

The default LAN path requires a directly connected subnet. Tailscale is optional for remote nodes and may require external route approval. Existing-mode inspection remains read-only. Interrupted mutation state is not permission to replay installers.

The skill stops after cluster/node and network acceptance. GPU configuration, model downloads, training and high availability are outside this release.
