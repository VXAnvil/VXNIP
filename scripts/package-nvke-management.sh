#!/bin/bash
set -euo pipefail

if [[ $# != 6 ]]; then
    echo "Usage: bash $0 nvke-checkout tool-bin-dir kind-image controller-image profile-image output-dir" >&2
    exit 2
fi
nvke=$(realpath "$1")
tools=$(realpath "$2")
images=("$3" "$4" "$5")
output=$(realpath -m "$6")
revision=$(git -C "$nvke" rev-parse HEAD)
[[ $revision == 2f31480b360cbd7fc9d9db7a6ba274a8d0fd5049 ]] ||
    { echo "Use the pinned NVKE checkout: 2f31480b360cbd7fc9d9db7a6ba274a8d0fd5049" >&2; exit 1; }
[[ ! -e $output ]] || { echo "Bundle output already exists." >&2; exit 1; }
staging=$(mktemp -d "$(dirname "$output")/.nvke-bundle.XXXXXX")
trap 'rm -rf -- "$staging"' EXIT
mkdir "$staging/bin" "$staging/nvke"
for tool in kind kubectl clusterctl helm nvkectl vmetal-controller; do
    install -m 0755 "$tools/$tool" "$staging/bin/$tool"
done
git -C "$nvke" archive "$revision" deploy controller/deploy controller/hack/ci/deploy-stack.sh \
    controller/hack/apply-capi-spiffe-exec-auth.sh controller/test/fixtures/releases |
    tar -x -C "$staging/nvke"
printf '%s\n' "$revision" >"$staging/nvke/revision"
printf '%s\n' "${images[@]}" >"$staging/images"
docker save --output "$staging/images.tar" "${images[@]}"
(
    cd "$staging"
    find bin nvke -type f -print0 | sort -z | xargs -0 sha256sum >SHA256SUMS
    sha256sum images images.tar >>SHA256SUMS
)
mv -T -- "$staging" "$output"
printf 'Management bundle: %s\n' "$output"
