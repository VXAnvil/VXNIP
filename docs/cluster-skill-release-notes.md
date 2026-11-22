# VXAnvil cluster skill release notes

This release uses native VXAnvil APIs and node state. Start a fresh site with unallocated Ubuntu 24.04 hosts. The skill downloads pinned AMD64/ARM64 native runtimes and verifies their size, checksum and source metadata before installation.

The Provider core, native runtime and skill are versioned separately. The bundled catalog identifies the exact core commit and NVKE source used by the downloaded runtime. CI requires tests, generated CRDs and both architecture builds before publishing.

The default LAN path requires a directly connected subnet. Tailscale is optional for remote nodes and may require external route approval. Existing-mode inspection remains read-only. Interrupted mutation state is not permission to replay installers.

The skill stops after cluster/node and network acceptance. GPU configuration, model downloads, training and high availability are outside this release.
