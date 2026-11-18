package controllers

import (
	"context"
	"encoding/json"
	"time"

	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/predicate"

	api "github.com/xdu31/cluster-api-provider-vmetal/api/v1alpha1"
	"github.com/xdu31/cluster-api-provider-vmetal/internal/discovery"
)

const (
	hostFinalizer = "homelab.infrastructure.cluster.x-k8s.io/host"
	probeInterval = time.Minute
)

type HostReconciler struct {
	client.Client
	Discovery discovery.Discoverer
}

func (r *HostReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var host api.HomelabHost
	if err := r.Get(ctx, req.NamespacedName, &host); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	if !host.DeletionTimestamp.IsZero() {
		if host.Status.Claim == nil && controllerutil.RemoveFinalizer(&host, hostFinalizer) {
			return ctrl.Result{}, r.Update(ctx, &host)
		}
		return ctrl.Result{RequeueAfter: probeInterval}, nil
	}

	if controllerutil.AddFinalizer(&host, hostFinalizer) {
		if err := r.Update(ctx, &host); err != nil {
			return ctrl.Result{}, err
		}
	}

	hardware, err := r.Discovery.Discover(ctx, host.Spec.SSHTarget)
	host.Status.ObservedGeneration = host.Generation
	switch {
	case err != nil:
		ctrl.LoggerFrom(ctx).Error(err, "SSH discovery failed")
		host.Status.DiscoveryReady = false
		host.Status.DiscoveryReason = "ProbeFailed"
	case hardware.ID != host.Spec.MachineID:
		host.Status.DiscoveryReady = false
		host.Status.DiscoveryReason = "IdentityMismatch"
	default:
		data, err := json.Marshal(hardware)
		if err != nil {
			return ctrl.Result{}, err
		}
		now := metav1.Now()
		host.Status.Hardware = &apiextensionsv1.JSON{Raw: data}
		host.Status.ObservedAt = &now
		host.Status.DiscoveryReady = true
		host.Status.DiscoveryReason = "Verified"
	}
	return ctrl.Result{RequeueAfter: probeInterval}, r.Status().Update(ctx, &host)
}

func (r *HostReconciler) SetupWithManager(manager ctrl.Manager) error {
	// Skip probe results and claims so publishing status does not trigger another probe.
	changes := predicate.Or(predicate.GenerationChangedPredicate{}, predicate.Funcs{
		UpdateFunc: func(e event.UpdateEvent) bool {
			return e.ObjectOld.GetDeletionTimestamp().IsZero() != e.ObjectNew.GetDeletionTimestamp().IsZero()
		},
	})
	return ctrl.NewControllerManagedBy(manager).For(&api.HomelabHost{}).
		WithEventFilter(changes).Complete(r)
}
