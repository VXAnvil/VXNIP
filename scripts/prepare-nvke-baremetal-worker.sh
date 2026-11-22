#!/bin/bash
set -euo pipefail

usage() {
    echo "Usage: sudo bash $0 --check|--apply /absolute/path/to/verified-host-artifacts ssh-user [/absolute/worker-storage-dir]" >&2
    exit 2
}

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

[[ ( $# == 3 || $# == 4 ) && ( $1 == --check || $1 == --apply ) && $2 == /* ]] || usage
mode=$1
nvidia_runtime=${NVKE_NVIDIA_RUNTIME:-}
artifacts=$2
ssh_user=$3
storage_dir=${4:-}
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ $ssh_user =~ ^[a-z_][a-z0-9_-]*$ ]] || usage
[[ $EUID == 0 ]] || fail "Run this procedure as root."
arch=$(dpkg --print-architecture)
case "$(uname -m):$arch" in
    x86_64:amd64|aarch64:arm64) ;;
    *) fail "Use a native Ubuntu AMD64 or ARM64 host." ;;
esac
id "$ssh_user" >/dev/null || fail "SSH user does not exist: $ssh_user"
. /etc/os-release
[[ $ID == ubuntu && ( $VERSION_ID == 22.04 || $VERSION_ID == 24.04 ) ]] ||
    fail "This lab procedure targets Ubuntu 22.04 and 24.04."
for tool in python3 ctr visudo ss; do
    command -v "$tool" >/dev/null || fail "Missing host tool: $tool"
done
if [[ -n $nvidia_runtime ]]; then
    [[ $nvidia_runtime == /usr/bin/nvidia-container-runtime && -x $nvidia_runtime ]] ||
        fail "Install NVIDIA Container Toolkit before preparing a GPU worker."
fi
[[ $(/usr/bin/containerd --version) =~ [[:space:]]v?2\.[0-9] ]] ||
    fail "This procedure expects host containerd 2.x."

shared_socket=/run/containerd/containerd.sock
worker_socket=/run/vxanvil-nvke-containerd/containerd.sock
[[ -S $shared_socket ]] || fail "The Quickstart containerd socket is absent."
docker_pid=$(systemctl show docker.service -p MainPID --value)
shared_pid=$(systemctl show containerd.service -p MainPID --value)
[[ $docker_pid -gt 0 && $shared_pid -gt 0 ]] || fail "The existing Docker and containerd must be running."
shared_inode=$(stat -Lc '%d:%i' "$shared_socket")
default_route=$(ip -4 route show default)
[[ -n $default_route ]] || fail "No IPv4 default route."
[[ ! -e /etc/kubernetes/kubelet.conf && ! -e /etc/kubernetes/bootstrap-kubelet.conf ]] ||
    fail "Kubelet is already configured. Leave this node running, or release its allocation first."
kubelet_listener=$(ss -H -ltn 'sport = :10250')
[[ -z $kubelet_listener ]] ||
    fail "TCP 10250 is in use by an existing kubelet or service. Use a dedicated worker."
[[ ! -e /var/lib/vxanvil-kubeadm/current.json ]] || fail "The bootstrap helper already owns this node."
[[ -z $(find /etc/cni/net.d -maxdepth 1 -type f ! -name .kubernetes-cni-keep -print -quit 2>/dev/null || true) ]] ||
    fail "An existing CNI configuration is present."

runtime_root=/var/lib/vxanvil-nvke-containerd
if [[ -n $storage_dir ]]; then
    [[ $storage_dir =~ ^/[a-zA-Z0-9_./-]+$ && $(realpath -m -- "$storage_dir") == "$storage_dir" ]] ||
        fail "Use a canonical absolute storage directory without symlinks or spaces."
    ancestor=$storage_dir
    while [[ $ancestor != / ]]; do
        if [[ -e $ancestor ]]; then
            [[ -d $ancestor ]] || fail "Storage path is not a directory: $ancestor"
            read -r owner permissions < <(stat -c '%u %a' -- "$ancestor")
            [[ $owner == 0 ]] && (( (8#$permissions & 0022) == 0 )) ||
                fail "Storage directories must be owned by root and writable only by root: $ancestor"
        fi
        ancestor=$(dirname -- "$ancestor")
    done
    runtime_root=$storage_dir/containerd
    if [[ -e $runtime_root || -L $runtime_root ]]; then
        [[ -d $runtime_root && ! -L $runtime_root && $(stat -c %u -- "$runtime_root") == 0 ]] &&
            grep -Fq -- "--root $runtime_root --state " /etc/systemd/system/vxanvil-nvke-containerd.service ||
            fail "The storage directory already contains an unrelated containerd root: $runtime_root"
    fi
fi

k8s=nvke-kubeadm-distro-1.36.3_1_${arch}.deb
ovn=nvke-cni-ovn-images-2.4.2_2.4.2_${arch}.deb
k8s_matrix=usr/lib/nvke/k8s-matrix/v1.36.3/$arch
cd "$artifacts"
for file in "$k8s" "$ovn" nvke-agent spire-agent nvke-agent.service spire-agent.service; do
    awk -v file="$file" '$2 == file || $2 == "*"file {print; count++} END {exit count != 1}' SHA256SUMS |
        sha256sum -c -
done
for package in "$k8s" "$ovn"; do
    [[ $(dpkg-deb -f "$package" Architecture) == "$arch" ]] ||
        fail "The architecture of $package does not match $arch."
done
packages=(openvswitch-switch conntrack socat ethtool)
if ! command -v cloud-init >/dev/null; then
    packages+=(cloud-init)
fi
apt_plan=$(apt-get -s --no-remove install "${packages[@]}")
if grep -Eq '^Remv |^Inst [^ ]+ \[' <<<"$apt_plan"; then
    printf '%s\n' "$apt_plan" >&2
    fail "The dependency transaction would remove or upgrade an installed package."
fi
grep -E '^(Inst|Conf) ' <<<"$apt_plan" || true
printf 'Independent containerd root: %s\n' "$runtime_root"
printf 'Worker artifact architecture: %s\n' "$arch"
printf 'Available memory and swap before preparation:\n'
free -h
swapon --show
[[ $mode == --apply ]] || exit 0

if [[ -n $storage_dir ]]; then
    if [[ ! -d $storage_dir ]]; then
        install -d -o root -g root -m 0700 "$storage_dir"
    fi
    dir=$(mktemp -d -- "$storage_dir/.prepare.XXXXXXXX")
else
    dir=$(mktemp -d)
fi
trap 'rm -rf -- "$dir"' EXIT

if ! command -v cloud-init >/dev/null && [[ ! -e /etc/cloud/cloud-init.disabled ]]; then
    # The separate NoCloud preparation step supplies a seed with networking disabled.
    install -D -o root -g root -m 0644 /dev/null /etc/cloud/cloud-init.disabled
fi
export DEBIAN_FRONTEND=noninteractive
apt-get -y --no-remove install "${packages[@]}"
dpkg-deb --fsys-tarfile "$k8s" | tar -x -C "$dir" \
    ./usr/bin/kubeadm ./usr/bin/kubelet ./usr/bin/crictl ./opt/cni/bin/loopback \
    ./lib/systemd/system/kubelet.service ./lib/systemd/system/kubelet.service.d/10-kubeadm.conf \
    "./$k8s_matrix"
dpkg-deb --fsys-tarfile "$ovn" | tar -x -C "$dir" ./usr/lib/nvke/cni-ovn/2.4.2

install_checked() {
    local permissions=$1 source=$2 destination=$3
    if [[ -e $destination || -L $destination ]]; then
        [[ ! -L $destination ]] && cmp -s "$source" "$destination" ||
            fail "Refusing to replace an existing file: $destination"
    else
        install -D -o root -g root -m "$permissions" "$source" "$destination"
    fi
}

for tool in kubeadm kubelet crictl; do
    install_checked 0755 "$dir/usr/bin/$tool" "/usr/bin/$tool"
done
install_checked 0755 "$dir/opt/cni/bin/loopback" /opt/cni/bin/loopback
install_checked 0755 nvke-agent /usr/local/bin/nvke-agent
install_checked 0755 spire-agent /usr/local/bin/spire-agent
getent group spire >/dev/null || groupadd --system spire
id spire >/dev/null 2>&1 || useradd --system -g spire -d /var/lib/spire-agent -s /usr/sbin/nologin spire
install -d -o spire -g spire -m 0750 /var/lib/spire-agent
install_checked 0644 nvke-agent.service /etc/systemd/system/nvke-agent.service
install_checked 0644 spire-agent.service /etc/systemd/system/spire-agent.service
alias=/etc/systemd/system/nke-agent.service
if [[ -e $alias || -L $alias ]]; then
    [[ $(readlink "$alias") == nvke-agent.service ]] || fail "Unexpected agent unit alias: $alias"
else
    ln -s nvke-agent.service "$alias"
fi
install_checked 0644 "$dir/lib/systemd/system/kubelet.service" /etc/systemd/system/kubelet.service
install_checked 0644 "$dir/lib/systemd/system/kubelet.service.d/10-kubeadm.conf" /etc/systemd/system/kubelet.service.d/10-kubeadm.conf
install_checked 0755 "$repo/scripts/vxanvil-kubeadm" /usr/local/sbin/vxanvil-kubeadm
printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/vxanvil-kubeadm ""\n' "$ssh_user" >"$dir/sudoers"
visudo -cf "$dir/sudoers"
install_checked 0440 "$dir/sudoers" /etc/sudoers.d/vxanvil-nvke-worker

cat >"$dir/containerd.toml" <<'CONFIG'
version = 3
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc]
  runtime_type = "io.containerd.runc.v2"
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc.options]
  SystemdCgroup = true
CONFIG
if [[ -n $nvidia_runtime ]]; then
    cat >>"$dir/containerd.toml" <<'CONFIG'
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.nvidia]
  runtime_type = "io.containerd.runc.v2"
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.nvidia.options]
  BinaryName = "/usr/bin/nvidia-container-runtime"
  SystemdCgroup = true
CONFIG
fi
install_checked 0644 "$dir/containerd.toml" /etc/vxanvil-nvke/containerd.toml
cat >"$dir/containerd.service" <<'UNIT'
[Unit]
Description=Container runtime for the NVKE VXAnvil worker
After=network-online.target
UNIT
if [[ -n $storage_dir ]]; then
    printf 'RequiresMountsFor=%s\n' "$storage_dir" >>"$dir/containerd.service"
fi
cat >>"$dir/containerd.service" <<UNIT

[Service]
RuntimeDirectory=vxanvil-nvke-containerd
ExecStart=/usr/bin/containerd --config /etc/vxanvil-nvke/containerd.toml --address /run/vxanvil-nvke-containerd/containerd.sock --root $runtime_root --state /run/vxanvil-nvke-containerd/state
Delegate=yes
KillMode=process
Restart=on-failure
UNIT
install_checked 0644 "$dir/containerd.service" /etc/systemd/system/vxanvil-nvke-containerd.service
cat >"$dir/agent.conf" <<'UNIT'
[Unit]
Requires=vxanvil-nvke-containerd.service
After=vxanvil-nvke-containerd.service

[Service]
BindPaths=/run/vxanvil-nvke-containerd/containerd.sock:/run/containerd/containerd.sock
Environment=NKE_OVS_DATAPATH_TYPE=system
UNIT
install_checked 0644 "$dir/agent.conf" /etc/systemd/system/nvke-agent.service.d/80-vxanvil-runtime.conf
cat >"$dir/kubelet.conf" <<'UNIT'
[Unit]
Requires=vxanvil-nvke-containerd.service
After=vxanvil-nvke-containerd.service

[Service]
ExecStart=
ExecStart=/usr/bin/kubelet $KUBELET_KUBECONFIG_ARGS $KUBELET_CONFIG_ARGS $KUBELET_KUBEADM_ARGS $KUBELET_EXTRA_ARGS --fail-swap-on=false --node-labels=vxanvil.nvke.dev/gateway=local --register-with-taints=vxanvil.nvke.dev/experiment=true:NoSchedule --container-runtime-endpoint=unix:///run/vxanvil-nvke-containerd/containerd.sock
UNIT
install_checked 0644 "$dir/kubelet.conf" /etc/systemd/system/kubelet.service.d/99-vxanvil-worker.conf
systemctl daemon-reload
systemctl start vxanvil-nvke-containerd.service
for attempt in {1..12}; do
    if crictl --config=/dev/null --runtime-endpoint "unix://$worker_socket" --timeout 2s ps -q; then break; fi
    sleep 1
done
crictl --config=/dev/null --runtime-endpoint "unix://$worker_socket" --timeout 5s ps -q
for archive in "$dir/$k8s_matrix"/*.tgz "$dir"/usr/lib/nvke/cni-ovn/2.4.2/*.tgz; do
    gzip -dc "$archive" | ctr --address "$worker_socket" --namespace k8s.io images import --all-platforms -
done
[[ $(systemctl show docker.service -p MainPID --value) == "$docker_pid" &&
   $(systemctl show containerd.service -p MainPID --value) == "$shared_pid" &&
   $(stat -Lc '%d:%i' "$shared_socket") == "$shared_inode" &&
   $(ip -4 route show default) == "$default_route" ]] ||
    fail "A shared runtime or the default route changed during preparation."
printf 'Worker tools and independent CRI ready. Complete the cloud-init and OVN checks before creating a Machine.\n'
