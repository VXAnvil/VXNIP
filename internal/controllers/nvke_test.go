package controllers

import (
	"context"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	api "github.com/xdu31/cluster-api-provider-vmetal/api/v1alpha1"
	"github.com/xdu31/cluster-api-provider-vmetal/internal/bootstrap"
)

func TestNVKEHandoffWithTwoHosts(t *testing.T) {
	ctx := context.Background()
	scheme := runtime.NewScheme()
	must(t, corev1.AddToScheme(scheme))
	must(t, api.SchemeBuilder.AddToScheme(scheme))
	api.AddInfrastructureToScheme(scheme)
	cluster := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": capiVersion, "kind": "Cluster",
		"metadata": map[string]any{"name": "lab", "namespace": "nvke-system"},
	}}
	nvkeMachine := &api.VMetalMachine{}
	nvkeMachine.SetGroupVersionKind(api.InfrastructureGroupVersion.WithKind("VMetalMachine"))
	c := fake.NewClientBuilder().WithScheme(scheme).
		WithStatusSubresource(&api.VMetalHost{}, nvkeMachine).WithObjects(cluster).Build()
	for index, tc := range []struct{ name, ip string }{
		{"control-plane", "10.0.0.53"}, {"worker", "10.0.0.54"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			id := strings.Repeat(string(rune('a'+index)), 64)
			now := metav1.Now()
			host := &api.VMetalHost{
				ObjectMeta: metav1.ObjectMeta{Name: "host-" + id[:32], Finalizers: []string{hostFinalizer}},
				Spec: api.VMetalHostSpec{
					MachineID: id, NodeIP: tc.ip, SSHTarget: "ubuntu@" + tc.ip, AllowBootstrap: true,
				},
				Status: api.VMetalHostStatus{
					DiscoveryReady: true, ObservedAt: &now,
					Hardware: &apiextensionsv1.JSON{Raw: []byte(`{"interfaces":[{"addresses":["` + tc.ip + `/24"]}]}`)},
				},
			}
			must(t, c.Create(ctx, host))
			parent := &unstructured.Unstructured{Object: map[string]any{
				"apiVersion": capiVersion, "kind": "Machine",
				"metadata": map[string]any{"namespace": "nvke-system", "name": tc.name, "uid": "capi-" + tc.name},
				"spec": map[string]any{
					"clusterName": "lab",
					"infrastructureRef": map[string]any{
						"apiGroup": api.InfrastructureGroupVersion.Group,
						"kind":     "VMetalMachine", "name": tc.name,
					},
				},
			}}
			must(t, c.Create(ctx, parent))
			machine := &api.VMetalMachine{
				TypeMeta: metav1.TypeMeta{APIVersion: api.InfrastructureGroupVersion.String(), Kind: "VMetalMachine"},
				ObjectMeta: metav1.ObjectMeta{
					Namespace: "nvke-system", Name: tc.name, UID: types.UID("infra-" + tc.name),
					OwnerReferences: []metav1.OwnerReference{{
						APIVersion: capiVersion, Kind: "Machine", Name: tc.name, UID: parent.GetUID(),
					}},
				},
				Spec: api.VMetalMachineSpec{HostRef: host.Name, BootstrapMode: "NVKE"},
			}
			must(t, c.Create(ctx, machine))
			var operations []string
			providerID := "vmetal://" + string(machine.UID)
			r := MachineReconciler{Client: c, InfrastructureGroup: api.InfrastructureGroupVersion,
				Bootstrap: kubeadmFunc(func(_ context.Context, target string, req bootstrap.Request) error {
					operations = append(operations, req.Action)
					if target != host.Spec.SSHTarget || req.MachineID != id {
						t.Fatalf("unexpected bootstrap target %s", target)
					}
					if req.Action == "apply-nvke" && (req.Plan != nil || req.Agent == nil ||
						req.Agent.NodeIP != tc.ip || req.Agent.ProviderID != providerID) {
						t.Fatalf("unexpected NVKE apply request: %+v", req)
					}
					return nil
				}),
			}
			machineKey := client.ObjectKeyFromObject(machine)
			machineGVK := api.InfrastructureGroupVersion.WithKind("VMetalMachine")
			getMachine := func() {
				t.Helper()
				machine.SetGroupVersionKind(machineGVK)
				must(t, c.Get(ctx, machineKey, machine))
				machine.SetGroupVersionKind(machineGVK)
			}
			_, err := r.Reconcile(ctx, request(machine))
			must(t, err)
			getMachine()
			if len(machine.Status.Addresses) != 1 || machine.Status.Addresses[0].Address != tc.ip ||
				machine.Status.Initialization != nil || len(operations) != 0 {
				t.Fatalf("unexpected address handoff before NVKE rendered bootstrap: %+v", machine.Status)
			}

			secret := &corev1.Secret{
				ObjectMeta: metav1.ObjectMeta{
					Namespace: machine.Namespace, Name: tc.name + "-bootstrap-data",
					Labels: map[string]string{"cluster.x-k8s.io/cluster-name": "lab"},
				},
				Type: corev1.SecretTypeOpaque,
				Data: map[string][]byte{"value": []byte(`#cloud-config
write_files:
- path: /etc/default/nke-agent
  owner: root:root
  permissions: "0644"
  content: |
    AGENT_ARGS="--secure-server-port 15005"
runcmd:
- systemctl restart nke-agent.service
`)},
			}
			must(t, c.Create(ctx, secret))
			must(t, unstructured.SetNestedField(parent.Object, secret.Name, "spec", "bootstrap", "dataSecretName"))
			must(t, c.Update(ctx, parent))
			_, err = r.Reconcile(ctx, request(machine))
			must(t, err)
			getMachine()
			if machine.Spec.ProviderID != providerID || machine.Status.Initialization == nil ||
				!machine.Status.Initialization.Provisioned {
				t.Fatalf("NVKE handoff did not publish infrastructure readiness: %+v", machine.Status)
			}

			must(t, c.Delete(ctx, machine))
			_, err = r.Reconcile(ctx, request(machine))
			must(t, err)
			must(t, c.Get(ctx, client.ObjectKeyFromObject(host), host))
			if host.Status.Claim != nil || strings.Join(operations, ",") != "apply-nvke,reset-nvke" {
				t.Fatalf("NVKE cleanup did not release the host: %v", operations)
			}
		})
	}
}
