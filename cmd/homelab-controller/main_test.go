package main

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/wait"
	"k8s.io/client-go/tools/clientcmd"
	clientcmdapi "k8s.io/client-go/tools/clientcmd/api"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	api "github.com/xdu31/cluster-api-provider-vmetal/api/v1alpha1"
)

func TestManagerResumesAndReleasesClaims(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("set KUBEBUILDER_ASSETS to run the controller integration test")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	t.Cleanup(cancel)
	testEnvironment := &envtest.Environment{
		CRDDirectoryPaths:     []string{"../../config/crd", "../../config/crd/nvke"},
		ErrorIfCRDPathMissing: true,
	}
	config, err := testEnvironment.Start()
	check(t, err)
	t.Cleanup(func() { check(t, testEnvironment.Stop()) })

	scheme := runtime.NewScheme()
	check(t, corev1.AddToScheme(scheme))
	check(t, api.SchemeBuilder.AddToScheme(scheme))
	api.AddInfrastructureToScheme(scheme)
	c, err := client.New(config, client.Options{Scheme: scheme})
	check(t, err)
	for _, namespace := range []string{"kube-system", "homelab-test"} {
		err := c.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}})
		if !apierrors.IsAlreadyExists(err) {
			check(t, err)
		}
	}

	id := strings.Repeat("c", 64)
	bin := t.TempDir()
	// Exercise the real SSH command and embedded probe without a remote machine.
	ssh := `#!/bin/sh
set -eu
test "${11}" = "ubuntu@worker"
cat > /dev/null
printf '%s\n' '{"machine_id_hash":"` + id + `","os":"Ubuntu","logical_cpus":4}'
`
	check(t, os.WriteFile(filepath.Join(bin, "ssh"), []byte(ssh), 0700))
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	kubeconfig := filepath.Join(bin, "kubeconfig")
	check(t, clientcmd.WriteToFile(clientcmdapi.Config{
		Clusters: map[string]*clientcmdapi.Cluster{
			"test": {Server: config.Host, CertificateAuthorityData: config.CAData},
		},
		AuthInfos: map[string]*clientcmdapi.AuthInfo{
			"test": {ClientCertificateData: config.CertData, ClientKeyData: config.KeyData},
		},
		Contexts:       map[string]*clientcmdapi.Context{"test": {Cluster: "test", AuthInfo: "test"}},
		CurrentContext: "test",
	}, kubeconfig))
	binary, err := os.Executable()
	check(t, err)

	host := &api.HomelabHost{
		ObjectMeta: metav1.ObjectMeta{Name: "host-" + id[:32]},
		Spec:       api.HomelabHostSpec{MachineID: id, SSHTarget: "ubuntu@worker"},
	}
	check(t, c.Create(ctx, host))
	newMachine := func(name string) *api.HomelabMachine {
		return &api.HomelabMachine{
			TypeMeta:   metav1.TypeMeta{APIVersion: api.GroupVersion.String(), Kind: "HomelabMachine"},
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "homelab-test"},
			Spec:       api.HomelabMachineSpec{HostRef: host.Name},
		}
	}
	machine := newMachine("worker")
	check(t, c.Create(ctx, machine))
	machine.SetGroupVersionKind(api.GroupVersion.WithKind("HomelabMachine"))
	start := func() func() {
		t.Helper()
		process := exec.CommandContext(ctx, binary, "-test.run=^TestControllerProcess$")
		process.Env = append(os.Environ(), "HOMELAB_TEST_PROCESS=1", "KUBECONFIG="+kubeconfig, "HOMELAB_ENABLE_NVKE_INFRA=1")
		process.Stdout, process.Stderr = os.Stdout, os.Stderr
		check(t, process.Start())
		done := make(chan struct{})
		var runErr error
		go func() {
			defer close(done)
			runErr = process.Wait()
		}()
		stopAndWait := func() {
			t.Helper()
			if err := process.Process.Signal(os.Interrupt); err != nil && !errors.Is(err, os.ErrProcessDone) {
				t.Error(err)
			}
			select {
			case <-done:
				check(t, runErr)
			case <-time.After(15 * time.Second):
				t.Error("controller did not stop")
				_ = process.Process.Kill()
			}
		}
		t.Cleanup(stopAndWait)
		return stopAndWait
	}
	waitForReservation := func(machine *api.HomelabMachine) {
		t.Helper()
		gvk := machine.GroupVersionKind()
		check(t, wait.PollUntilContextTimeout(ctx, 200*time.Millisecond, 40*time.Second, true,
			func(ctx context.Context) (bool, error) {
				machine.SetGroupVersionKind(gvk)
				if err := c.Get(ctx, client.ObjectKeyFromObject(machine), machine); err != nil {
					return false, err
				}
				return machine.Status.Phase == "Reserved", nil
			}))
		machine.SetGroupVersionKind(gvk)
		check(t, c.Get(ctx, client.ObjectKeyFromObject(host), host))
		if host.Status.Claim == nil || host.Status.Claim.UID != machine.UID {
			t.Fatalf("host is missing the reservation for %s: %+v", machine.Name, host.Status.Claim)
		}
	}

	stop := start()
	waitForReservation(machine)
	stop()

	// A restart must recover the machine status from the persisted host claim.
	machine.Status = api.HomelabMachineStatus{}
	check(t, c.Status().Update(ctx, machine))
	start()
	waitForReservation(machine)

	replacement := newMachine("replacement")
	replacementGVK := api.InfrastructureGroupVersion.WithKind("HomelabMachine")
	replacement.SetGroupVersionKind(replacementGVK)
	check(t, c.Create(ctx, replacement))
	check(t, wait.PollUntilContextTimeout(ctx, 200*time.Millisecond, 15*time.Second, true,
		func(ctx context.Context) (bool, error) {
			replacement.SetGroupVersionKind(replacementGVK)
			if err := c.Get(ctx, client.ObjectKeyFromObject(replacement), replacement); err != nil {
				return false, err
			}
			return replacement.Status.Reason == "HostClaimed", nil
		}))
	replacement.SetGroupVersionKind(replacementGVK)
	check(t, c.Delete(ctx, machine))
	waitForReservation(replacement)
}

func TestControllerProcess(t *testing.T) {
	if os.Getenv("HOMELAB_TEST_PROCESS") != "1" {
		t.Skip("only run as the controller subprocess")
	}
	check(t, run())
}

func check(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}
