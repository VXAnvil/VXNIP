#!/bin/bash
# Runtime prerequisites only. No compiler, Go toolchain, source checkout or image build.
set -euo pipefail
[[ $EUID == 0 && $# == 1 && $1 == manager ]]
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends ca-certificates docker.io python3 python3-yaml \
  openssh-client jq iproute2 iptables openssl conntrack socat ethtool kmod
systemctl enable --now docker.service
docker info >/dev/null
modprobe openvswitch
test -r "/boot/config-$(uname -r)"
