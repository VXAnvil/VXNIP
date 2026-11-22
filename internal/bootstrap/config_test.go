package bootstrap

import (
	"strings"
	"testing"
)

func TestKubeadmPlanForInitAndJoin(t *testing.T) {
	for _, command := range []string{"init", "join"} {
		t.Run(command, func(t *testing.T) {
			path := "/run/kubeadm/kubeadm-join-config.yaml"
			kind := "JoinConfiguration"
			clusterConfig := ""
			if command == "init" {
				path = "/run/kubeadm/kubeadm.yaml"
				kind = "InitConfiguration"
				clusterConfig = `      apiVersion: kubeadm.k8s.io/v1beta4
      kind: ClusterConfiguration
      controlPlaneEndpoint: "10.0.0.10:6443"
      ---
`
			}
			payload := "## template: jinja\n#cloud-config\nwrite_files:\n- path: " + path + "\n" + `  owner: root:root
  permissions: '0640'
  content: |
` + clusterConfig + `      apiVersion: kubeadm.k8s.io/v1beta4
      kind: ` + kind + `
      nodeRegistration:
        kubeletExtraArgs:
        - name: provider-id
          value: old-id
        - name: node-ip
          value: 10.0.0.10
runcmd:
- 'kubeadm ` + command + " --config " + path + `  && echo success > /run/cluster-api/bootstrap-success.complete'
`
			providerID := "vxanvil://new-id"
			plan, err := KubeadmPlan([]byte(payload), providerID)
			if err != nil {
				t.Fatal(err)
			}
			if plan.Command != command || !strings.Contains(plan.Config, providerID) ||
				strings.Contains(plan.Config, "old-id") || !strings.Contains(plan.Config, "node-ip") {
				t.Fatalf("unexpected plan: %+v", plan)
			}
			for _, unsupported := range []string{
				strings.Replace(payload, "runcmd:", "bootcmd: []\nruncmd:", 1),
				strings.Replace(payload, "echo success", "curl example.com", 1),
				strings.Replace(payload, "permissions:", "append: true\n  permissions:", 1),
				strings.Replace(payload, "kubeadm.k8s.io/v1beta4", "kubeadm.k8s.io/v1beta3", 1),
			} {
				if _, err := KubeadmPlan([]byte(unsupported), providerID); err == nil {
					t.Fatal("unsupported bootstrap data was accepted")
				}
			}
		})
	}
}
