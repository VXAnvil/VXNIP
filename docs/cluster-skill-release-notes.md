# VXNIP cluster skill v0.3.0-rc.4

Fix Pod egress on bare-metal OVN local-gateway workers that share a host with Docker. Host/image-download connectivity and Node Ready previously could pass while ordinary Pod traffic was dropped by Docker's default forwarding policy.

- Persist two narrowly scoped forwarding rules through a plan/physical-host-bound recovery service: the current local Pod subnet to the actual uplink, and established/related replies. Preserve the default policy, Docker, routes and OVN-owned SNAT; stop for administrator deny/custom policies or ambiguous iptables backends.
- Require fresh external DNS, numeric-IP TCP and certificate-validated HTTPS probes on every workload node, including the Docker control plane, before announcing readiness. Record each result and clean temporary probe namespaces by recorded identity.
- Allow explicit probe targets for networks that restrict public destinations. Distinguish host egress, internal DNS and Pod egress in the operator instructions. Keep OVN/OVS as the CNI implementation.
- Retain GPU/CDI behavior, hardware-neutral worker naming, source pins and the existing native runtime release.

Validation uses offline behavior tests and a required isolated Linux CI network-namespace test. The kernel test reproduces the dropped connection with existing SNAT, verifies the repair without changing the default policy/NAT/routes, checks real recovery after rule removal/uplink changes and rejects untrusted TLS. No physical Spark is used for this release testing. This is not a fresh end-to-end installation, physical reboot or model/NCCL acceptance claim.

Changed runtime hashes require fresh reviewed plans for new installations. Preserve the original skill/plan/state for interrupted older runs; this update does not adopt or reinstall active allocations. Download vxnip-cluster.zip and its .sha256 file.
