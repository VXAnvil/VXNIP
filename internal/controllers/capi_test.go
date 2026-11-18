package controllers

import (
	"context"
	"errors"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	api "github.com/xdu31/cluster-api-provider-vmetal/api/v1alpha1"
	"github.com/xdu31/cluster-api-provider-vmetal/internal/bootstrap"
)

type kubeadmFunc func(context.Context, string, bootstrap.Request) error

func (fn kubeadmFunc) Run(ctx context.Context, target string, request bootstrap.Request) error {
	return fn(ctx, target, request)
}

func TestCABPKBootstrapAndCleanup(t *testing.T) {
	ctx := context.Background()
	scheme := runtime.NewScheme()
	must(t, corev1.AddToScheme(scheme))
	must(t, api.SchemeBuilder.AddToScheme(scheme))
	cluster := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": capiVersion, "kind": "Cluster",
		"metadata": map[string]any{"name": "lab", "namespace": "default"},
	}}
	capiMachine := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": capiVersion, "kind": "Machine",
		"metadata": map[string]any{"name": "worker", "namespace": "default", "uid": "capi-worker"},
		"spec": map[string]any{
			"clusterName": "lab",
			"infrastructureRef": map[string]any{
				"apiGroup": api.GroupVersion.Group,
				"kind":     "HomelabMachine", "name": "worker",
			},
		},
	}}
	host := &api.HomelabHost{
		ObjectMeta: metav1.ObjectMeta{Name: "host-" + strings.Repeat("a", 32), Finalizers: []string{hostFinalizer}},
		Spec:       api.HomelabHostSpec{MachineID: strings.Repeat("a", 64), SSHTarget: "ubuntu@worker", AllowBootstrap: true},
		Status: api.HomelabHostStatus{
			DiscoveryReady: true, ObservedAt: &metav1.Time{Time: metav1.Now().Time},
		},
	}
	machine := &api.HomelabMachine{
		ObjectMeta: metav1.ObjectMeta{
			Name: "worker", Namespace: "default", UID: types.UID("infra-worker"),
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: capiVersion, Kind: "Machine", Name: "worker", UID: capiMachine.GetUID(),
			}},
		},
		Spec: api.HomelabMachineSpec{HostRef: host.Name},
	}
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name: "bootstrap", Namespace: "default",
			Labels: map[string]string{"cluster.x-k8s.io/cluster-name": "lab"},
		},
		Type: "cluster.x-k8s.io/secret",
		Data: map[string][]byte{"format": []byte("cloud-config"), "value": []byte(`## template: jinja
#cloud-config
write_files:
- path: /run/kubeadm/kubeadm-join-config.yaml
  owner: root:root
  permissions: '0640'
  content: |
    apiVersion: kubeadm.k8s.io/v1beta4
    kind: JoinConfiguration
runcmd:
- kubeadm join --config /run/kubeadm/kubeadm-join-config.yaml && echo success > /run/cluster-api/bootstrap-success.complete
`)},
	}
	c := fake.NewClientBuilder().WithScheme(scheme).
		WithStatusSubresource(host, machine).WithObjects(cluster, capiMachine, host, machine, secret).Build()
	req := request(machine)
	hostKey := client.ObjectKeyFromObject(host)
	backendFailure := errors.New("SSH temporarily unavailable")
	var actions []string
	resetReady := false
	controller := MachineReconciler{Client: c, Bootstrap: kubeadmFunc(func(ctx context.Context, target string, op bootstrap.Request) error {
		actions = append(actions, op.Action)
		if target != host.Spec.SSHTarget || op.MachineID != host.Spec.MachineID || op.OwnerUID != string(machine.UID) {
			t.Fatalf("incorrect remote identity: %s %+v", target, op)
		}
		switch op.Action {
		case "apply":
			var stored api.HomelabMachine
			must(t, c.Get(ctx, req.NamespacedName, &stored))
			if !stored.Status.BootstrapStarted || !strings.Contains(op.Plan.Config, "homelab://infra-worker") {
				t.Fatal("bootstrap started without persisting the intent or setting the kubelet identity")
			}
		case "reset":
			if !resetReady {
				return backendFailure
			}
		default:
			t.Fatalf("unexpected operation %q", op.Action)
		}
		return nil
	})}

	_, err := controller.Reconcile(ctx, req)
	must(t, err)
	must(t, c.Get(ctx, req.NamespacedName, machine))
	if machine.Status.Reason != "WaitingForBootstrap" || len(actions) != 0 {
		t.Fatalf("did not wait for CABPK: status %+v actions %v", machine.Status, actions)
	}
	must(t, unstructured.SetNestedField(capiMachine.Object, secret.Name, "spec", "bootstrap", "dataSecretName"))
	must(t, c.Update(ctx, capiMachine))
	_, err = controller.Reconcile(ctx, req)
	must(t, err)
	must(t, c.Get(ctx, req.NamespacedName, machine))
	if machine.Spec.ProviderID != "homelab://infra-worker" ||
		machine.Status.Initialization == nil || !machine.Status.Initialization.Provisioned {
		t.Fatalf("bootstrap completion was not published: %+v %+v", machine.Spec, machine.Status)
	}
	_, err = controller.Reconcile(ctx, req)
	must(t, err)
	if len(actions) != 1 {
		t.Fatalf("published bootstrap was repeated: %v", actions)
	}

	must(t, c.Delete(ctx, machine))
	if _, err := controller.Reconcile(ctx, req); !errors.Is(err, backendFailure) {
		t.Fatalf("expected failed reset, got %v", err)
	}
	must(t, c.Get(ctx, hostKey, host))
	if host.Status.Claim == nil {
		t.Fatal("failed reset released the host")
	}
	resetReady = true
	_, err = controller.Reconcile(ctx, req)
	must(t, err)
	must(t, c.Get(ctx, hostKey, host))
	if host.Status.Claim != nil {
		t.Fatal("successful reset did not release the host")
	}
	if err := c.Get(ctx, req.NamespacedName, machine); !apierrors.IsNotFound(err) {
		t.Fatalf("machine was not finalized: %v", err)
	}
}
