package controllers

import (
	"context"
	"errors"
	"strings"
	"testing"

	api "github.com/vmetal-lab/cluster-api-provider-vmetal/api/v1alpha1"
	"github.com/vmetal-lab/cluster-api-provider-vmetal/internal/bootstrap"
	"github.com/vmetal-lab/cluster-api-provider-vmetal/internal/discovery"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

type failedProbe struct{}

func (failedProbe) Discover(context.Context, string) (discovery.Hardware, error) {
	return discovery.Hardware{}, errors.New("Permission denied (publickey). private-operator-detail")
}

func TestHostDiscoveryPublishesSanitizedCondition(t *testing.T) {
	scheme := runtime.NewScheme()
	must(t, api.SchemeBuilder.AddToScheme(scheme))
	host := &api.VMetalHost{
		ObjectMeta: metav1.ObjectMeta{Name: "host-" + strings.Repeat("a", 32)},
		Spec:       api.VMetalHostSpec{MachineID: strings.Repeat("a", 64), SSHTarget: "ubuntu@worker"},
	}
	c := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(host).WithObjects(host).Build()
	r := HostReconciler{Client: c, Discovery: failedProbe{}}
	_, err := r.Reconcile(context.Background(), request(host))
	must(t, err)
	must(t, c.Get(context.Background(), client.ObjectKeyFromObject(host), host))
	if host.Status.DiscoveryReason != "SSHAuthenticationFailed" || len(host.Status.Conditions) != 1 ||
		host.Status.Conditions[0].Status != metav1.ConditionFalse ||
		strings.Contains(host.Status.DiscoveryMessage, "private-operator-detail") {
		t.Fatalf("unexpected diagnostic status: %+v", host.Status)
	}
}

func TestFailedResetRetainsClaimAndReportsOperation(t *testing.T) {
	ctx := context.Background()
	scheme := runtime.NewScheme()
	must(t, api.SchemeBuilder.AddToScheme(scheme))
	machine := &api.VMetalMachine{
		ObjectMeta: metav1.ObjectMeta{Name: "worker", Namespace: "default", UID: "allocation",
			Finalizers: []string{machineFinalizer}},
		Spec:   api.VMetalMachineSpec{HostRef: "host-" + strings.Repeat("b", 32), BootstrapMode: "NVKE"},
		Status: api.VMetalMachineStatus{Phase: "Provisioned", BootstrapStarted: true},
	}
	host := &api.VMetalHost{
		ObjectMeta: metav1.ObjectMeta{Name: machine.Spec.HostRef, Finalizers: []string{hostFinalizer}},
		Spec:       api.VMetalHostSpec{MachineID: strings.Repeat("b", 64), SSHTarget: "ubuntu@worker"},
		Status:     api.VMetalHostStatus{Claim: &api.HostClaim{Namespace: machine.Namespace, Name: machine.Name, UID: machine.UID}},
	}
	c := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(host, machine).WithObjects(host, machine).Build()
	must(t, c.Delete(ctx, machine))
	r := MachineReconciler{Client: c, Bootstrap: kubeadmFunc(func(context.Context, string, bootstrap.Request) error {
		return errors.New("remote reset failed")
	})}
	_, err := r.Reconcile(ctx, request(machine))
	if err == nil {
		t.Fatal("reset failure was hidden")
	}
	must(t, c.Get(ctx, client.ObjectKeyFromObject(host), host))
	must(t, c.Get(ctx, client.ObjectKeyFromObject(machine), machine))
	if host.Status.Claim == nil || !machine.Status.BootstrapStarted || machine.Status.Reason != "ResetFailed" ||
		!strings.Contains(machine.Status.Message, "reset-nvke") || len(machine.Finalizers) == 0 {
		t.Fatalf("failed reset released ownership or lost diagnostic: %+v", machine.Status)
	}
}
