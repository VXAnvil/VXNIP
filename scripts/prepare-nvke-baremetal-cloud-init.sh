#!/bin/bash
set -euo pipefail
test "$#" = 0
test "$EUID" = 0

status=$(cloud-init status --long 2>/dev/null || true)
if grep -Fxq 'status: done' <<<"$status"; then
    printf 'cloud-init already reports done.\n'
    exit 0
fi

marker=/etc/cloud/cloud-init.disabled
backup=/var/lib/cloud/vxnip-nvke-cloud-init.disabled
seed=/var/lib/cloud/seed/nocloud
config=/etc/cloud/cloud.cfg.d/zz-vxnip-nvke-lab.cfg
metadata='instance-id: vxnip-nvke-baremetal'
if [[ ! -f $marker && ! -f $backup ]]; then
    printf 'Expected the Ubuntu installer cloud-init disable marker. Inspect cloud-init status before continuing.\n' >&2
    exit 1
fi
if [[ -e $seed ]] && ! grep -Fxq "$metadata" "$seed/meta-data"; then
    printf 'Refusing to replace the existing NoCloud seed.\n' >&2
    exit 1
fi
if [[ -f $marker && -e $backup ]]; then
    printf 'Both the cloud-init marker and its VXNIP backup exist.\n' >&2
    exit 1
fi
dir=$(mktemp -d)
trap 'rm -rf -- "$dir"' EXIT
cat >"$dir/cloud.cfg" <<'CLOUD'
datasource_list: [NoCloud]
network:
  config: disabled
preserve_hostname: true
ssh_deletekeys: false
cloud_init_modules: []
cloud_config_modules: []
cloud_final_modules: []
CLOUD
if [[ -e $config ]] && ! cmp -s "$dir/cloud.cfg" "$config"; then
    printf 'Refusing to replace the existing VXNIP cloud-init configuration.\n' >&2
    exit 1
fi
route=$(ip -4 route show default)
install -d -m 0755 "$seed"
printf '%s\n' "$metadata" >"$seed/meta-data"
printf '%s\n' '#cloud-config' 'manage_etc_hosts: false' >"$seed/user-data"
install -m 0644 "$dir/cloud.cfg" "$config"
if [[ -f $marker ]]; then mv -- "$marker" "$backup"; fi
cloud-init clean
/usr/lib/cloud-init/ds-identify --force
systemctl daemon-reload
cloud-init init --local
cloud-init init
cloud-init modules --mode=config
cloud-init modules --mode=final
cloud-init status | grep -Fx 'status: done'
test "$(ip -4 route show default)" = "$route"
