package controllers

import (
	"context"
	"fmt"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime/schema"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	api "github.com/xdu31/cluster-api-provider-vmetal/api/v1alpha1"
	"github.com/xdu31/cluster-api-provider-vmetal/internal/bootstrap"
)

const (
	machineFinalizer = "vmetal.infrastructure.cluster.x-k8s.io/claim"
	allocationPoll   = 10 * time.Second
)

type MachineReconciler struct {
	client.Client
	Bootstrap           bootstrap.Runner
	InfrastructureGroup schema.GroupVersion
}

func (r *MachineReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var machine api.VMetalMachine
	gvk := infraGroup(r.InfrastructureGroup).WithKind("VMetalMachine")
	machine.SetGroupVersionKind(gvk)
	if err := r.Get(ctx, req.NamespacedName, &machine); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	machine.SetGroupVersionKind(gvk)
	if isPaused(&machine) && machine.DeletionTimestamp.IsZero() {
		return ctrl.Result{}, nil
	}
	if !machine.DeletionTimestamp.IsZero() && !controllerutil.ContainsFinalizer(&machine, machineFinalizer) {
		return ctrl.Result{}, nil
	}
	if machine.DeletionTimestamp.IsZero() && controllerutil.AddFinalizer(&machine, machineFinalizer) {
		if err := r.updateMachine(ctx, &machine, false); err != nil {
			return ctrl.Result{}, err
		}
	}

	var host api.VMetalHost
	err := r.Get(ctx, client.ObjectKey{Name: machine.Spec.HostRef}, &host)
	if err != nil && !apierrors.IsNotFound(err) {
		return ctrl.Result{}, err
	}
	claim := api.HostClaim{Namespace: machine.Namespace, Name: machine.Name, UID: machine.UID}
	ownsClaim := host.Status.Claim != nil && *host.Status.Claim == claim
	if !machine.DeletionTimestamp.IsZero() {
		if machine.Status.BootstrapStarted {
			if !ownsClaim {
				return ctrl.Result{}, fmt.Errorf("cannot reset a host without its original claim")
			}
			action := "reset"
			if machine.Spec.BootstrapMode == "NVKE" {
				action = "reset-nvke"
			}
			if err := r.runBootstrap(ctx, &machine, &host, bootstrap.Request{Action: action}); err != nil {
				return ctrl.Result{}, err
			}
			machine.Status.BootstrapStarted = false
			if err := r.updateMachine(ctx, &machine, true); err != nil {
				return ctrl.Result{}, err
			}
		}
		if ownsClaim {
			host.Status.Claim = nil
			if err := r.Status().Update(ctx, &host); err != nil {
				return ctrl.Result{}, err
			}
		}
		controllerutil.RemoveFinalizer(&machine, machineFinalizer)
		return ctrl.Result{}, r.updateMachine(ctx, &machine, false)
	}

	switch {
	case ownsClaim:
		return r.reconcileBootstrap(ctx, &machine, &host)
	case machine.Status.BootstrapStarted:
		return ctrl.Result{}, fmt.Errorf("cannot continue bootstrap without the original claim")
	case apierrors.IsNotFound(err):
		return r.setStatus(ctx, &machine, "Pending", "HostMissing")
	case !host.DeletionTimestamp.IsZero():
		return r.setStatus(ctx, &machine, "Pending", "HostDeleting")
	case host.Status.Claim != nil:
		return r.setStatus(ctx, &machine, "Pending", "HostClaimed")
	case !controllerutil.ContainsFinalizer(&host, hostFinalizer),
		!host.Status.DiscoveryReady, host.Status.ObservedAt == nil,
		host.Spec.ManagedAddress && host.Status.ObservedGeneration != host.Generation,
		time.Since(host.Status.ObservedAt.Time) > 2*probeInterval:
		return r.setStatus(ctx, &machine, "Pending", "HostUnverified")
	}

	// A status update carries the resourceVersion read above. An API conflict
	// forces reconciliation to read the winning claim before it can try again.
	host.Status.Claim = &claim
	if err := r.Status().Update(ctx, &host); err != nil {
		return ctrl.Result{}, err
	}
	return r.reconcileBootstrap(ctx, &machine, &host)
}

func (r *MachineReconciler) setStatus(ctx context.Context, machine *api.VMetalMachine, phase, reason string) (ctrl.Result, error) {
	// Poll for changes to the requested host. This also repairs status from
	// the authoritative claim after a restart between the two writes.
	result := ctrl.Result{RequeueAfter: allocationPoll}
	if machine.Status.Phase == phase && machine.Status.Reason == reason {
		return result, nil
	}
	machine.Status.Phase, machine.Status.Reason = phase, reason
	return result, r.updateMachine(ctx, machine, true)
}

func (r *MachineReconciler) updateMachine(ctx context.Context, machine *api.VMetalMachine, status bool) error {
	// API decoding can discard TypeMeta for typed objects. Preserve it across
	// writes because this Go type is registered under two API groups.
	gvk := infraGroup(r.InfrastructureGroup).WithKind("VMetalMachine")
	machine.SetGroupVersionKind(gvk)
	defer machine.SetGroupVersionKind(gvk)
	if status {
		return r.Status().Update(ctx, machine)
	}
	return r.Update(ctx, machine)
}

func (r *MachineReconciler) SetupWithManager(manager ctrl.Manager) error {
	machine := &api.VMetalMachine{}
	machine.SetGroupVersionKind(infraGroup(r.InfrastructureGroup).WithKind("VMetalMachine"))
	builder := ctrl.NewControllerManagedBy(manager).For(machine)
	if r.InfrastructureGroup == api.InfrastructureGroupVersion {
		builder.Named("vmetalmachine-nvke")
	}
	return builder.
		WithOptions(controller.Options{MaxConcurrentReconciles: 2}).Complete(r)
}
