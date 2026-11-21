#!/bin/bash
# Runtime prerequisites only. No compiler, Go toolchain, source checkout or image build.
set -euo pipefail
[[ $EUID == 0 && $# == 1 && $1 == manager ]]
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
packages=(ca-certificates python3 python3-yaml openssh-client jq iproute2 iptables
  openssl conntrack socat ethtool kmod)
if command -v docker >/dev/null; then
    # A dedicated manager may already have a healthy Docker CE installation.
    # Do not mix its containerd.io with Ubuntu's conflicting docker.io package.
    docker info >/dev/null
else
    packages+=(docker.io)
fi
apt-get install -y --no-install-recommends "${packages[@]}"
systemctl enable --now docker.service
docker info >/dev/null
modprobe openvswitch
test -r "/boot/config-$(uname -r)"
