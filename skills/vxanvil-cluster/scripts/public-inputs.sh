#!/bin/bash
# Native public-source builds for the fixed, documented NVKE evaluation path.
set -euo pipefail
[[ ( $# == 1 || ( $# == 2 && $2 == --node-only ) ) && $EUID == 0 ]] || { echo 'Usage: sudo bash public-inputs.sh workspace [--node-only]' >&2; exit 2; }
root=$1
mode=${2:-manager}
scripts=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
mkdir -p "$root"
cd "$root"
umask 022
export DEBIAN_FRONTEND=noninteractive GIT_TERMINAL_PROMPT=0 GOMAXPROCS=4 GOFLAGS=-p=3 LC_ALL=C
apt-get update
packages=(build-essential git curl jq ca-certificates openssh-client python3 python3-yaml
  iproute2 openssl gnupg dpkg-dev zstd xz-utils conntrack socat ethtool kmod)
if [[ ${VXANVIL_USE_EXISTING_BUILD_DOCKER:-0} == 1 ]]; then
    docker info >/dev/null
    docker buildx version >/dev/null
else
    packages+=(docker.io docker-buildx)
fi
if [[ $mode == --node-only ]]; then
    # A native builder can share a bare-metal host with existing Docker workloads.
    # Never upgrade/remove their runtime dependencies as a side effect of building.
    apt-get -s --no-remove --no-upgrade install "${packages[@]}" >apt-plan.txt
    python3 "$scripts/apt_guard.py" apt-plan.txt
    apt-get install -y --no-remove --no-upgrade "${packages[@]}"
    systemctl is-active --quiet docker.service
else
    apt-get install -y "${packages[@]}"
    systemctl enable --now docker
    if [[ ${VXANVIL_PORTABLE_KERNEL_CONFIG:-0} != 1 ]]; then
        test -r "/boot/config-$(uname -r)"
    fi
fi
docker info >/dev/null
arch=$(dpkg --print-architecture)
case "$arch" in amd64|arm64) ;; *) echo 'Unsupported architecture' >&2; exit 1;; esac
mkdir -p downloads artifacts tools logs
download() {
    curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fL --retry 3 \
      --connect-timeout 20 --max-time 1800 "$1" -o "$2.part"
    mv "$2.part" "$2"
}
checkout() {
    local url=$1 revision=$2 directory=$3
    [[ ! -e $directory ]] || { echo 'Checkout already exists; reconcile interrupted build first.' >&2; exit 1; }
    git init "$directory"
    git -C "$directory" remote add origin "$url"
    git -C "$directory" -c core.hooksPath=/dev/null fetch --depth 1 origin "$revision"
    git -C "$directory" -c core.hooksPath=/dev/null checkout --detach FETCH_HEAD
    [[ $(git -C "$directory" rev-parse HEAD) == "$revision" ]]
}
provider_revision=${VXANVIL_PROVIDER_REVISION:-633b2efb1d3b087376b995c906887a3d8c994ba7}
[[ $provider_revision =~ ^[a-f0-9]{40}$ ]] || { echo 'Provider source requires an exact commit.' >&2; exit 1; }
checkout https://github.com/VXAnvil/cluster-api-provider-vxanvil.git "$provider_revision" provider
grep -Fx 'module github.com/VXAnvil/cluster-api-provider-vxanvil' provider/go.mod >/dev/null || {
    echo 'Provider pin must use the native VXAnvil module.' >&2; exit 1;
}
checkout https://github.com/dsx-ai-factory/nvke.git \
  2f31480b360cbd7fc9d9db7a6ba274a8d0fd5049 nvke

# The metadata is fetched over authenticated HTTPS from the tool's publisher.
# Every downloaded executable/archive is verified before use; no curl|bash.
download 'https://go.dev/dl/?mode=json&include=all' downloads/go-releases.json
python3 - "$arch" <<'PY'
import json,sys
from pathlib import Path
releases=json.loads(Path('downloads/go-releases.json').read_text())
asset=next(f for r in releases if r['version']=='go1.27.4' for f in r['files']
           if f['os']=='linux' and f['arch']==sys.argv[1] and f['kind']=='archive')
Path('downloads/go-name').write_text(asset['filename'])
Path('downloads/go.SHA256SUMS').write_text(asset['sha256']+'  '+asset['filename']+'\n')
PY
go_archive=$(cat downloads/go-name)
download "https://go.dev/dl/$go_archive" "downloads/$go_archive"
(cd downloads && sha256sum -c go.SHA256SUMS && tar -xzf "$go_archive" -C ../tools)
export PATH="$root/tools/go/bin:$root/tools:$PATH"
go version

download "https://github.com/kubernetes-sigs/kind/releases/download/v0.32.0/kind-linux-$arch" "downloads/kind-linux-$arch"
download "https://github.com/kubernetes-sigs/kind/releases/download/v0.32.0/kind-linux-$arch.sha256sum" downloads/kind.sha256sum
(cd downloads && sha256sum -c kind.sha256sum)
install -m 0755 "downloads/kind-linux-$arch" tools/kind
download "https://dl.k8s.io/release/v1.36.3/bin/linux/$arch/kubectl" downloads/kubectl
download "https://dl.k8s.io/release/v1.36.3/bin/linux/$arch/kubectl.sha256" downloads/kubectl.sha256
(cd downloads && printf '%s  kubectl\n' "$(cat kubectl.sha256)" | sha256sum -c -)
install -m 0755 downloads/kubectl tools/kubectl
download https://api.github.com/repos/kubernetes-sigs/cluster-api/releases/tags/v1.13.6 downloads/capi-release.json
python3 - "$arch" <<'PY'
import json,sys
from pathlib import Path
name='clusterctl-linux-'+sys.argv[1]
asset=next(a for a in json.loads(Path('downloads/capi-release.json').read_text())['assets'] if a['name']==name)
assert asset['digest'].startswith('sha256:')
Path('downloads/clusterctl.SHA256SUMS').write_text(asset['digest'][7:]+'  '+name+'\n')
PY
download "https://github.com/kubernetes-sigs/cluster-api/releases/download/v1.13.6/clusterctl-linux-$arch" "downloads/clusterctl-linux-$arch"
(cd downloads && sha256sum -c clusterctl.SHA256SUMS)
install -m 0755 "downloads/clusterctl-linux-$arch" tools/clusterctl
download "https://get.helm.sh/helm-v4.2.4-linux-$arch.tar.gz" "downloads/helm-v4.2.4-linux-$arch.tar.gz"
download "https://get.helm.sh/helm-v4.2.4-linux-$arch.tar.gz.sha256sum" downloads/helm.sha256sum
(cd downloads && sha256sum -c helm.sha256sum && tar -xzf "helm-v4.2.4-linux-$arch.tar.gz")
install -m 0755 "downloads/linux-$arch/helm" tools/helm
if [[ $mode != --node-only ]]; then
    (cd provider && go build -trimpath -o "$root/tools/vxanvil-controller" ./cmd/vxanvil-controller)
    make -C nvke/cli build
    install -m 0755 nvke/cli/bin/nvkectl tools/nvkectl
fi

make -C nvke/os-image/packages/kubeadm-distro debs ARCH="$arch" K8S_VERSIONS=v1.36.3 \
  DEB_REVISION=1 OUT_DIR="$root/artifacts" >logs/kubeadm-deb.log 2>&1
BUNDLE_ARCH="$arch" bash nvke/controller/hack/ci/prepare-capd-bundles.sh "$root/artifacts" >logs/ovn-spire.log 2>&1
base_image=vxanvil-public-node-base:2f31480b
if [[ $mode == --node-only ]]; then
    [[ ${VXANVIL_BUILD_LABEL:-} =~ ^[a-z0-9-]+$ ]]
    base_image=vxanvil-bootstrap-$VXANVIL_BUILD_LABEL-$arch:2f31480b
    if docker image inspect "$base_image" >/dev/null 2>&1; then
        echo 'Native builder image already exists; reconcile the prior build rather than overwrite it.' >&2
        exit 1
    fi
fi
CAPD_KINDEST_NODE_PLATFORM="linux/$arch" CAPD_KINDEST_NODE_TAG=v1.36.4 \
 CAPD_KINDEST_NODE_IMAGE="$base_image" CAPD_NODE_AGENT_LOCAL_BUILD=true \
 NODE_AGENT_VERSION=public-2f31480b CAPD_SPIRE_ATTESTATION_METHOD=joinToken \
 SPIRE_AGENT_BUNDLE_PATH="$root/artifacts/spire-agent.tgz" \
 CAPD_CNI_CHARTS_DEB_PATH="$root/artifacts/nvke-cni-ovn-charts-2.4.2_2.4.2_$arch.deb" \
 CAPD_CNI_IMAGES_DEB_PATH="$root/artifacts/nvke-cni-ovn-images-2.4.2_2.4.2_$arch.deb" \
 CAPD_HELM_DEB_PATH="$root/artifacts/nke-e2e-helm.deb" \
 bash nvke/controller/hack/capd-kindest-node/build-image.sh >logs/dependency-base.log 2>&1
cid=$(docker create "$base_image")
trap 'docker rm -f "$cid" >/dev/null 2>&1 || true' EXIT
for tool in nvke-agent spire-agent; do
    docker cp "$cid:/usr/local/bin/$tool" "artifacts/$tool"
    docker cp "$cid:/etc/systemd/system/$tool.service" "artifacts/$tool.service"
done
docker rm "$cid" >/dev/null
trap - EXIT
(cd artifacts && sha256sum nvke-kubeadm-distro-*_"${arch}".deb nvke-cni-ovn-*_"${arch}".deb \
  nvke-agent spire-agent nvke-agent.service spire-agent.service >SHA256SUMS)
if [[ $mode == --node-only ]]; then
    (cd artifacts && sha256sum --quiet -c SHA256SUMS)
    exit 0
fi
docker build -f nvke/controller/Dockerfile --build-arg BUILD_VERSION=2f31480b \
  -t vxanvil-public-nvke-controller:2f31480b nvke >logs/controller-image.log 2>&1
docker build -f nvke/controller/test/fixtures/Dockerfile -t docker.local/cwe-profiles:public-2f31480b \
  nvke/controller/test/fixtures >logs/profile-image.log 2>&1
bash provider/scripts/build-nvke-control-plane.sh vxanvil-public-node-base:2f31480b \
  "$root/artifacts" vxanvil-control-plane:public-1.36.3 "/boot/config-$(uname -r)" >logs/control-plane-image.log 2>&1
docker pull kindest/node:v1.36.4
VXANVIL_CONTROL_PLANE_IMAGE=vxanvil-control-plane:public-1.36.3 \
  bash provider/scripts/package-nvke-management.sh "$root/nvke" "$root/tools" kindest/node:v1.36.4 \
  vxanvil-public-nvke-controller:2f31480b docker.local/cwe-profiles:public-2f31480b "$root/management-bundle"
(cd management-bundle && sha256sum --quiet -c SHA256SUMS)
