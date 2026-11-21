package controllers

import (
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	api "github.com/VXAnvil/cluster-api-provider-vxanvil/api/v1alpha1"
	ext "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/tools/clientcmd"
	configapi "k8s.io/client-go/tools/clientcmd/api"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
)

func TestReleasedLegacyHostMigrationWithAPIServer(t *testing.T) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		t.Skip("set KUBEBUILDER_ASSETS")
	}
	if _, err := exec.LookPath("kubectl"); err != nil {
		t.Skip("kubectl is required for migration integration")
	}
	environment := &envtest.Environment{CRDDirectoryPaths: []string{"../../config/crd"}, ErrorIfCRDPathMissing: true}
	cfg, err := environment.Start()
	must(t, err)
	t.Cleanup(func() { must(t, environment.Stop()) })
	scheme := runtime.NewScheme()
	must(t, ext.AddToScheme(scheme))
	must(t, api.SchemeBuilder.AddToScheme(scheme))
	c, err := client.New(cfg, client.Options{Scheme: scheme})
	must(t, err)
	preserve := true
	legacy := &ext.CustomResourceDefinition{
		ObjectMeta: metav1.ObjectMeta{Name: "homelabhosts.homelab.infrastructure.cluster.x-k8s.io"},
		Spec: ext.CustomResourceDefinitionSpec{Group: "homelab.infrastructure.cluster.x-k8s.io", Scope: ext.ClusterScoped,
			Names: ext.CustomResourceDefinitionNames{Plural: "homelabhosts", Kind: "HomelabHost"},
			Versions: []ext.CustomResourceDefinitionVersion{{Name: "v1alpha1", Served: true, Storage: true,
				Schema: &ext.CustomResourceValidation{OpenAPIV3Schema: &ext.JSONSchemaProps{Type: "object", XPreserveUnknownFields: &preserve}}}}},
	}
	must(t, c.Create(context.Background(), legacy))
	directory := t.TempDir()
	kubeconfig := filepath.Join(directory, "kubeconfig")
	must(t, clientcmd.WriteToFile(configapi.Config{
		Clusters:  map[string]*configapi.Cluster{"test": {Server: cfg.Host, CertificateAuthorityData: cfg.CAData}},
		AuthInfos: map[string]*configapi.AuthInfo{"test": {ClientCertificateData: cfg.CertData, ClientKeyData: cfg.KeyData}},
		Contexts:  map[string]*configapi.Context{"test": {Cluster: "test", AuthInfo: "test"}}, CurrentContext: "test",
	}, kubeconfig))
	// envtest waits for the dynamic discovery endpoint through kubectl.
	command := exec.Command("kubectl", "--kubeconfig", kubeconfig, "wait", "--for=condition=Established", "crd/"+legacy.Name, "--timeout=20s")
	out, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("CRD discovery: %v %s", err, out)
	}
	identity := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	oldID := fmt.Sprintf("%x", sha256.Sum256([]byte("nvke-homelab:"+identity)))
	newID := fmt.Sprintf("%x", sha256.Sum256([]byte("vmetal:"+identity)))
	old := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "homelab.infrastructure.cluster.x-k8s.io/v1alpha1", "kind": "HomelabHost",
		"metadata": map[string]any{"name": "host-" + oldID[:32]},
		"spec":     map[string]any{"machineID": oldID, "sshTarget": "ubuntu@worker", "allowBootstrap": true},
	}}
	must(t, c.Create(context.Background(), old))
	must(t, os.WriteFile(filepath.Join(directory, "ssh"), []byte("#!/bin/sh\nprintf '%s\\n' '"+identity+"'\n"), 0700))
	t.Setenv("PATH", directory+string(os.PathListSeparator)+os.Getenv("PATH"))
	plan := filepath.Join(directory, "migration.json")
	script := "../../scripts/migrate-vxanvil-hosts.py"
	for _, apply := range []bool{false, true, true} {
		args := []string{script, "--kubeconfig", kubeconfig, "--plan", plan}
		if apply {
			args = append(args, "--apply")
		}
		out, err := exec.Command("python3", args...).CombinedOutput()
		if err != nil {
			t.Fatalf("migration apply=%v: %v %s", apply, err, out)
		}
	}
	var registered api.VXAnvilHost
	must(t, c.Get(context.Background(), client.ObjectKey{Name: "host-" + newID[:32]}, &registered))
	if registered.Spec.MachineID != newID || registered.Annotations["vmetal.nvke.dev/migrated-from-uid"] != string(old.GetUID()) {
		t.Fatalf("migration lost identity provenance: %+v", registered)
	}
}
