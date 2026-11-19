#!/bin/bash
set -euo pipefail
umask 077
[[ $# == 3 || $# == 4 ]] || {
    echo "Usage: $0 dependency-base-image verified-artifact-dir output-image [host-kernel-config]" >&2
    exit 2
}
base_id=$(docker image inspect "$1" --format '{{.Id}}')
base="vmetal-build-base:${base_id#sha256:}"
docker image tag "$1" "$base"
[[ $(docker image inspect "$base" --format '{{.Id}}') == "$base_id" ]]
artifacts=$(realpath "$2")
image=$3
repo=$(cd "$(dirname "$0")/.." && pwd)
arch=$(dpkg --print-architecture)
[[ $arch == amd64 || $arch == arm64 ]]
[[ $image =~ ^[a-zA-Z0-9./_-]+:[a-zA-Z0-9._-]+$ ]]
context=$(mktemp -d)
trap 'rm -rf -- "$context"' EXIT
mkdir -p "$context/root" "$context/scripts"
packages=(nvke-kubeadm-distro-1.36.3_1_"$arch".deb
    nvke-cni-ovn-charts-2.4.2_2.4.2_"$arch".deb
    nvke-cni-ovn-images-2.4.2_2.4.2_"$arch".deb)
for file in "${packages[@]}" nvke-agent spire-agent nvke-agent.service spire-agent.service; do
    (cd "$artifacts"
     awk -v f="$file" '$2==f || $2=="*"f {print; count++} END {exit count!=1}' SHA256SUMS | sha256sum -c -)
done
for package in "${packages[@]}"; do
    [[ $(dpkg-deb -f "$artifacts/$package" Architecture) == "$arch" ]]
    dpkg-deb --extract "$artifacts/$package" "$context/root"
done
# Debian node images use merged /usr. Normalize archive directories before COPY
# so package /lib contents cannot replace the base image's /lib symlink.
python3 - "$context/root" <<'PY_MERGE'
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1])
for name in ("bin", "sbin", "lib", "lib64"):
    source = root / name
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, root / "usr" / name, dirs_exist_ok=True, symlinks=True)
        shutil.rmtree(source)
PY_MERGE
# Explicit directory modes also cover directories created under the private umask.
find "$context/root" -type d -exec chmod 0755 {} +
for tool in nvke-agent spire-agent; do
    install -D -m 0755 "$artifacts/$tool" "$context/root/usr/local/bin/$tool"
    install -D -m 0644 "$artifacts/$tool.service" "$context/root/etc/systemd/system/$tool.service"
done
kernel_config=${4:-/boot/config-$(uname -r)}
[[ -f $kernel_config ]] || { echo "Supply the running host kernel config." >&2; exit 1; }
target_kernel=${VMETAL_TARGET_KERNEL:-$(uname -r)}
if [[ $(basename "$kernel_config") == config-* ]]; then
    target_kernel=$(basename "$kernel_config")
    target_kernel=${target_kernel#config-}
fi
[[ $target_kernel =~ ^[a-zA-Z0-9.+_-]+$ ]]
install -D -m 0644 "$kernel_config" "$context/root/boot/config-$target_kernel"
install -m 0755 "$repo/scripts/vmetal-kubeadm" "$context/scripts/"
# The dependency image may be built online once. All node versions come from
# the verified artifacts and the resulting image is shipped inside the bundle.
{
    printf 'ARG NVKE_NODE_IMAGE=vmetal-build-base:required\nFROM ${NVKE_NODE_IMAGE} AS assets\nCOPY root/ /\n'
    sed '1,2d' "$repo/examples/nvke-docker-control-plane.Dockerfile"
} >"$context/Dockerfile"
# The agent validates charts through dpkg-query, so extracting files alone is
# insufficient. This package has no dependencies or maintainer scripts.
cp "$artifacts/${packages[1]}" "$context/ovn-charts.deb"
sed -i '/^COPY root\//a COPY ovn-charts.deb /tmp/ovn-charts.deb\nRUN dpkg -i /tmp/ovn-charts.deb && rm /tmp/ovn-charts.deb' "$context/Dockerfile"
helper=$(sha256sum "$repo/scripts/vmetal-kubeadm" | cut -d' ' -f1)
manifest=$(sha256sum "$artifacts/SHA256SUMS" | cut -d' ' -f1)
docker build --build-arg "NVKE_NODE_IMAGE=$base" \
    --label vmetal.nvke.dev/kubernetes=v1.36.3 \
    --label "vmetal.nvke.dev/kernel=$target_kernel" \
    --label "vmetal.nvke.dev/helper-sha256=$helper" \
    --label "vmetal.nvke.dev/artifacts-sha256=$manifest" -t "$image" "$context"
docker run --rm --network none --entrypoint /usr/bin/kubeadm "$image" version -o short | grep -Fx v1.36.3
docker run --rm --network none --user vmetal --entrypoint /usr/bin/python3 "$image" -c 'import os; assert os.geteuid()!=0'
docker run --rm --network none --entrypoint /usr/bin/dpkg-query "$image" \
    -W -f='${Version}\n' nvke-cni-ovn-charts-2.4.2 | grep -Fx 2.4.2
docker run --rm --network none --entrypoint /usr/bin/python3 "$image" -c '
from cloudinit import stages
init = stages.Init()
init.read_cfg()
cfg = init.cfg
assert cfg["network"]["config"] == "disabled"
assert cfg["ssh_deletekeys"] is False
assert all(cfg[key] == [] for key in ("cloud_init_modules", "cloud_config_modules", "cloud_final_modules"))'
printf 'Control-plane image %s %s\n' "$image" "$(docker image inspect "$image" -f '{{.Id}}')"
