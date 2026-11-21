# Cluster skill: mixed architecture and preservation validation

Date: 2026-11-20 (America/Vancouver).

This record covers the working-tree VMetal skill/executor changes for mixed AMD64/ARM64 artifacts, Tailscale, isolated bare-metal preparation and read-only existing-cluster inspection. It does not describe a published release. Existing-mode validation and the isolated mixed-architecture VM install/verification described below completed. A fresh unallocated physical bare-metal install was not performed.

## Automated checks

- 48 offline behavioral tests passed in `scripts/test_cluster_bootstrap.py`.
- Existing management-tool and kubeadm Python regressions passed (4 + 4 tests).
- Python AST and Bash syntax checks passed for repository scripts, workflows and the skill.
- ShellCheck, Python/Bash syntax and skill metadata validation passed; behavioral and live checks cover the HTTP readiness fix.
- The live-acceptance ZIP was checked for corruption and its runtime hashes match its exact plan. Public release archives are packaged from the tagged sources with their own SHA-256 checksum.
- Three Linux-only management-bundle checks were skipped on the macOS validation host. Go/provider code was not changed in this task.

Behavioral coverage includes native artifact builder selection, Tailscale interface/address validation and resumable route gates, bare-metal compatibility, apt removal/upgrade/pending-configuration rejection, shared container/runtime/driver preservation, unknown allocation state, existing-mode dispatch without installers, detected workload drift, name-only CAPI nodeRef compatibility, UID/providerID mismatch rejection and credential-free packaging.

## Live environment and actions

The existing ThinkPad AMD64 management host and Spark ARM64 bare-metal worker were accessed through their already authenticated SSH ControlMaster sessions. No private key content was read, SSH trust was not changed, and neither host required sudo for existing-mode inspection.

The final `cluster.py plan` and `apply` CLI run used `mode: existing`, `network_mode: tailscale` and an empty changes list. The executor performed only read-only machine/API/network observations; it did not upload remote runtime state, fetch/build artifacts, install components, change routes or Tailscale preferences, register/reallocate hosts, restart services, create probe resources or modify existing workloads.

Results:

| Check | Observed result |
| --- | --- |
| Physical and Kubernetes identity | AMD64/ARM64 hosts match the expected Host -> infrastructure Machine -> CAPI Machine -> Node chain; provider IDs match allocation UIDs |
| API | Exact expected workload endpoint passes TLS and readyz |
| Manager to worker | Agent TCP 15005 and kubelet TCP 10250 reachable over Tailscale |
| Worker to management/control plane | SPIRE TCP 30441 reachable; workload API readyz passes CA/hostname validation |
| Tailscale preferences | Existing manager NoSNAT and worker route acceptance match the topology and remain unchanged |
| MTU | Both peer paths use interface MTU 1280; existing OVN MTU 1180 fits the calculated budget |
| Audit interval protection | 4 Hosts, 2 Nodes, 60 workload controllers and 76 Pods have unchanged protected observations |
| Whole-task baseline comparison | Node UID/providerID/bootID, Host UID/claim and Machine identities unchanged; all 35 Deployment identities, generation/replica/availability observations and 76 Pod identities/status/restart observations unchanged |
| Provider service | Same service state/process/restart observations as the initial task baseline |

The final existing-mode result is `checks_passed: true`, `unchanged: true`, `ready: null`. Fresh Pod HTTP/Service DNS probes and SPIRE authentication remain explicitly outside this read-only run. The actual CAPI nodeRef contains a name only, so `node_ref_uid_verified` is false; observed Node UIDs are retained and compared, and supplied UIDs would still be enforced. Spark's ordinary SSH account cannot inspect host Docker containers, so host-Docker coverage remains false rather than claiming an empty or healthy inventory.

Real connection data, machine identifiers, kubeconfig locations, full snapshots and exact plan digests are retained privately outside the repository and excluded from the skill ZIP.

## Isolated fresh-install validation completed

Two new Ubuntu 24.04 VMs run on Spark: an ARM64 KVM manager (6 CPUs, 16 GiB RAM) and an AMD64 TCG worker (2 CPUs, 8 GiB RAM). They use private user-mode NAT, loopback-only operator SSH forwards, independent disks/identities and dedicated cluster networks. No existing host bridge, GPU, shared runtime or allocation is reused. The user authenticated both guests to Tailscale.

Completed on the fresh manager: public source/checksum acquisition, native ARM64 Kubernetes/OVN/SPIRE artifacts and images, Kind/CAPI/NVKE/SPIRE/VMetal management setup, then new workload control-plane creation and API readyz. The AMD64 native build completed inside the emulated AMD64 guest. Both architecture-specific artifact manifests verified successfully. The user approved the manager's planned API/SPIRE routes; the worker gate verified tailscale0 selection and real API/SPIRE TCP connectivity before preparation. The manager preserves source addresses.

The real run exposed a naming collision: the original skill gave Kind management and workload control planes the same Docker name. Ownership checks correctly stopped before workload creation. The fix explicitly separates the management site and workload cluster names and adds two behavioral regressions. Operator recovery first verified the inactive process/remote lock, exact old digest, successful build/management receipts, artifact manifests, healthy management API, unchanged machine identities, empty tenant directory and absent workload inventory/network. The original unknown journal and runtime remain archived. Only the two verified completed phases were carried into an explicitly recorded corrected plan; the failed phase was not marked successful.

After the user's host relocation/reboot, the original workload control plane initially could not start because host swap had returned. Its kubelet configuration was backed up and changed to tolerate host swap while retaining Pod `NoSwap`; host swap and existing data volumes/allocations were preserved. Both original Nodes recovered to Ready and all 35 Deployments became available, with original Node/provider IDs and Deployment identities/specifications retained. Periodic checks during isolated builds still show 35/35 available. A subsequent existing-mode audit exposed the old control-plane image regenerating machine-id on restart. Its original value was recovered from a saved Node record and matched cryptographically to the unchanged Host registration; the current upstream entrypoint preservation guard was applied without restarting services. Host discovery returned to Verified. The follow-up read-only audit passed identities, TLS/routes/MTU and unchanged observations for 4 Hosts, 2 Nodes, 60 controllers and 76 Pods; its fresh Pod/Service/SPIRE-auth and host-Docker coverage remain explicitly false.

The final create-mode executor result is `ready: true`. Both Nodes are Ready on Kubernetes 1.36.3/containerd 2.3.0: an ARM64 Docker control plane in the management VM and an AMD64 VM worker. Exact Host -> infrastructure Machine -> CAPI Machine -> Node bindings and allocation-derived provider IDs were verified. Fresh Pod HTTP passed in both directions, and cross-node Service DNS/HTTP passed. Temporary test namespaces were removed. OVN MTU is 1180, within the Tailscale interface budget of 1280 minus 100 bytes. After joining, worker API/SPIRE routes still resolve via tailscale0; no LAN fallback was used.

The first network probe exposed an application-startup race: Kubernetes Pod Ready could precede BusyBox httpd listening, causing one direction to return connection refused under TCG. Adding an HTTP readiness probe made both directions pass. The fix is included in the provider's standalone helper and the skill's own portable Pod probe. Probe stdout/stderr are now retained in private per-run logs. Regressions cover early Pod readiness, real request failure, exact-namespace cleanup and diagnostic privacy. A verification-only plan update preserved and revalidated all completed install receipts and physical identities; it did not replay installation or node allocation. The corrected full executor verification then passed.

Final existing-mode protection also passed with `checks_passed: true`, `unchanged: true`, `ready: null`. The original protected snapshot before and after the later installation/test interval has the same digest: 4 Hosts, 4 Machines, 2 Nodes, 60 workload controllers and 76 Pods. All 35 original Deployments are available. Existing-mode coverage still explicitly excludes fresh probe creation, a full SPIRE audit and unavailable host-Docker observations.

This is a live mixed-architecture VM/Tailscale acceptance result with the audited naming and verification repairs above, not a claim that the first attempt was unattended or that a physical bare-metal fresh install was tested. The isolated bare-metal create path remains covered offline and must be validated separately on an unallocated physical machine.
