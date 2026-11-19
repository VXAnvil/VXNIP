# Examples

| File | Use |
| --- | --- |
| [nvke-docker-gpu.env.example](nvke-docker-gpu.env.example) | Network layout for a Docker control plane and AMD64 NVIDIA GPU worker |
| [nvke-docker-control-plane.Dockerfile](nvke-docker-control-plane.Dockerfile) | Control-plane image input used by `scripts/build-nvke-control-plane.sh` |
| [nvke-two-hosts.yaml.tmpl](nvke-two-hosts.yaml.tmpl) | NVKE cluster and worker allocation through the generic infrastructure adapter |
| [kubeadm-two-vm.yaml.tmpl](kubeadm-two-vm.yaml.tmpl) | Standalone CAPI cluster using kubeadm bootstrap |

Start with the [NVKE setup guide](../docs/nvke-quickstart.md). For template variables and prerequisites, see [development examples](../docs/development.md). Build the Docker image using the [verified image workflow](../docs/operations.md#build-a-reproducible-control-plane-image).

Replace the example addresses with routable addresses and non-overlapping networks. Keep hostnames and machine IDs unique.
