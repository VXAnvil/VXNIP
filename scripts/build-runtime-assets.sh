#!/bin/bash
# Publisher/CI entrypoint. Never called by the default skill installation path.
set -euo pipefail
[[ $EUID == 0 && $# == 3 ]] || { echo 'Usage: sudo build-runtime-assets.sh fresh-workspace output runtime-tag' >&2; exit 2; }
repo=$(cd "$(dirname "$0")/.." && pwd)
[[ ! -e $1 ]] || { echo 'Use a fresh publisher workspace; do not replay an uncertain build.' >&2; exit 1; }
export VMETAL_PORTABLE_KERNEL_CONFIG=1
bash "$repo/skills/vmetal-cluster/scripts/public-inputs.sh" "$1"
python3 "$repo/scripts/package-runtime-assets.py" "$1" "$2" --arch "$(dpkg --print-architecture)" --tag "$3"
