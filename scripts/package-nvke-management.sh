#!/bin/bash
set -euo pipefail

if [[ $# != 6 ]]; then
    echo "Usage: bash $0 nvke-checkout tool-bin-dir kind-image controller-image profile-image output-dir" >&2
    exit 2
fi
nvke=$(realpath "$1")
tools=$(realpath "$2")
images=("$3" "$4" "$5")
if [[ -n ${VMETAL_CONTROL_PLANE_IMAGE:-} ]]; then
    images+=("$VMETAL_CONTROL_PLANE_IMAGE")
fi
output=$(realpath -m "$6")
revision=$(git -C "$nvke" rev-parse HEAD)
[[ $revision == 2f31480b360cbd7fc9d9db7a6ba274a8d0fd5049 ]] ||
    { echo "Use the pinned NVKE checkout: 2f31480b360cbd7fc9d9db7a6ba274a8d0fd5049" >&2; exit 1; }
[[ ! -e $output ]] || { echo "Bundle output already exists." >&2; exit 1; }
staging=$(mktemp -d "$(dirname "$output")/.nvke-bundle.XXXXXX")
trap 'rm -rf -- "$staging"' EXIT
mkdir "$staging/bin" "$staging/nvke" "$staging/provider"
repo=$(cd "$(dirname "$0")/.." && pwd)
# An explicit file list keeps tests, source code and optional lab tools out of
# the runtime bundle. Preserve the existing paths and executable permissions.
manifest="$repo/config/management-bundle-files.txt"
while IFS= read -r file || [[ -n $file ]]; do
    [[ -z $file || $file == \#* ]] && continue
    [[ $file != /* && "/$file/" != *"/../"* && -f $repo/$file ]] || {
        echo "Invalid or missing bundle file: $file" >&2
        exit 1
    }
    (cd "$repo" && cp --parents -p -- "$file" "$staging/provider/")
done <"$manifest"
if [[ -n ${VMETAL_CONTROL_PLANE_IMAGE:-} ]]; then
    expected=$(sha256sum "$repo/scripts/vmetal-kubeadm" | cut -d' ' -f1)
    [[ $(docker image inspect "$VMETAL_CONTROL_PLANE_IMAGE" -f '{{index .Config.Labels "vmetal.nvke.dev/helper-sha256"}}') == "$expected" ]]
    [[ $(docker image inspect "$VMETAL_CONTROL_PLANE_IMAGE" -f '{{index .Config.Labels "vmetal.nvke.dev/kubernetes"}}') == v1.36.3 ]]
    printf '%s\n' "$VMETAL_CONTROL_PLANE_IMAGE" >"$staging/control-plane-image"
fi
for tool in kind kubectl clusterctl helm nvkectl vmetal-controller; do
    install -m 0755 "$tools/$tool" "$staging/bin/$tool"
done
git -C "$nvke" archive "$revision" deploy controller/deploy controller/hack/ci/deploy-stack.sh \
    controller/hack/apply-capi-spiffe-exec-auth.sh controller/test/fixtures/releases |
    tar -x -C "$staging/nvke"
printf '%s\n' "$revision" >"$staging/nvke/revision"
printf '%s\n' "${images[@]}" >"$staging/images"
for image in "${images[@]}"; do
    docker image inspect "$image" --format '{{.Id}}'
done >"$staging/image-ids"
docker save --output "$staging/images.tar" "${images[@]}"
(
    cd "$staging"
    find bin nvke provider -type f -print0 | sort -z | xargs -0 sha256sum >SHA256SUMS
    sha256sum images image-ids images.tar >>SHA256SUMS
    if [[ -f control-plane-image ]]; then sha256sum control-plane-image >>SHA256SUMS; fi
)
mv -T -- "$staging" "$output"
printf 'Management bundle: %s\n' "$output"
