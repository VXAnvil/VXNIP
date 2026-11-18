# VMetal

VMetal is a Cluster API infrastructure provider for existing Linux machines in home labs and small labs. It discovers hosts over SSH, allocates them to Kubernetes clusters, and supports both kubeadm bootstrapping and NVIDIA NVKE's generic infrastructure contract.

The lab has verified Ubuntu AMD64 and ARM64 workers, a Docker-hosted NVKE workload control plane, a GTX 1080 Ti worker, and a remote DGX Spark worker. Checks covered node readiness, Pod networking, NVKE agent certificates, GPU registration, and recovery after reboot. See the [NVKE setup guide](docs/nvke-quickstart.md) for the deployment inputs and verification steps.

VMetal retains the existing `HomelabHost`, `HomelabCluster`, `HomelabMachine`, and `HomelabMachineTemplate` APIs. Existing controller commands, the default inventory location, and the `homelab://` provider ID format also carry forward. These names appear in Kubernetes objects, state directories, and the upgrade procedure.

## Quickstart

The standalone CLI runs on macOS or Linux with Go 1.24 or newer and an OpenSSH client. The initial target is an existing Ubuntu host with Python 3, SSH access and a valid, unique `/etc/machine-id`. Configure SSH key authentication and verify the host key in your OpenSSH known-hosts file before registration. Set any custom port or identity file through an OpenSSH host alias.

```sh
go build -o vmetal .
./vmetal register desktop ubuntu@desktop.local
./vmetal list
./vmetal inspect desktop
./vmetal refresh desktop
./vmetal reserve desktop lab-cluster
./vmetal release desktop lab-cluster
go test ./...
```

`inspect` reads the last successful probe and `refresh` contacts the host again. The observation timestamp records the last successful contact. Discovery runs unprivileged and reads system information over SSH. PCI discovery can identify NVIDIA display devices without a driver. GPU names, VRAM, driver version and the driver's reported maximum CUDA version depend on a working `nvidia-smi`.

The inventory is stored in `nvke-homelab-provider/inventory.json` under the user's OS configuration directory. Set `-state /path/to/inventory.json` before the subcommand to use a different file. Use one inventory on a local filesystem for all reservations in a homelab. Registration detects aliases using a hash of the Linux machine ID, so cloned installations need unique IDs. Reservations are advisory ownership records scoped to that inventory. Release checks the cluster owner and updates only the local record.

## Try Kubernetes host allocation

Use a development Kubernetes cluster and set `KUBECONFIG` to its kubeconfig before running these commands. The controller uses the same OpenSSH configuration as the CLI, so run it from a machine that can reach the Ubuntu host. The kubeconfig identity needs cluster-wide access to watch the homelab resources, update their status and finalizers, and manage a leader-election Lease in `kube-system`. Restrict creation of these resources to trusted operators. A namespaced `HomelabMachine` can request any registered host.

```sh
kubectl apply -f config/crd
go run ./cmd/homelab-controller
```

In a second terminal with the same `KUBECONFIG`, use the CLI and `jq` to get the machine identity. Replace `ubuntu@worker.local` with your Ubuntu test host.

```sh
go build -o vmetal .
target=ubuntu@worker.local
id=$(./vmetal register test-worker "$target" | jq -er '.hardware.machine_id_hash')
host=host-$(printf '%s' "$id" | cut -c 1-32)
kubectl apply -f - <<EOF
apiVersion: homelab.infrastructure.cluster.x-k8s.io/v1alpha1
kind: HomelabHost
metadata:
  name: $host
spec:
  machineID: "$id"
  sshTarget: "$target"
---
apiVersion: homelab.infrastructure.cluster.x-k8s.io/v1alpha1
kind: HomelabMachine
metadata:
  name: test-worker
  namespace: default
spec:
  hostRef: $host
EOF
kubectl get homelabhosts
kubectl -n default get homelabmachines
```

The controller verifies the SSH identity, then records a reservation for the machine's Kubernetes UID. Competing requests stay `Pending` while the host is claimed. Delete a standalone `HomelabMachine` to release it. A host marked for deletion stays in the API until its reservation is released. The standalone workflow above only uses read-only SSH probes. Keep a physical machine in a single allocation system, since the standalone JSON inventory and the Kubernetes controller do not share reservations.

## Bootstrap a two-VM workload cluster

The lab example has been verified with Cluster API v1.13.6, workload Kubernetes v1.34.13, Ubuntu 24.04 VMs, and Flannel v0.28.10. The worker was scaled down, reset, released, and admitted again on the same VM with a new provider ID.

Use a separate development management cluster with Cluster API's `v1beta2` core, kubeadm bootstrap, and kubeadm control-plane providers. The two workload VMs must be disposable Ubuntu installations with unique hostnames and machine IDs, key-based SSH, Python 3, kubeadm and kubelet at the workload Kubernetes version (use Kubernetes 1.34.x for this example), and working containerd with the systemd cgroup driver. Disable swap and configure the usual kubeadm networking prerequisites on the VMs. The management cluster and the worker must be able to reach the control-plane VM's stable IP on TCP 6443. Choose cluster CIDRs that do not overlap your VM, LAN, or management networks.

On **each VM**, install the helper as root and use `sudo visudo -f /etc/sudoers.d/homelab-kubeadm` to grant only the SSH account access to the exact helper command. Replace `ubuntu` with that VM's SSH user.

```sh
sudo install -o root -g root -m 0755 scripts/homelab-kubeadm /usr/local/sbin/homelab-kubeadm
```

```sudoers
ubuntu ALL=(root) NOPASSWD: /usr/local/sbin/homelab-kubeadm ""
```

On the controller host, apply the CRDs and start the controller as in the allocation example. Apply `kubectl apply -f config/rbac/capi.yaml` to let the CAPI and kubeadm control-plane controllers manage the provider's infrastructure objects. The Homelab controller also needs to read CAPI Machines, Clusters, and kubeadm bootstrap Secrets. With the management kubeconfig selected, set the VM endpoints and the exact version installed on both VMs, then render the example:

```sh
(
  set -eu
  export CONTROL_PLANE_SSH=ubuntu@192.168.56.10 WORKER_SSH=ubuntu@192.168.56.11
  export CONTROL_PLANE_IP=192.168.56.10 KUBERNETES_VERSION=v1.34.0
  CONTROL_PLANE_ID=$(./vmetal register lab-cp "$CONTROL_PLANE_SSH" | jq -er '.hardware.machine_id_hash')
  WORKER_ID=$(./vmetal register lab-worker "$WORKER_SSH" | jq -er '.hardware.machine_id_hash')
  test -n "$CONTROL_PLANE_ID" && test -n "$WORKER_ID" || exit 1
  export CONTROL_PLANE_ID WORKER_ID
  export CONTROL_PLANE_HOST=host-$(printf '%s' "$CONTROL_PLANE_ID" | cut -c 1-32)
  export WORKER_HOST=host-$(printf '%s' "$WORKER_ID" | cut -c 1-32)
  envsubst < examples/kubeadm-two-vm.yaml.tmpl > /tmp/homelab-lab.yaml
  kubectl apply -f /tmp/homelab-lab.yaml
)
```

When the control-plane API is reachable, run `clusterctl get kubeconfig homelab-lab > /tmp/homelab-lab.kubeconfig` and install a CNI configured for the example's pod CIDR. Check `kubectl --kubeconfig /tmp/homelab-lab.kubeconfig get nodes -o wide`. To verify worker reuse, scale the worker MachineDeployment to zero, wait for its Machines to finish deleting and the host claim to clear, then scale back to one and wait for the worker to become Ready again.

The example uses the default kubeadm cloud-config for a single control plane and a worker. The helper applies kubeadm configuration and the required control-plane keys. It rejects additional shell commands, cloud-init operations, and control-plane joins. It persists the allocation attempt, so an interrupted or failed init/join requires reset before reuse. Cleanup runs `kubeadm reset` and removes the CNI configuration on an opted-in VM. If cleanup fails, the host remains claimed. Keep diagnostics such as `/var/lib/homelab-kubeadm/kubeadm.log` on the VM, since they can contain bootstrap credentials.

The API integration test uses an isolated API server and etcd. With [envtest assets](https://book.kubebuilder.io/reference/envtest) for Kubernetes 1.34 installed, run `KUBEBUILDER_ASSETS=/path/to/envtest go test -race ./...`. Without the assets, the API integration test is skipped. Run `python3 scripts/test_homelab_kubeadm.py` to verify the VM helper's initial state check.

## Test the NVKE infra adapter

See the [NVKE setup guide](docs/nvke-quickstart.md) for management setup, worker enrollment, and GPU worker checks.

The experimental adapter targets the generic `infra` contract in NVKE commit `2f31480`. It uses the standard `infrastructure.cluster.x-k8s.io` group for cluster and machine objects, with the existing `HomelabHost` inventory shared by both API groups. The kubeadm-only example continues to use the original API group.

Use a dedicated NVKE development deployment with `supportedInfraProviders: infra`, tenant kubeconfig publishing, and SPIRE join-token attestation for this experiment. Prepare two unallocated Linux nodes with the matching NVKE agent and service, the SPIRE agent and its `spire` system account, the required NVKE node dependencies and CNI packages, and the Homelab helper and SSH configuration from the previous example. Verify that `cloud-init status` reports `done` on both nodes, as required by NVKE's node preflight. The pinned agent unit should recognize both `nke-agent.service` and `nvke-agent.service`. Keep existing agent configuration off these dedicated test nodes. The helper places the supplied agent configuration, adds a kubelet identity drop-in, and starts the services. NVKE drives `kubeadm init/join` through its agent.

When using preinstalled OVN Debian packages, set `cni.installAfterInit: true` in the NVKE controller Helm values before creating the workload cluster. NVKE then installs the CNI from the node's OVN charts package after kubeadm initializes the control plane.

Select the dedicated management kubeconfig and deploy:

```sh
kubectl apply -f config/crd
kubectl apply -f config/crd/nvke
kubectl apply -f config/rbac/capi.yaml
kubectl apply -f config/rbac/nvke.yaml
HOMELAB_ENABLE_NVKE_INFRA=1 go run ./cmd/homelab-controller
```

The stock CAPI installation can manage resources in the standard infrastructure API group. The RBAC in `config/rbac/nvke.yaml` grants the NVKE service account access to the three Homelab infrastructure kinds in the NVKE workload namespace and read access to their CRD definitions. Adjust both namespaces and the service account to match your test deployment.

When NVKE publishes SPIFFE exec-auth kubeconfigs, run its upstream `controller/hack/apply-capi-spiffe-exec-auth.sh --kubeconfig <management-kubeconfig>` after the SPIRE CSI driver is installed. This lets the CAPI controllers authenticate to the workload API and match Machines to Nodes.

Use the read-only probe to get each machine ID. Create two `HomelabHost` records as in the allocation example, with `spec.allowBootstrap: true` and `spec.nodeIP` set to the routable IPv4 address reported by the host. The management network must reach the agent on both hosts, and each host must reach the SPIRE server and the workload control-plane endpoint. For a virtual worker, use a guest address reachable from the management host. Use the guest's architecture when preparing binaries and container images.

Configure `nvkectl` for the dedicated test site. Export `CONTROL_PLANE_HOST` and `WORKER_HOST` with the two inventory names, `CONTROL_PLANE_IP` with the control-plane address, and `KUBERNETES_VERSION` with the NVKE workload version (without the leading `v`), then render:

```sh
envsubst < examples/nvke-two-hosts.yaml.tmpl > /tmp/homelab-nvke-lab.yaml
nvkectl create cluster -f /tmp/homelab-nvke-lab.yaml
nvkectl get clusters
nvkectl get kubeconfig homelab-nvke-lab --output-file /tmp/homelab-nvke-lab.kubeconfig
kubectl --kubeconfig /tmp/homelab-nvke-lab.kubeconfig get nodes
```

Check that both nodes are `Ready` and each Node's provider ID matches its CAPI Machine. Scale the worker pool down and restore it to verify that the former allocation is cleaned up and the same host returns with a new provider ID. The current adapter supports join-token agent configuration and rejects bootstrap payloads with extra shell commands, custom files, or other SPIRE attestors.
