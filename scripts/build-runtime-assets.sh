#!/bin/bash
# Publisher/CI entrypoint. Never called by the default skill installation path.
set -euo pipefail
[[ $EUID == 0 && $# == 3 ]] || { echo 'Usage: sudo build-runtime-assets.sh fresh-workspace output runtime-tag' >&2; exit 2; }
repo=$(cd "$(dirname "$0")/.." && pwd)
[[ ! -e $1 ]] || { echo 'Use a fresh publisher workspace; do not replay an uncertain build.' >&2; exit 1; }
# A publisher builds its exact checked-out revision. Consumer pins are updated
# only after these native outputs have been published and verified.
[[ -z $(git -C "$repo" status --porcelain --untracked-files=no) ]] || { echo 'Publisher checkout is dirty.' >&2; exit 1; }
VXANVIL_PROVIDER_REVISION=$(git -C "$repo" rev-parse HEAD)
export VXANVIL_PROVIDER_REVISION
export VXANVIL_PORTABLE_KERNEL_CONFIG=1 VXANVIL_USE_EXISTING_BUILD_DOCKER=1
bash "$repo/skills/vxanvil-cluster/scripts/public-inputs.sh" "$1"
python3 "$repo/scripts/package-runtime-assets.py" "$1" "$2" --arch "$(dpkg --print-architecture)" --tag "$3"

# These outputs contain only public runtime files; the unprivileged CI uploader must read them.
chmod 0755 "$2"
chmod 0644 "$2"/*
