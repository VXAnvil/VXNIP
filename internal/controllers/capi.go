package controllers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"strings"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	api "github.com/VXAnvil/cluster-api-provider-vxanvil/api/v1alpha1"
	"github.com/VXAnvil/cluster-api-provider-vxanvil/internal/bootstrap"
)

const (
	capiVersion      = "cluster.x-k8s.io/v1beta2"
	pausedAnnotation = "cluster.x-k8s.io/paused"
)

func isPaused(obj client.Object) bool {
	_, found := obj.GetAnnotations()[pausedAnnotation]
	return found
}

type ClusterReconciler struct {
	client.Client
	InfrastructureGroup schema.GroupVersion
}

func infraGroup(group schema.GroupVersion) schema.GroupVersion {
	if group.Empty() {
		return api.GroupVersion
	}
	return group
}

func (r *ClusterReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var cluster api.VXAnvilCluster
	cluster.SetGroupVersionKind(infraGroup(r.InfrastructureGroup).WithKind("VXAnvilCluster"))
	if err := r.Get(ctx, req.NamespacedName, &cluster); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !cluster.DeletionTimestamp.IsZero() || isPaused(&cluster) || cluster.Status.Initialization != nil {
		return ctrl.Result{}, nil
	}
	// The endpoint was allocated and configured by the operator before the
	// Cluster was created. This provider has no external cluster-level resources.
	cluster.Status.Initialization = &api.InfrastructureInitialization{Provisioned: true}
	cluster.SetGroupVersionKind(infraGroup(r.InfrastructureGroup).WithKind("VXAnvilCluster"))
	return ctrl.Result{}, r.Status().Update(ctx, &cluster)
}

func (r *ClusterReconciler) SetupWithManager(manager ctrl.Manager) error {
	cluster := &api.VXAnvilCluster{}
	cluster.SetGroupVersionKind(infraGroup(r.InfrastructureGroup).WithKind("VXAnvilCluster"))
	builder := ctrl.NewControllerManagedBy(manager).For(cluster)
	if r.InfrastructureGroup == api.InfrastructureGroupVersion {
		builder.Named("vxanvilcluster-nvke")
	}
	return builder.Complete(r)
}

func capiOwner(machine *api.VXAnvilMachine) *metav1.OwnerReference {
	for _, owner := range machine.OwnerReferences {
		if owner.Kind == "Machine" && strings.HasPrefix(owner.APIVersion, "cluster.x-k8s.io/") {
			return &owner
		}
	}
	return nil
}

func (r *MachineReconciler) reconcileBootstrap(
	ctx context.Context, machine *api.VXAnvilMachine, host *api.VXAnvilHost,
) (ctrl.Result, error) {
	owner := capiOwner(machine)
	if owner == nil {
		if machine.Status.BootstrapStarted {
			return ctrl.Result{RequeueAfter: allocationPoll}, nil
		}
		return r.setStatus(ctx, machine, "Reserved", "HostReserved")
	}
	var capiMachine unstructured.Unstructured
	capiMachine.SetAPIVersion(capiVersion)
	capiMachine.SetKind("Machine")
	if err := r.Get(ctx, client.ObjectKey{Namespace: machine.Namespace, Name: owner.Name}, &capiMachine); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{RequeueAfter: allocationPoll}, nil
		}
		return ctrl.Result{}, err
	}
	ref, _, _ := unstructured.NestedStringMap(capiMachine.Object, "spec", "infrastructureRef")
	if capiMachine.GetUID() != owner.UID || ref["apiGroup"] != infraGroup(r.InfrastructureGroup).Group ||
		ref["kind"] != "VXAnvilMachine" || ref["name"] != machine.Name {
		return ctrl.Result{}, fmt.Errorf("CAPI Machine does not reference this VXAnvilMachine")
	}
	if isPaused(&capiMachine) || !capiMachine.GetDeletionTimestamp().IsZero() {
		return ctrl.Result{RequeueAfter: allocationPoll}, nil
	}
	var cluster unstructured.Unstructured
	cluster.SetAPIVersion(capiVersion)
	cluster.SetKind("Cluster")
	clusterName, _, _ := unstructured.NestedString(capiMachine.Object, "spec", "clusterName")
	if err := r.Get(ctx, client.ObjectKey{Namespace: machine.Namespace, Name: clusterName}, &cluster); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{RequeueAfter: allocationPoll}, nil
		}
		return ctrl.Result{}, err
	}
	paused, _, _ := unstructured.NestedBool(cluster.Object, "spec", "paused")
	if paused || isPaused(&cluster) || !cluster.GetDeletionTimestamp().IsZero() {
		return ctrl.Result{RequeueAfter: allocationPoll}, nil
	}
	if machine.Status.Initialization != nil && machine.Status.Initialization.Provisioned {
		return ctrl.Result{}, nil
	}
	if !host.Spec.AllowBootstrap {
		return r.setStatus(ctx, machine, "Reserved", "BootstrapDisabled")
	}
	if machine.Spec.BootstrapMode == "NVKE" && len(machine.Status.Addresses) == 0 {
		if !verifiedNodeIP(host) {
			return r.setStatus(ctx, machine, "Reserved", "NodeIPUnverified")
		}
		machine.Status.Addresses = []api.MachineAddress{{Type: "InternalIP", Address: host.Spec.NodeIP}}
		if err := r.updateMachine(ctx, machine, true); err != nil {
			return ctrl.Result{}, err
		}
	}
	secretName, _, _ := unstructured.NestedString(capiMachine.Object, "spec", "bootstrap", "dataSecretName")
	if secretName == "" {
		return r.setStatus(ctx, machine, "Reserved", "WaitingForBootstrap")
	}
	var secret corev1.Secret
	if err := r.Get(ctx, client.ObjectKey{Namespace: machine.Namespace, Name: secretName}, &secret); err != nil {
		if apierrors.IsNotFound(err) {
			return r.setStatus(ctx, machine, "Reserved", "WaitingForBootstrap")
		}
		return ctrl.Result{}, err
	}
	if secret.Labels["cluster.x-k8s.io/cluster-name"] != clusterName {
		return ctrl.Result{}, fmt.Errorf("bootstrap Secret belongs to a different cluster")
	}
	providerID := "vxanvil://" + string(machine.UID)
	if machine.Spec.ProviderID != "" && machine.Spec.ProviderID != providerID {
		return ctrl.Result{}, fmt.Errorf("spec.providerID does not match the allocation")
	}
	request := bootstrap.Request{Action: "apply"}
	if machine.Spec.BootstrapMode == "NVKE" {
		if secret.Type != corev1.SecretTypeOpaque || secret.Name != owner.Name+"-bootstrap-data" ||
			len(secret.Data["format"]) != 0 {
			return ctrl.Result{}, fmt.Errorf("expected an NVKE bootstrap Secret")
		}
		plan, err := bootstrap.NVKEPlan(secret.Data["value"], providerID, host.Spec.NodeIP)
		if err != nil {
			return ctrl.Result{}, err
		}
		request.Action, request.Agent = "apply-nvke", &plan
	} else {
		if secret.Type != "cluster.x-k8s.io/secret" || string(secret.Data["format"]) != "cloud-config" {
			return ctrl.Result{}, fmt.Errorf("expected a kubeadm cloud-config Secret")
		}
		plan, err := bootstrap.KubeadmPlan(secret.Data["value"], providerID)
		if err != nil {
			return ctrl.Result{}, err
		}
		request.Plan = &plan
	}
	if !machine.Status.BootstrapStarted {
		machine.Status.BootstrapStarted = true
		if _, err := r.setStatus(ctx, machine, "Reserved", "BootstrapRunning"); err != nil {
			return ctrl.Result{}, err
		}
	}
	if err := r.runBootstrap(ctx, machine, host, request); err != nil {
		return ctrl.Result{}, err
	}
	if machine.Spec.ProviderID == "" {
		machine.Spec.ProviderID = providerID
		if err := r.updateMachine(ctx, machine, false); err != nil {
			return ctrl.Result{}, err
		}
	}
	machine.Status.Initialization = &api.InfrastructureInitialization{Provisioned: true}
	return r.setStatus(ctx, machine, "Provisioned", "BootstrapComplete")
}

func verifiedNodeIP(host *api.VXAnvilHost) bool {
	address, err := netip.ParseAddr(host.Spec.NodeIP)
	if err != nil || !address.Is4() || !address.IsGlobalUnicast() ||
		!host.Status.DiscoveryReady || host.Status.Hardware == nil {
		return false
	}
	var hardware struct {
		Interfaces []struct {
			Addresses []string `json:"addresses"`
		} `json:"interfaces"`
	}
	if json.Unmarshal(host.Status.Hardware.Raw, &hardware) != nil {
		return false
	}
	for _, iface := range hardware.Interfaces {
		for _, candidate := range iface.Addresses {
			if prefix, err := netip.ParsePrefix(candidate); err == nil && prefix.Addr() == address {
				return true
			}
		}
	}
	return false
}

func (r *MachineReconciler) runBootstrap(ctx context.Context, machine *api.VXAnvilMachine,
	host *api.VXAnvilHost, request bootstrap.Request) error {
	if r.Bootstrap == nil {
		return fmt.Errorf("bootstrap runner is unavailable")
	}
	request.MachineID, request.OwnerUID = host.Spec.MachineID, string(machine.UID)
	if err := r.Bootstrap.Run(ctx, host.Spec.SSHTarget, request); err != nil {
		machine.Status.Reason = "BootstrapFailed"
		if strings.HasPrefix(request.Action, "reset") {
			machine.Status.Reason = "ResetFailed"
		}
		machine.Status.Message = fmt.Sprintf("Remote %s failed. Check the node helper, CRI, agent and SPIRE journals. Allocation ownership is retained.", request.Action)
		return errors.Join(err, r.updateMachine(ctx, machine, true))
	}
	machine.Status.Message = ""
	return nil
}
