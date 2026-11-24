# Pod egress with Docker and OVN local gateway

Node Ready, image pulls and host HTTPS do not establish Pod egress. Test the host and an ordinary Pod separately, then distinguish internal DNS, external DNS, numeric-IP TCP and certificate-validated HTTPS. A busybox wget build without certificate verification only demonstrates connectivity, not verified TLS.

## Known Docker forwarding failure

OVN/OVS supplies the cluster data plane. In local gateway mode, OVN also installs native nftables SNAT rules. Docker may leave the host's FORWARD policy at DROP without a matching allowance for traffic arriving on ovn-k8s-mp0. Pod-to-Pod traffic can work while outbound traffic and node-local-dns upstream queries time out.

Inspect the active iptables backend, DOCKER-USER/FORWARD chains, OVN's native nftables table, the management-port subnet and the actual uplink from ip -j -4 route get 1.1.1.1. Do not infer that SNAT is missing from iptables -t nat alone: OVN's native nftables chains are separate.

For this failure, new installs persist only:

1. ovn-k8s-mp0 + the current local Pod subnet to the actual host uplink, for new/established/related traffic.
2. Actual uplink to ovn-k8s-mp0 + that subnet, for established/related replies.

The recovery service checks the exact plan and physical machine identity, waits for CNI, reconciles every five seconds, and removes its exact prior rules when a subnet/uplink changes. It does not set global FORWARD ACCEPT, flush rules, disable Docker's firewall, add duplicate SNAT, change default routes or replace the CNI. Unknown administrator deny/custom policies and ambiguous backends require inspection.

For an existing repaired cluster, inspect persistence separately. Current in-memory rules do not prove reboot/recovery coverage. Do not overwrite an old run's runtime files, rerun create, restart/reallocate workers or switch to eBPF merely to repair this forwarding boundary. Use the actual cluster owner's authorized maintenance path.

## Acceptance targets

Each fresh acceptance runs a scheduler-selected, non-GPU Pod on every workload node. All three checks must pass independently:

- Resolve example.com.
- Open a numeric-IP TCP connection to 1.1.1.1:443.
- Fetch https://example.com/ with normal CA and hostname verification.

Select reachable allowed endpoints explicitly when those public defaults are restricted:

    {
      "egress": {
        "dns_name": "egress.example.net",
        "tcp_ip": "192.0.2.10",
        "tcp_port": 443,
        "https_url": "https://egress.example.net/health"
      }
    }

Replace documentation addresses with real allowed endpoints. Do not disable certificate verification to turn a failure into a pass. Private logs retain individual DNS/TCP/TLS results and Node/Pod/image identities. Existing inspection remains read-only; it does not run these active acceptance probes.
