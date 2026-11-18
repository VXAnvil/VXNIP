# VMetal installation and diagnostics hardening validation

Validation date: November 18, 2026. Changes were tested separately from the running Spark experiment. This record covers the five improvements described in [operations.md](operations.md).

## Automated checks

- `go test -race ./...` passed with envtest assets and `kubectl` available. This includes controller restart/claim ownership, failed reset ownership, sanitized discovery diagnostics, and legacy migration against a real temporary Kubernetes API server.
- Migration integration ran plan, apply and replay. Its SSH process was a test double supplying a fixed machine ID; the Kubernetes API and CRDs were real. This does not constitute migration of a running legacy cluster.
- Four Python management regression tests passed: interrupted migration replay, claim safety, mapping safety and Tailscale MTU derivation.
- Four existing node-helper tests passed. Shell syntax, Python parsing and `git diff --check` passed.
- Provider binaries built for Linux AMD64 and ARM64. The new management installation was executed on AMD64; a fresh ARM64 management installation was not performed in this round.

## Fresh management and tenant installation

A temporary Ubuntu 24.04 VM hosted a new Kind management site and a separate Docker tenant control plane. A second Docker node, on another bridge, served as the CPU worker. The dependency image was cached; the new builder overlaid checksum-verified Kubernetes 1.36.3, OVN 2.4.2 and agent artifacts. The target VM kernel config was supplied explicitly.

1. Built the control-plane image and checked Kubernetes version and non-root Python execution.
2. Packaged four images, the Provider binary and runtime files with SHA-256 checksums and image IDs.
3. Initialized a new management site. Repeated initialization passed without creating another tenant.
4. Exported the CLI client and sourced it in a terminal with an initially minimal PATH. `nvkectl`, `nv get clusters` and `wk get nodes` worked.
5. Registered both hosts, obtained SPIRE certificates, and let NVKE provision the control plane and worker. Both Nodes reached `Ready`, with `vmetal://` provider IDs.
6. Ran ordinary Pods on each node. HTTP succeeded in both directions.
7. Restarted the generated manager network recovery unit. API `/readyz` returned `ok`; a second pair of Pod HTTP checks succeeded in both directions.
8. Network diagnostics passed API CA/hostname verification, SPIRE TCP connectivity, route/MTU inspection and agent certificate expiry checks. UDP remained explicitly marked as requiring the separate workload test, which passed.

The Docker worker fixture needed scoped exceptions for Docker's unpublished-port isolation and a no-SNAT rule between its bridge and the control-plane bridge. These rules were confined to the temporary VM. They are additional test-fixture setup; the bare-metal worker installer was not exercised by this Docker worker test. The existing remote Spark remained registered in its original site.

## Problems found and corrected

- Merged-/usr extraction and private directory modes could make a node image unusable. The builder normalizes archive directories and verifies execution as the SSH user.
- macOS AppleDouble files copied into a bundle could be interpreted as manifests. Packaging removes them before computing the checksum manifest.
- A fresh host's inotify quota could be exhausted by Kind, OVN and additional systemd containers. This caused a worker's PID 1 to exit before SSH became available. Management initialization now persists adequate minimum limits and preserves higher settings; repeated initialization verified the resulting configuration.
- Docker's additional isolation and source NAT in the worker fixture initially blocked agent RPC and overlay traffic. After correcting only the fixture, NVKE retried provisioning successfully. Both HTTP directions were then verified before and after manager network recovery.

## Existing environment and cleanup

The existing Spark Provider controller was not replaced during these tests. Its cluster and experiment deployments were retained. Two CPU-only experiment VMs were temporarily stopped to free memory and were started again after the temporary test VM was removed. The CPU control-plane VM did not finish graceful shutdown within the timeout and was forcibly stopped. Both CPU Nodes returned to `Ready` after restart; their provider IDs matched the recorded values, and another bidirectional Pod HTTP test passed.

The additional build and VM disk footprint triggered `DiskPressure` on the existing Spark control-plane Node. Cleanup removed the temporary VM and bundle and only this round's identified reclaimable build cache. ThinkPad root free space returned to approximately 38 GiB. At 23:37:02 UTC the Spark control-plane `DiskPressure` condition returned to `False`; the worker also reported `DiskPressure=False`. All 33 existing Spark Deployments had their desired available replicas, both Nodes were `Ready`, and the Spark still advertised one GPU. A fresh pair of ordinary Pods passed HTTP in both directions across the existing Spark tenant.

## Scope and remaining administrator actions

The legacy migration tool handles released host inventory. Active legacy Machines, workloads and provider IDs are released through their original controllers; this is not an in-place API-group conversion for a live cluster. Tailscale route/grant approval remains an administrator action. Presented agent certificate expiry and SPIRE connectivity checks do not replace long-duration renewal or whole-host reboot testing.
