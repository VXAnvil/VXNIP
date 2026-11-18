package main

import (
	"fmt"
	"os"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	api "github.com/xdu31/cluster-api-provider-vmetal/api/v1alpha1"
	"github.com/xdu31/cluster-api-provider-vmetal/internal/bootstrap"
	"github.com/xdu31/cluster-api-provider-vmetal/internal/controllers"
	"github.com/xdu31/cluster-api-provider-vmetal/internal/discovery"
)

func run() error {
	ctrl.SetLogger(zap.New())
	scheme := runtime.NewScheme()
	if err := corev1.AddToScheme(scheme); err != nil {
		return err
	}
	if err := api.SchemeBuilder.AddToScheme(scheme); err != nil {
		return err
	}
	standaloneMachine := &api.VMetalMachine{}
	standaloneMachine.SetGroupVersionKind(api.GroupVersion.WithKind("VMetalMachine"))
	directReads := []client.Object{&api.VMetalHost{}, standaloneMachine, &corev1.Secret{}}
	enableNVKE := os.Getenv("VMETAL_ENABLE_NVKE_INFRA") == "1"
	if enableNVKE {
		api.AddInfrastructureToScheme(scheme)
		nvkeMachine := &api.VMetalMachine{}
		nvkeMachine.SetGroupVersionKind(api.InfrastructureGroupVersion.WithKind("VMetalMachine"))
		directReads = append(directReads, nvkeMachine)
	}
	config, err := ctrl.GetConfig()
	if err != nil {
		return err
	}
	config.Timeout = 15 * time.Second
	manager, err := ctrl.NewManager(config, ctrl.Options{
		Scheme:                  scheme,
		Metrics:                 metricsserver.Options{BindAddress: "0"},
		LeaderElection:          true,
		LeaderElectionID:        "vmetal-controller",
		LeaderElectionNamespace: "kube-system",
		// A finalizer must never be removed based on an old host claim from
		// a cache that has yet to observe the reservation write.
		Client: client.Options{Cache: &client.CacheOptions{DisableFor: directReads}},
	})
	if err != nil {
		return err
	}
	host := controllers.HostReconciler{Client: manager.GetClient(), Discovery: discovery.SSH{}}
	if err := host.SetupWithManager(manager); err != nil {
		return err
	}
	machine := controllers.MachineReconciler{Client: manager.GetClient(), Bootstrap: bootstrap.SSH{}}
	if err := machine.SetupWithManager(manager); err != nil {
		return err
	}
	cluster := controllers.ClusterReconciler{Client: manager.GetClient()}
	if err := cluster.SetupWithManager(manager); err != nil {
		return err
	}
	if enableNVKE {
		nvkeMachine := machine
		nvkeMachine.InfrastructureGroup = api.InfrastructureGroupVersion
		nvkeCluster := cluster
		nvkeCluster.InfrastructureGroup = api.InfrastructureGroupVersion
		if err := nvkeMachine.SetupWithManager(manager); err != nil {
			return err
		}
		if err := nvkeCluster.SetupWithManager(manager); err != nil {
			return err
		}
	}
	return manager.Start(ctrl.SetupSignalHandler())
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "vmetal-controller:", err)
		os.Exit(1)
	}
}
