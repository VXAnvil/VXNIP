#!/bin/bash
set -euo pipefail

if [[ $# != 3 || $EUID != 0 ]]; then
    echo "Usage: sudo bash $0 artifact-dir ssh-user provider-public-key" >&2
    exit 2
fi

artifacts=$(realpath "$1")
script_dir=$(dirname "$(realpath "$0")")
ssh_user=$2
public_key=$(realpath "$3")
home=$(getent passwd "$ssh_user" | cut -d: -f6)
group=$(id -gn "$ssh_user")
[[ $ssh_user =~ ^[a-z_][a-z0-9_-]*$ && $home == /home/* ]]
if ! systemd-detect-virt --vm --quiet || systemd-detect-virt --container --quiet; then
    echo "Use this script inside a dedicated VM." >&2
    exit 1
fi
[[ ! -e /var/lib/vmetal-kubeadm/current.json && ! -e /etc/kubernetes/kubelet.conf ]] ||
    { echo "The node is already claimed or joined." >&2; exit 1; }
if [[ -n $(swapon --noheadings --show) ]]; then
    echo "Disable swap on the dedicated VM before preparing it." >&2
    exit 1
fi
cloud_init_status=0
cloud-init status --wait || cloud_init_status=$?
if (( cloud_init_status == 2 )); then
    # Lima can generate deprecated cloud-config keys on an otherwise healthy VM.
    status_json=$(cloud-init status --format json || [[ $? == 2 ]])
    python3 -c '
import json, sys

status = json.load(sys.stdin)
warnings = status.get("recoverable_errors", {})
if (status.get("status") != "done" or status.get("errors") != []
        or set(warnings) != {"DEPRECATED"}):
    sys.exit("cloud-init reported errors beyond deprecation warnings")
' <<<"$status_json"
elif (( cloud_init_status != 0 )); then
    exit "$cloud_init_status"
fi
key=$(cat "$public_key")
[[ $key != *$'\n'* && $key =~ ^(ssh-|ecdsa-|sk-) ]]
ssh-keygen -l -f "$public_key" >/dev/null

arch=$(dpkg --print-architecture)
cd "$artifacts"
packages=(nvke-kubeadm-distro-1.36.3_1_"$arch".deb
    nvke-cni-ovn-charts-2.4.2_2.4.2_"$arch".deb nvke-cni-ovn-images-2.4.2_2.4.2_"$arch".deb)
for file in "${packages[@]}" nvke-agent spire-agent nvke-agent.service spire-agent.service; do
    awk -v file="$file" '$2 == file { print; found=1 } END { if (!found) exit 1 }' SHA256SUMS |
        sha256sum -c -
done

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y openssh-server openvswitch-switch "${packages[@]/#/./}"

getent group spire >/dev/null || groupadd --system spire
id spire >/dev/null 2>&1 || useradd --system --gid spire --home-dir /var/lib/spire-agent spire
install -d -o spire -g spire -m 0750 /var/lib/spire-agent
install -m 0755 nvke-agent spire-agent /usr/local/bin/
install -m 0644 nvke-agent.service spire-agent.service /etc/systemd/system/
ln -sfn nvke-agent.service /etc/systemd/system/nke-agent.service
install -o root -g root -m 0755 "$script_dir/vmetal-kubeadm" /usr/local/sbin/vmetal-kubeadm

install -d -o "$ssh_user" -g "$group" -m 0700 "$home/.ssh"
keys="$home/.ssh/authorized_keys"
touch "$keys"
grep -qxF "restrict $key" "$keys" || printf '%s\n' "restrict $key" >>"$keys"
chown "$ssh_user:$group" "$keys"
chmod 0600 "$keys"
sudoers=$(mktemp)
trap 'rm -f "$sudoers"' EXIT
printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/vmetal-kubeadm ""\n' "$ssh_user" >"$sudoers"
visudo -cf "$sudoers"
install -m 0440 "$sudoers" /etc/sudoers.d/vmetal-kubeadm
systemctl daemon-reload
systemctl enable --now ssh.service containerd.service openvswitch-switch.service
kubeadm version -o short
[[ -x /opt/cni/bin/loopback ]] || { echo "Missing CNI loopback binary." >&2; exit 1; }
crictl --runtime-endpoint unix:///run/containerd/containerd.sock --timeout 5s info |
    python3 -c '
import json, sys
conditions = json.load(sys.stdin)["status"]["conditions"]
if not any(c["type"] == "RuntimeReady" and c["status"] is True for c in conditions):
    sys.exit("CRI RuntimeReady is false")
print("CRI RuntimeReady: /run/containerd/containerd.sock")
'
