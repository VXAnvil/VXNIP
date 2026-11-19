#!/bin/bash
set -euo pipefail
test "$(id -u)" = 0

root=${1:-}
bin="$root/usr/bin"
sbin="$root/usr/sbin"
vswitchd="$sbin/ovs-vswitchd"
if [ -n "$root" ]; then
    export LD_LIBRARY_PATH="$root/usr/lib/x86_64-linux-gnu"
    vswitchd="$root/usr/lib/openvswitch-switch/ovs-vswitchd"
fi
test -x "$vswitchd"

dir=$(mktemp -d /var/tmp/nvke-ovs-check.XXXXXX)
trap 'rm -rf -- "$dir"' EXIT
default_route=$(ip -4 route show default)
unshare --net bash -euo pipefail -s -- "$root" "$bin" "$sbin" "$vswitchd" "$dir" <<'PROBE'
root=$1
bin=$2
sbin=$3
vswitchd=$4
dir=$5
export OVS_RUNDIR="$dir"
db_pid=
switch_pid=
cleanup() {
    for pid in "$switch_pid" "$db_pid"; do
        if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; fi
    done
}
trap cleanup EXIT
trap 'exit 130' INT TERM
"$bin/ovsdb-tool" create "$dir/conf.db" "$root/usr/share/openvswitch/vswitch.ovsschema"
"$sbin/ovsdb-server" "$dir/conf.db" --remote="punix:$dir/db.sock" --unixctl="$dir/db.ctl" >"$dir/db.log" 2>&1 &
db_pid=$!
for attempt in $(seq 1 5); do
    if [ -S "$dir/db.sock" ]; then break; fi
    sleep 1
done
if [ ! -S "$dir/db.sock" ]; then
    cat "$dir/db.log"
    exit 1
fi
"$bin/ovs-vsctl" --db="unix:$dir/db.sock" --timeout=8 --no-wait init
"$vswitchd" "unix:$dir/db.sock" --unixctl="$dir/switch.ctl" >"$dir/switch.log" 2>&1 &
switch_pid=$!
if ! "$bin/ovs-vsctl" --db="unix:$dir/db.sock" --timeout=12 add-br br-ex -- set bridge br-ex datapath_type=system; then
    cat "$dir/db.log" "$dir/switch.log"
    exit 1
fi
ip link set br-ex up
test -z "$("$bin/ovs-vsctl" --db="unix:$dir/db.sock" list-ports br-ex)"
ip -br link show br-ex
PROBE
test "$(ip -4 route show default)" = "$default_route"
printf '%s\n' 'OVS kernel bridge started without a physical uplink; host default route unchanged.'
