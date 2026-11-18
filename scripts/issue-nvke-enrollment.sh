#!/bin/bash
set -euo pipefail
umask 077
if [[ $# != 2 || $EUID != 0 ]]; then
    echo "Usage: sudo bash $0 management-state-dir output-directory" >&2
    exit 2
fi
state=$(realpath "$1")
output=$(realpath -m "$2")
mapfile -t site <"$state/site"
[[ ${#site[@]} == 3 ]] && test -f "$state/ready" && test -d "$state/worker-bundle" ||
    { echo "Management initialization has not completed." >&2; exit 1; }
parent=$(dirname "$output")
test -d "$parent"
[[ ! -e $output ]] || { echo "Invitation output already exists." >&2; exit 1; }
staging=$(mktemp -d "$parent/.nvke-invitation.XXXXXX")
trap 'rm -rf -- "$staging"' EXIT
cp -a "$state/worker-bundle/." "$staging/"
python3 "$state/nvke-enrollment-server.py" --state "$state/registry" invite >"$staging/invitation"
cat >"$staging/enroll.sh" <<EOF
#!/bin/bash
set -euo pipefail
[[ \$# == 1 ]] || { echo "Usage: sudo bash \$0 verified-worker-artifact-dir" >&2; exit 2; }
bundle=\$(dirname "\$(realpath "\$0")")
exec bash "\$bundle/enroll-nvke-vm-worker.sh" https://${site[1]}:18443 "\$1" "\$bundle" - ${site[1]}
EOF
chmod 0755 "$staging/enroll.sh"
mv -T -- "$staging" "$output"
printf 'Transfer the private directory %s to the VM. Run: sudo bash %s/enroll.sh /path/to/verified-worker-artifacts\n' "$output" "$output"
