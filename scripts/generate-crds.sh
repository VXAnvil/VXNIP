#!/bin/sh
set -eu
cd "$(dirname "$0")/.."

go run sigs.k8s.io/controller-tools/cmd/controller-gen@v0.19.0 object crd \
  paths=./api/... output:crd:dir=config/crd
mkdir -p config/crd/nvke
for kind in vxanvilclusters vxanvilmachines vxanvilmachinetemplates; do
  sed 's/vxanvil\.infrastructure\.cluster\.x-k8s\.io/infrastructure.cluster.x-k8s.io/g' \
    "config/crd/vxanvil.infrastructure.cluster.x-k8s.io_$kind.yaml" \
    > "config/crd/nvke/infrastructure.cluster.x-k8s.io_$kind.yaml"
done
