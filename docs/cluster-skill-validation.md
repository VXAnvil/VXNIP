# Cluster skill validation

Run `python3 scripts/test_cluster_bootstrap.py` and `python3 scripts/test_cluster_gpu.py` for offline coverage of planning, SSH trust, architecture selection, runtime downloads, source/checksum validation, allocation identity, resume behavior and probe cleanup. Linux CI also executes bundle tests and real API integration tests; skipped required API tests fail the gate.

Fresh-install acceptance uses public artifacts and new Ubuntu guests. Record the exact core source, runtime release and skill commit. Require matching Host/Machine/Node identities, Ready nodes, ordinary Pod HTTP in both directions and Service DNS/HTTP, plus external DNS, numeric-IP TCP and certificate-validated HTTPS on every workload node. A successful build or enrollment is not installation acceptance.

Existing-mode inspection is read-only and reports `ready: null`; it cannot establish fresh Pod connectivity, SPIRE renewal or a new installation. Opt-in GPU creation additionally requires fresh scheduled CUDA kernels and physical UUID matching. Models, NCCL and physical-host reboot testing remain separate acceptance steps.

Detailed site logs and credentials remain outside this repository. Current release results are linked from its release notes after the actual checks complete.

Egress regression uses offline behavioral tests plus a disposable Linux CI network-namespace topology with a Pod, forwarding host and local DNS/TLS endpoint. It first reproduces Docker FORWARD DROP with existing SNAT, then verifies the scoped repair, unchanged default policy/NAT/routes, idempotence, real monitor recovery after removed rules/uplink changes and rejection of an untrusted TLS certificate. The required CI step fails instead of skipping when its Linux/root prerequisites are unavailable. No physical Spark is used for this release testing. A fresh end-to-end installation or physical reboot is not claimed.
