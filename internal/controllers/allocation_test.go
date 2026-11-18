package controllers

import (
	"context"
	"errors"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	api "github.com/xdu31/cluster-api-provider-vmetal/api/v1alpha1"
	"github.com/xdu31/cluster-api-provider-vmetal/internal/discovery"
)

type probeFunc func(context.Context, string) (discovery.Hardware, error)

func (probe probeFunc) Discover(ctx context.Context, target string) (discovery.Hardware, error) {
	return probe(ctx, target)
}

var errInterrupted = errors.New("interrupted after writing the host claim")

type interruptedClient struct {
	client.Client
	beforeClaim func(context.Context) error
}

func (c interruptedClient) Status() client.SubResourceWriter {
	return interruptedStatus{SubResourceWriter: c.Client.Status(), beforeClaim: c.beforeClaim}
}

type interruptedStatus struct {
	client.SubResourceWriter
	beforeClaim func(context.Context) error
}

func (s interruptedStatus) Update(ctx context.Context, obj client.Object, opts ...client.SubResourceUpdateOption) error {
	switch obj := obj.(type) {
	case *api.HomelabHost:
		if obj.Status.Claim != nil {
			if err := s.beforeClaim(ctx); err != nil {
				return err
			}
		}
	case *api.HomelabMachine:
		if obj.Status.Phase == "Reserved" {
			return errInterrupted
		}
	}
	return s.SubResourceWriter.Update(ctx, obj, opts...)
}

func TestAllocationWithAPIServer(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("set KUBEBUILDER_ASSETS to run the Kubernetes API integration test")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	testEnvironment := &envtest.Environment{
		CRDDirectoryPaths:     []string{"../../config/crd"},
		ErrorIfCRDPathMissing: true,
	}
	cfg, err := testEnvironment.Start()
	must(t, err)
	t.Cleanup(func() { must(t, testEnvironment.Stop()) })

	scheme := runtime.NewScheme()
	must(t, corev1.AddToScheme(scheme))
	must(t, api.SchemeBuilder.AddToScheme(scheme))
	c, err := client.New(cfg, client.Options{Scheme: scheme})
	must(t, err)

	id := strings.Repeat("a", 64)
	host := &api.HomelabHost{
		ObjectMeta: metav1.ObjectMeta{Name: "host-" + id[:32]},
		Spec:       api.HomelabHostSpec{MachineID: id, SSHTarget: "ubuntu@worker", ManagedAddress: true},
	}
	alias := host.DeepCopy()
	alias.Name = "host-" + strings.Repeat("b", 32)
	if err := c.Create(ctx, alias); !apierrors.IsInvalid(err) {
		t.Fatalf("alternate name must be rejected for this identity, got %v", err)
	}
	must(t, c.Create(ctx, host))
	hostReq := request(host)
	observedID := strings.Repeat("b", 64)
	hostController := HostReconciler{Client: c, Discovery: probeFunc(func(context.Context, string) (discovery.Hardware, error) {
		return discovery.Hardware{ID: observedID, OS: "Ubuntu", LogicalCPUs: 4}, nil
	})}
	_, err = hostController.Reconcile(ctx, hostReq)
	must(t, err)

	machines := make([]*api.HomelabMachine, 0, 2)
	for _, namespace := range []string{"pool-a", "pool-b"} {
		must(t, c.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}}))
		machine := &api.HomelabMachine{
			ObjectMeta: metav1.ObjectMeta{Name: "worker", Namespace: namespace},
			Spec:       api.HomelabMachineSpec{HostRef: host.Name},
		}
		must(t, c.Create(ctx, machine))
		machines = append(machines, machine)
	}
	controller := MachineReconciler{Client: c}
	_, err = controller.Reconcile(ctx, request(machines[0]))
	must(t, err)
	must(t, c.Get(ctx, client.ObjectKeyFromObject(machines[0]), machines[0]))
	if machines[0].Status.Reason != "HostUnverified" {
		t.Fatalf("identity mismatch must prevent allocation, got %+v", machines[0].Status)
	}
	observedID = id
	_, err = hostController.Reconcile(ctx, hostReq)
	must(t, err)

	must(t, c.Get(ctx, client.ObjectKeyFromObject(host), host))
	host.Spec.SSHTarget = "ubuntu@10.0.0.8"
	host.Spec.NodeIP = "10.0.0.8"
	must(t, c.Update(ctx, host))
	_, err = controller.Reconcile(ctx, request(machines[0]))
	must(t, err)
	must(t, c.Get(ctx, client.ObjectKeyFromObject(machines[0]), machines[0]))
	if machines[0].Status.Reason != "HostUnverified" {
		t.Fatalf("an address change requires a new verified probe, got %+v", machines[0].Status)
	}
	_, err = hostController.Reconcile(ctx, hostReq)
	must(t, err)

	// Both reconciles reach the status write with the same resourceVersion.
	reached := make(chan struct{}, 2)
	proceed := make(chan struct{})
	racing := MachineReconciler{Client: interruptedClient{Client: c, beforeClaim: func(ctx context.Context) error {
		reached <- struct{}{}
		select {
		case <-proceed:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}}}
	var workers sync.WaitGroup
	results := make(chan error, 2)
	for _, machine := range machines {
		workers.Add(1)
		go func() {
			defer workers.Done()
			_, err := racing.Reconcile(ctx, request(machine))
			results <- err
		}()
	}
	for range machines {
		select {
		case <-reached:
		case <-ctx.Done():
			close(proceed)
			workers.Wait()
			t.Fatal("both callers did not attempt to claim the host")
		}
	}
	close(proceed)
	workers.Wait()
	first, second := <-results, <-results
	if !((errors.Is(first, errInterrupted) && apierrors.IsConflict(second)) ||
		(apierrors.IsConflict(first) && errors.Is(second, errInterrupted))) {
		t.Fatalf("want one persisted claim and one API conflict, got %v and %v", first, second)
	}
	must(t, c.Get(ctx, client.ObjectKeyFromObject(host), host))
	if host.Status.Claim == nil {
		t.Fatal("claim missing after interrupted reconciliation")
	}
	moved := host.DeepCopy()
	moved.Spec.NodeIP = "10.0.0.9"
	if err := c.Update(ctx, moved); !apierrors.IsInvalid(err) {
		t.Fatalf("changing a claimed host address must fail, got %v", err)
	}
	winner, loser := machines[0], machines[1]
	if host.Status.Claim.UID != winner.UID {
		winner, loser = loser, winner
	}
	if host.Status.Claim.UID != winner.UID || host.Status.Claim.Namespace != winner.Namespace {
		t.Fatalf("unexpected persisted owner: %+v", host.Status.Claim)
	}

	// Recreate the reconciler with a fresh API client and no in-memory state.
	freshClient, err := client.New(cfg, client.Options{Scheme: scheme})
	must(t, err)
	restarted := MachineReconciler{Client: freshClient}
	_, err = restarted.Reconcile(ctx, request(winner))
	must(t, err)
	_, err = restarted.Reconcile(ctx, request(loser))
	must(t, err)
	must(t, c.Get(ctx, client.ObjectKeyFromObject(winner), winner))
	must(t, c.Get(ctx, client.ObjectKeyFromObject(loser), loser))
	if winner.Status.Phase != "Reserved" || loser.Status.Reason != "HostClaimed" {
		t.Fatalf("reservation did not recover: winner %+v, loser %+v", winner.Status, loser.Status)
	}

	must(t, c.Delete(ctx, loser))
	_, err = restarted.Reconcile(ctx, request(loser))
	must(t, err)
	must(t, c.Get(ctx, client.ObjectKeyFromObject(host), host))
	if host.Status.Claim == nil || host.Status.Claim.UID != winner.UID {
		t.Fatal("deleting a nonowner changed the claim")
	}
	must(t, c.Delete(ctx, host))
	_, err = hostController.Reconcile(ctx, hostReq)
	must(t, err)
	must(t, c.Get(ctx, client.ObjectKeyFromObject(host), host))
	if host.DeletionTimestamp.IsZero() || host.Status.Claim == nil {
		t.Fatal("deleting a claimed host must retain its record")
	}
	must(t, c.Delete(ctx, winner))
	_, err = restarted.Reconcile(ctx, request(winner))
	must(t, err)
	_, err = hostController.Reconcile(ctx, hostReq)
	must(t, err)
	if err := c.Get(ctx, client.ObjectKeyFromObject(host), host); !apierrors.IsNotFound(err) {
		t.Fatalf("host remained after releasing its final claim: %v", err)
	}
	if err := c.Get(ctx, client.ObjectKeyFromObject(winner), winner); !apierrors.IsNotFound(err) {
		t.Fatalf("machine remained after release: %v", err)
	}
}

func request(obj client.Object) ctrl.Request {
	return ctrl.Request{NamespacedName: client.ObjectKeyFromObject(obj)}
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}
