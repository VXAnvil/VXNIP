#!/bin/bash
set -euo pipefail
umask 077

if [[ $# != 5 || $EUID != 0 ]]; then
    echo "Usage: sudo bash $0 bundle-dir state-dir private-ip enrollment-cidr kind-name" >&2
    exit 2
fi
bundle=$(realpath "$1")
state=$(realpath -m "$2")
address=$3
cidr=$4
site=$5
repo=$(dirname "$(dirname "$(realpath "$0")")")
[[ $site =~ ^[a-z][a-z0-9-]{0,31}$ && $site != *- ]]
python3 - "$address" "$cidr" <<'PY'
from ipaddress import IPv4Address, IPv4Network
import sys
ip = IPv4Address(sys.argv[1])
if not ip.is_private or ip.is_loopback or ip.is_unspecified or ip not in IPv4Network(sys.argv[2]):
    sys.exit("Use a private address inside the enrollment network.")
PY
ip -o -4 addr show | awk -v ip="$address" '{split($4,a,"/"); if (a[1]==ip) found=1} END {exit !found}'
install -d -m 0700 "$state"
exec 9>"$state/init.lock"
flock -n 9 || { echo "Initialization is already running." >&2; exit 1; }

site_config=$(printf '%s\n' "$site" "$address" "$cidr")
if [[ -e $state/site ]]; then
    [[ $(cat "$state/site") == "$site_config" ]] || { echo "State directory belongs to another site." >&2; exit 1; }
else
    printf '%s\n' "$site_config" >"$state/site"
fi

(cd "$bundle" && sha256sum --quiet -c SHA256SUMS)
for tool in kind kubectl clusterctl helm nvkectl homelab-controller; do
    [[ -x $bundle/bin/$tool ]]
done
[[ $(cat "$bundle/nvke/revision") == 2f31480b360cbd7fc9d9db7a6ba274a8d0fd5049 ]]
mapfile -t images <"$bundle/images"
[[ ${#images[@]} == 3 ]]
node_image=${images[0]}
controller_image=${images[1]}
profile_image=${images[2]}
for image in "${images[@]}"; do
    [[ $image =~ ^[a-zA-Z0-9./_-]+:[a-zA-Z0-9._-]+$ ]]
done
[[ $profile_image == docker.local/cwe-profiles:* ]]

if ! command -v docker >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y docker.io
fi
systemctl enable --now docker.service
export PATH="$bundle/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export KUBECONFIG="$state/management.kubeconfig"
export KIND_CLUSTER_NAME="$site" MANAGEMENT_VIP="$address" NODE_AGENT_SPIRE_ATTESTATION_METHOD=joinToken

if [[ ! -e $state/creates-kind ]]; then
    if kind get clusters | grep -Fxq "$site"; then
        echo "A Kind cluster with this name already exists." >&2
        exit 1
    fi
    touch "$state/creates-kind"
fi
if ! kind get clusters | grep -Fxq "$site"; then
    docker load -i "$bundle/images.tar"
    cat >"$state/kind.yaml" <<YAML
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
  extraPortMappings:
  - containerPort: 30441
    hostPort: 30441
    listenAddress: "$address"
  - containerPort: 30443
    hostPort: 30443
    listenAddress: "$address"
YAML
    kind create cluster --name "$site" --config "$state/kind.yaml" --image "$node_image" --kubeconfig "$KUBECONFIG" --wait 120s
fi
if [[ ! -s $KUBECONFIG ]]; then
    kind export kubeconfig --name "$site" --kubeconfig "$KUBECONFIG"
fi
[[ $(kubectl config current-context) == "kind-$site" ]]
for image in "$controller_image" "$profile_image"; do
    if ! docker image inspect "$image" >/dev/null 2>&1; then
        docker load -i "$bundle/images.tar"
    fi
done
kind load docker-image "$controller_image" "$profile_image" --name "$site"
kubectl wait --for=condition=Ready "node/$site-control-plane" --timeout=120s
clusterctl init --kubeconfig "$KUBECONFIG" --core cluster-api:v1.13.6 \
    --bootstrap kubeadm:v1.13.6 --control-plane kubeadm:v1.13.6

if [[ ! -e $state/db-password ]]; then
    openssl rand -hex 24 >"$state/db-password"
fi
export DGXC_TENANT_USER=homelab
DGXC_TENANT_PASSWORD=$(cat "$state/db-password")
export DGXC_TENANT_PASSWORD
export CNPG_SUPERUSER_PASSWORD="$DGXC_TENANT_PASSWORD"
export CNPG_CLOUD_PASSWORD="$DGXC_TENANT_PASSWORD" CNPG_TEMPORAL_PASSWORD="$DGXC_TENANT_PASSWORD"
export NODE_REBOOT_SOFT_REBOOT_ENABLED=false
export STACK_DIR="$bundle/nvke"
export DEPLOY_PATHS_DIR="$state/deploy"
export NVKE_CONTROLLER_RELEASES_DIR="$STACK_DIR/controller/test/fixtures/releases"
mkdir -p "$DEPLOY_PATHS_DIR"
cp -a "$STACK_DIR/controller/deploy/." "$DEPLOY_PATHS_DIR/"
overlay="$DEPLOY_PATHS_DIR/capd-e2e"
sed -i '/name: capd-reboot-controller,/d' "$overlay/nvke-deploy.yaml"
sed -i 's/nodePort: 30440/nodePort: 30441/' "$overlay/spire-server-nodeport/spire-server-nodeport.yaml"
sed -i 's/serverPort: "30440"/serverPort: "30441"/; s/installAfterInit: false/installAfterInit: true/; s/^    tag: tilt$/    tag: '"${profile_image##*:}"'/' "$overlay/controller/overlay.yaml"

if [[ ! -f $state/nvke-ready ]]; then
    bash "$STACK_DIR/controller/hack/ci/deploy-stack.sh" "$controller_image"
    kubectl -n nvke-controller rollout status deployment/nvke-controller --timeout=240s
    bash "$STACK_DIR/controller/hack/apply-capi-spiffe-exec-auth.sh" --kubeconfig "$KUBECONFIG"
    touch "$state/nvke-ready"
fi
kubectl apply -f "$repo/config/crd"
kubectl apply -f "$repo/config/crd/nvke"
kubectl apply -f "$repo/config/rbac/capi.yaml"
kubectl apply -f "$repo/config/rbac/nvke.yaml"
kubectl -n nvke-controller rollout status deployment/nvke-controller --timeout=180s

install -d -m 0700 "$state/worker-bundle" "$state/registry"
if [[ ! -s $state/ssh-key ]]; then
    ssh-keygen -q -t ed25519 -N '' -f "$state/ssh-key"
fi
if [[ ! -s $state/server.key || ! -s $state/ca.crt ]]; then
    openssl req -x509 -newkey rsa:3072 -nodes -days 90 -subj "/CN=$site-enrollment" \
        -addext "subjectAltName=IP:$address" -keyout "$state/server.key" -out "$state/ca.crt"
fi
touch "$state/known-hosts"
install -m 0644 "$state/ssh-key.pub" "$state/worker-bundle/provider.pub"
install -m 0644 "$state/ca.crt" "$state/worker-bundle/ca.crt"
for file in enroll-nvke-vm-worker.sh prepare-nvke-vm-worker.sh configure-nvke-lab-host-recovery.sh homelab-kubeadm; do
    install -m 0755 "$repo/scripts/$file" "$state/worker-bundle/$file"
done
install -m 0644 "$repo/scripts/nvke-enrollment-server.py" "$state/nvke-enrollment-server.py"
install -d -m 0700 "$state/bin"
ln -sfn "$bundle/bin/kubectl" "$state/bin/kubectl"
cat >"$state/bin/ssh" <<EOF
#!/bin/sh
exec /usr/bin/ssh -F /dev/null -i "$state/ssh-key" -o IdentitiesOnly=yes -o UserKnownHostsFile="$state/known-hosts" -o GlobalKnownHostsFile=/dev/null "\$@"
EOF
chmod 0755 "$state/bin/ssh"

cat >"/etc/systemd/system/homelab-nvke-$site-provider.service" <<UNIT
[Unit]
Description=Homelab Provider for $site
After=docker.service
Requires=docker.service
[Service]
Environment="PATH=$state/bin:/usr/local/bin:/usr/bin:/bin"
Environment="KUBECONFIG=$KUBECONFIG"
Environment=HOMELAB_ENABLE_NVKE_INFRA=1
ExecStart=$bundle/bin/homelab-controller
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
cat >"/etc/systemd/system/homelab-nvke-$site-enrollment.service" <<UNIT
[Unit]
Description=Homelab enrollment for $site
After=network-online.target docker.service
Wants=network-online.target
[Service]
Environment="PATH=$state/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/usr/bin/python3 $state/nvke-enrollment-server.py --state $state/registry serve --bind $address --network $cidr --cert $state/ca.crt --key $state/server.key --kubeconfig $KUBECONFIG --ssh-key $state/ssh-key --known-hosts $state/known-hosts
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
cat >"/etc/systemd/system/homelab-nvke-$site-api.service" <<UNIT
[Unit]
Description=Local NVKE API for $site
After=docker.service
Requires=docker.service
[Service]
ExecStart=$bundle/bin/kubectl --kubeconfig $KUBECONFIG -n nvke-controller port-forward --address 127.0.0.1 service/nvke-controller 18081:8080
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
docker update --restart=unless-stopped "$site-control-plane" >/dev/null
systemctl daemon-reload
systemctl enable --now "homelab-nvke-$site-provider" "homelab-nvke-$site-enrollment" "homelab-nvke-$site-api"
wait_port() {
    local ip=$1 port=$2
    for attempt in $(seq 1 12); do
        if timeout 2 bash -c 'exec 3<>"/dev/tcp/$1/$2"' _ "$ip" "$port" 2>/dev/null; then return; fi
        sleep 2
    done
    echo "Service unavailable at $ip:$port" >&2
    return 1
}
for port in 30441 30443 18443; do
    wait_port "$address" "$port"
done
wait_port 127.0.0.1 18081
export XDG_CONFIG_HOME="$state/nvkectl-config" XDG_CACHE_HOME="$state/nvkectl-cache"
nvkectl config set-site --name "$site" --url http://127.0.0.1:18081 --kubeconfig "$KUBECONFIG"
nvkectl --site "$site" get clusters
touch "$state/ready"
printf 'Management kubeconfig: %s\n' "$KUBECONFIG"
printf 'Issue a worker invitation: sudo bash %s/scripts/issue-nvke-enrollment.sh %s /path/to/worker-bundle\n' "$repo" "$state"
