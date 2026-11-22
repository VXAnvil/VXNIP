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

	api "github.com/VXAnvil/VXNIP/api/v1alpha1"
	"github.com/VXAnvil/VXNIP/internal/bootstrap"
	"github.com/VXAnvil/VXNIP/internal/controllers"
	"github.com/VXAnvil/VXNIP/internal/discovery"
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
	standaloneMachine := &api.VXNIPMachine{}
	standaloneMachine.SetGroupVersionKind(api.GroupVersion.WithKind("VXNIPMachine"))
	directReads := []client.Object{&api.VXNIPHost{}, standaloneMachine, &corev1.Secret{}}
	enableNVKE := nvkeEnabled()
	if enableNVKE {
		api.AddInfrastructureToScheme(scheme)
		nvkeMachine := &api.VXNIPMachine{}
		nvkeMachine.SetGroupVersionKind(api.InfrastructureGroupVersion.WithKind("VXNIPMachine"))
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
		LeaderElectionID:        "vxnip-controller",
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

func nvkeEnabled() bool {
	return os.Getenv("VXNIP_ENABLE_NVKE_INFRA") == "1"
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "vxnip-controller:", err)
		os.Exit(1)
	}
}
