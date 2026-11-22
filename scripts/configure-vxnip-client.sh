#!/bin/bash
set -euo pipefail
umask 077
[[ $# == 2 || $# == 3 ]] || {
    echo "Usage: sudo bash $0 management-state client-directory [workload-kubeconfig]" >&2
    exit 2
}
state=$(realpath "$1")
output=$(realpath -m "$2")
[[ $output =~ ^/[a-zA-Z0-9_./-]+$ ]] || { echo "Use a client path without whitespace." >&2; exit 1; }
mapfile -t site <"$state/site"
[[ ${#site[@]} == 3 && -f $state/ready ]]
if [[ -n ${VXNIP_MANAGEMENT_BUNDLE:-} ]]; then
    bundle=$(realpath "$VXNIP_MANAGEMENT_BUNDLE")
else
    bundle=$(cat "$state/bundle")
fi
[[ -x $bundle/bin/nvkectl && -x $bundle/bin/kubectl ]]
install -d -m 0700 "$output" "$output/bin"
exec 9>"$output/configure.lock"
flock -n 9
if [[ -f $output/site ]]; then
    cmp -s "$state/site" "$output/site" || { echo "Client directory belongs to another site." >&2; exit 1; }
fi
install -m 0600 "$state/site" "$output/site"
# Exporting a client intentionally grants the management kubeconfig's privileges.
install -m 0600 "$state/management.kubeconfig" "$output/management.kubeconfig.next"
mv -f "$output/management.kubeconfig.next" "$output/management.kubeconfig"
for tool in nvkectl kubectl; do
    install -m 0755 "$bundle/bin/$tool" "$output/bin/$tool"
done
if [[ $# == 3 ]]; then
    install -m 0600 "$3" "$output/workload.kubeconfig.next"
    mv -f "$output/workload.kubeconfig.next" "$output/workload.kubeconfig"
fi
XDG_CONFIG_HOME="$output/config" XDG_CACHE_HOME="$output/cache" \
    "$output/bin/nvkectl" config set-site --name "${site[0]}" \
      --url http://127.0.0.1:18081 --kubeconfig "$output/management.kubeconfig"
{
    printf '# Source this file in Bash or Zsh.\n'
    printf 'export PATH=%q:"$PATH"\n' "$output/bin"
    printf 'export XDG_CONFIG_HOME=%q XDG_CACHE_HOME=%q NVKE_SITE=%q\n' "$output/config" "$output/cache" "${site[0]}"
    printf 'export KUBECONFIG=%q\n' "$output/management.kubeconfig"
} >"$output/env.sh.next"
chmod 0600 "$output/env.sh.next"
mv -f "$output/env.sh.next" "$output/env.sh"
if [[ $EUID == 0 && -n ${SUDO_UID:-} && $SUDO_UID != 0 ]]; then
    chown -R "$SUDO_UID:$SUDO_GID" "$output"
fi
printf '\nLoad this site in any new terminal:\nsource %q\n' "$output/env.sh"
printf 'Commands:\nnvkectl --site "$NVKE_SITE" get clusters\n'
printf 'kubectl --kubeconfig %q get vxniphosts\n' "$output/management.kubeconfig"
if [[ -f $output/workload.kubeconfig ]]; then
    printf 'kubectl --kubeconfig %q get nodes\n' "$output/workload.kubeconfig"
fi
