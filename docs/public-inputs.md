# Prepare public inputs on Ubuntu

Use this page when you have no existing NVKE checkout, management bundle or node artifacts. It prepares the inputs for [NVKE setup](nvke-quickstart.md) using public source repositories and upstream downloads. No GitHub or NVIDIA account is required for this pinned evaluation path. Use the fixed release source and matching node packages below.

This is an evaluation setup for Ubuntu 24.04 on AMD64 or ARM64. The commands select native AMD64 or ARM64 tools and packages. Validate each installation using the readiness and network checks in the setup guide. Use a fresh management host with sudo, at least 6 CPUs, 16 GiB RAM and 100 GiB free disk, plus a separate unallocated Ubuntu worker. A CPU-only worker is enough for the first Ready-node check. GPU software and models are separate steps. Do not run host preparation against a machine already allocated to another cluster.

Run the build sections as your regular user, in one Bash session on the management host. Build natively for that host architecture. Source builds download several GB and can take time. Keep the generated bundle and its checksum manifest together.

## Install build prerequisites

```bash
sudo apt-get update
sudo apt-get install -y build-essential git curl jq ca-certificates openssh-client \
  python3 python3-yaml iproute2 openssl gnupg dpkg-dev zstd xz-utils \
  docker.io docker-buildx conntrack socat ethtool kmod
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Log out and back in so Docker group membership takes effect. `docker info` must succeed. Docker group membership grants administrative access to this dedicated host.

Use normal public build-file permissions. A private `umask 077` makes the upstream temporary Debian `control` directory invalid; keep invitations and site credentials private through the installers, rather than applying that mask to all build output.

```bash
set -euo pipefail
umask 022
export VXNIP_WORKSPACE="$HOME/public-vxnip"
mkdir -p "$VXNIP_WORKSPACE"
cd "$VXNIP_WORKSPACE"
mkdir -p downloads artifacts logs tools
root=$PWD
arch=$(dpkg --print-architecture)
case "$arch" in amd64|arm64) ;; *) echo "Unsupported architecture"; exit 1;; esac
export GIT_TERMINAL_PROMPT=0
export GOMAXPROCS=4 GOFLAGS=-p=3
docker info
```

## Check the host and VM prerequisites

The Docker control plane uses the management host's kernel. Check its Open vSwitch module and kernel config before building the image:

```bash
sudo modprobe openvswitch
test -r "/boot/config-$(uname -r)"
```

If the module or matching config is missing, prepare the matching Ubuntu kernel packages before continuing. The control-plane image must be rebuilt when the target Docker host kernel changes.

For the CPU worker path, use a dedicated Ubuntu Server VM with cloud-init's first boot completed:

```bash
cloud-init status --wait
```

The preparation script requires `status: done`. Give the VM a unique hostname and machine ID, usable SSH and a reachable private address. A CPU worker VM needs at least 2 CPUs and 4 GiB RAM.

For a management VM provisioned with a NoCloud seed CD, preserve that VM's own seed/metadata for subsequent boots and detach the virtual seed CD after cloud-init completes, using the hypervisor's controls. Do this before starting the privileged Docker tenant: it can see the VM's block devices and must not consume the management VM's seed instead of its own. Follow the hypervisor's procedure for its configuration drive; boot/config-drive handling varies by platform.

## Install Go and check out fixed source versions

Building the Provider alone requires Go 1.24+, but this NVKE revision requires **Go 1.27.4**. The commands below obtain its checksum from the official Go download metadata.

```bash
curl -fsSL --retry 3 'https://go.dev/dl/?mode=json&include=all' -o go-releases.json
python3 - "$arch" <<'PY_GO'
import json, sys
from pathlib import Path
releases = json.loads(Path("go-releases.json").read_text())
asset = next(f for r in releases if r["version"] == "go1.27.4"
             for f in r["files"] if f["os"] == "linux"
             and f["arch"] == sys.argv[1] and f["kind"] == "archive")
Path("go-download.txt").write_text(asset["filename"] + "\n")
Path("go.SHA256SUMS").write_text(asset["sha256"] + "  " + asset["filename"] + "\n")
PY_GO
go_archive=$(cat go-download.txt)
curl -fL --retry 3 "https://go.dev/dl/$go_archive" -o "$go_archive"
sha256sum -c go.SHA256SUMS
sudo tar -C /usr/local -xzf "$go_archive"
export PATH="/usr/local/go/bin:$root/tools:$PATH"
go version

git clone --depth 1 --branch v0.0.3-rc.1 \
  https://github.com/VXAnvil/VXNIP.git provider
git init nvke
git -C nvke remote add origin https://github.com/dsx-ai-factory/nvke.git
git -C nvke fetch --depth 1 origin 2f31480b360cbd7fc9d9db7a6ba274a8d0fd5049
git -C nvke checkout --detach FETCH_HEAD
test "$(git -C provider describe --tags --exact-match)" = v0.0.3-rc.1
test -z "$(git -C provider status --porcelain --untracked-files=no)"
test "$(git -C nvke rev-parse HEAD)" = 2f31480b360cbd7fc9d9db7a6ba274a8d0fd5049
```

Use a new workspace directory. Stop if an output or checkout already exists; do not overwrite a different installation.

## Download and verify management tools

These are the versions used by the pinned path: kind v0.32.0, kubectl v1.36.3, clusterctl v1.13.6 and Helm v4.2.4. The clusterctl digest comes from the chosen public GitHub Release asset metadata. The other tools publish checksum files.

```bash
curl -fsSL --retry 3 "https://github.com/kubernetes-sigs/kind/releases/download/v0.32.0/kind-linux-$arch" -o "downloads/kind-linux-$arch"
curl -fsSL --retry 3 "https://github.com/kubernetes-sigs/kind/releases/download/v0.32.0/kind-linux-$arch.sha256sum" -o downloads/kind.sha256sum
(cd downloads && sha256sum -c kind.sha256sum)
install -m 0755 "downloads/kind-linux-$arch" tools/kind
curl -fsSL --retry 3 "https://dl.k8s.io/release/v1.36.3/bin/linux/$arch/kubectl" -o downloads/kubectl
curl -fsSL --retry 3 "https://dl.k8s.io/release/v1.36.3/bin/linux/$arch/kubectl.sha256" -o downloads/kubectl.sha256
(cd downloads && printf '%s  kubectl\n' "$(cat kubectl.sha256)" | sha256sum -c -)
install -m 0755 downloads/kubectl tools/kubectl
curl -fsSL --retry 3 'https://api.github.com/repos/kubernetes-sigs/cluster-api/releases/tags/v1.13.6' -o downloads/capi-release.json
python3 - "$arch" <<'PY'
import json,sys
from pathlib import Path
name='clusterctl-linux-'+sys.argv[1]
a=next(a for a in json.loads(Path('downloads/capi-release.json').read_text())['assets'] if a['name']==name)
assert a['digest'].startswith('sha256:')
Path('downloads/clusterctl.SHA256SUMS').write_text(a['digest'][7:]+'  '+name+'\n')
PY
curl -fsSL --retry 3 "https://github.com/kubernetes-sigs/cluster-api/releases/download/v1.13.6/clusterctl-linux-$arch" -o "downloads/clusterctl-linux-$arch"
(cd downloads && sha256sum -c clusterctl.SHA256SUMS)
install -m 0755 "downloads/clusterctl-linux-$arch" tools/clusterctl
curl -fsSL --retry 3 "https://get.helm.sh/helm-v4.2.4-linux-$arch.tar.gz" -o "downloads/helm-v4.2.4-linux-$arch.tar.gz"
curl -fsSL --retry 3 "https://get.helm.sh/helm-v4.2.4-linux-$arch.tar.gz.sha256sum" -o downloads/helm.sha256sum
(cd downloads && sha256sum -c helm.sha256sum && tar -xzf "helm-v4.2.4-linux-$arch.tar.gz")
install -m 0755 "downloads/linux-$arch/helm" tools/helm
```

## Obtain the Provider and build nvkectl

The installer takes a native controller executable. Build it from the fixed public release checkout:

```bash
(cd provider && go build -trimpath -o "$root/tools/vxnip-controller" ./cmd/vxnip-controller)
make -C nvke/cli build
install -m 0755 nvke/cli/bin/nvkectl tools/nvkectl
```

The NVKE controller image is built below from the exact public checkout, with the build context at the NVKE repository root. This path does not rely on permission to pull NVKE's published controller image.

## Build node artifacts from public source

The reference package builders download and verify the Kubernetes artifacts in the committed architecture lock files, build OVN 2.4.2 from its locked public source, and verify the public SPIRE binaries. They also build a local dependency image with the agent and container networking services. This avoids a private bundle server or a copy of someone else's node image.

```bash
make -C nvke/os-image/packages/kubeadm-distro debs ARCH="$arch" K8S_VERSIONS=v1.36.3 DEB_REVISION=1 OUT_DIR="$root/artifacts" >logs/kubeadm-deb.log 2>&1
BUNDLE_ARCH="$arch" bash nvke/controller/hack/ci/prepare-capd-bundles.sh "$root/artifacts" >logs/ovn-spire.log 2>&1
CAPD_KINDEST_NODE_PLATFORM="linux/$arch" CAPD_KINDEST_NODE_TAG=v1.36.4 CAPD_KINDEST_NODE_IMAGE=vxnip-public-node-base:2f31480b \
 CAPD_NODE_AGENT_LOCAL_BUILD=true NODE_AGENT_VERSION=public-2f31480b CAPD_SPIRE_ATTESTATION_METHOD=joinToken \
 SPIRE_AGENT_BUNDLE_PATH="$root/artifacts/spire-agent.tgz" \
 CAPD_CNI_CHARTS_DEB_PATH="$root/artifacts/nvke-cni-ovn-charts-2.4.2_2.4.2_$arch.deb" \
 CAPD_CNI_IMAGES_DEB_PATH="$root/artifacts/nvke-cni-ovn-images-2.4.2_2.4.2_$arch.deb" \
 CAPD_HELM_DEB_PATH="$root/artifacts/nke-e2e-helm.deb" \
 bash nvke/controller/hack/capd-kindest-node/build-image.sh >logs/dependency-base.log 2>&1
cid=$(docker create vxnip-public-node-base:2f31480b)
trap 'docker rm -f "$cid" >/dev/null 2>&1 || true' EXIT
for tool in nvke-agent spire-agent; do
 docker cp "$cid:/usr/local/bin/$tool" "artifacts/$tool"
 docker cp "$cid:/etc/systemd/system/$tool.service" "artifacts/$tool.service"
done
docker rm "$cid" >/dev/null
trap - EXIT
(cd artifacts && sha256sum nvke-kubeadm-distro-*_${arch}.deb nvke-cni-ovn-*_${arch}.deb nvke-agent spire-agent nvke-agent.service spire-agent.service >SHA256SUMS)
```

The seven helper inputs in `artifacts/SHA256SUMS` must all be present, match the intended architecture and pass verification. Preserve this manifest when copying the artifacts to workers. The Kubernetes package also carries its pinned runtime and control-plane images; it is more than a kubeadm executable.

## Build the profile and control-plane images, then package

The profile image uses NVKE's **public test fixtures and matching runbooks**, not the small standalone example profile or files from another management site. These are evaluation profiles; production policy and qualification are outside this guide.

```bash
docker build -f nvke/controller/Dockerfile --build-arg BUILD_VERSION=2f31480b \
  -t vxnip-public-nvke-controller:2f31480b nvke
docker build -f nvke/controller/test/fixtures/Dockerfile \
  -t docker.local/cwe-profiles:public-2f31480b nvke/controller/test/fixtures

bash provider/scripts/build-nvke-control-plane.sh \
  vxnip-public-node-base:2f31480b "$root/artifacts" \
  vxnip-control-plane:public-1.36.3 "/boot/config-$(uname -r)"

export NVKE_CHECKOUT="$root/nvke"
export LINUX_TOOL_DIR="$root/tools"
export KIND_NODE_IMAGE=kindest/node:v1.36.4
export NVKE_CONTROLLER_IMAGE=vxnip-public-nvke-controller:2f31480b
export NVKE_PROFILE_IMAGE=docker.local/cwe-profiles:public-2f31480b
export VXNIP_CONTROL_PLANE_IMAGE=vxnip-control-plane:public-1.36.3

bash provider/scripts/package-nvke-management.sh \
  "$NVKE_CHECKOUT" "$LINUX_TOOL_DIR" "$KIND_NODE_IMAGE" \
  "$NVKE_CONTROLLER_IMAGE" "$NVKE_PROFILE_IMAGE" "$root/management-bundle"
(cd management-bundle && sha256sum --quiet -c SHA256SUMS)
```

`docker.local/cwe-profiles` is a local image name, not a registry that you must sign into. Initialization side-loads it into Kind. The control-plane image contains the helper, node artifacts and this host's kernel configuration; rebuild it for a different Docker host kernel.

The result is:

- `management-bundle/`: verified tools, NVKE deployment files, selected Provider runtime files and four image payloads;
- `artifacts/`: verified native worker packages, agents, units and their checksum manifest;
- `provider/`: the public Provider source checkout.

Continue with [management initialization and worker enrollment](nvke-quickstart.md#prepare-the-management-site). Use this bundle and `CP_IMAGE=vxnip-control-plane:public-1.36.3` for the Docker tenant. Do not repeat packaging into the same output directory.

All downloads and builds need outbound access to the public upstream hosts. Source-built image bytes can differ with package repository updates; the generated manifests identify the bytes you actually built. Keep those manifests and fixed source revisions for your installation.
