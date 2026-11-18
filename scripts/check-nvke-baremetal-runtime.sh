#!/bin/bash
set -euo pipefail

test "$(id -u)" = 0
test "$(uname -m)" = x86_64
artifacts=${1:?pass the verified AMD64 artifact directory}
pkg=nvke-kubeadm-distro-1.36.3_1_amd64.deb
expected=$(awk -v pkg="$pkg" '$2==pkg || $2=="*"pkg {print $1}' "$artifacts/SHA256SUMS")
test "${#expected}" = 64
printf '%s  %s\n' "$expected" "$artifacts/$pkg" | sha256sum -c -

dir=$(mktemp -d /var/tmp/nvke-runtime-check.XXXXXX)
pid=
cleanup() {
    if [ -n "$pid" ]; then
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    fi
    rm -rf -- "$dir"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

docker_pid=$(systemctl show docker.service -p MainPID --value)
containerd_pid=$(systemctl show containerd.service -p MainPID --value)
test "$docker_pid" -gt 0
test "$containerd_pid" -gt 0

dpkg-deb --fsys-tarfile "$artifacts/$pkg" |
    tar -x -C "$dir" ./usr/bin/kubeadm ./usr/bin/kubelet ./usr/bin/crictl
"$dir/usr/bin/kubeadm" version -o short
"$dir/usr/bin/kubelet" --version

cat >"$dir/config.toml" <<'CONFIG'
version = 3
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc]
  runtime_type = "io.containerd.runc.v2"
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc.options]
  SystemdCgroup = true
CONFIG

/usr/bin/containerd --config "$dir/config.toml" --address "$dir/containerd.sock" \
    --root "$dir/root" --state "$dir/state" >"$dir/containerd.log" 2>&1 &
pid=$!

for attempt in $(seq 1 12); do
    if "$dir/usr/bin/crictl" --runtime-endpoint "unix://$dir/containerd.sock" \
        --timeout 2s info >"$dir/cri.json" 2>/dev/null &&
        jq -e '.status.conditions | any(.type == "RuntimeReady" and .status == true)' "$dir/cri.json" >/dev/null; then
        break
    fi
    sleep 1
done
if ! jq -e '.status.conditions | any(.type == "RuntimeReady" and .status == true)' "$dir/cri.json" >/dev/null; then
    tail -25 "$dir/containerd.log"
    exit 1
fi

shared_socket=/run/containerd/containerd.sock
shared_socket_inode=$(stat -Lc '%d:%i' "$shared_socket")
probe_socket_inode=$(stat -Lc '%d:%i' "$dir/containerd.sock")
test "$shared_socket_inode" != "$probe_socket_inode"
unshare --mount --propagation private bash -euo pipefail -c '
    mount --bind "$1/containerd.sock" "$2"
    test "$(stat -Lc "%d:%i" "$2")" = "$3"
    "$1/usr/bin/crictl" --runtime-endpoint "unix://$2" --timeout 2s info |
        jq -e '\''.status.conditions | any(.type == "RuntimeReady" and .status == true)'\'' >/dev/null
' bash "$dir" "$shared_socket" "$probe_socket_inode"
test "$(stat -Lc '%d:%i' "$shared_socket")" = "$shared_socket_inode"

test "$(systemctl show docker.service -p MainPID --value)" = "$docker_pid"
test "$(systemctl show containerd.service -p MainPID --value)" = "$containerd_pid"
printf '%s\n' 'Independent CRI RuntimeReady at the NVKE path inside a private mount namespace.'
printf '%s\n' 'The existing Docker and containerd processes and socket are unchanged.'
