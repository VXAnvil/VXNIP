package bootstrap

import (
	"strings"
	"testing"
)

const agentPayload = `#cloud-config
write_files:
  - path: /etc/default/nke-agent
    owner: root:root
    permissions: "0644"
    content: |
      AGENT_ARGS="--secure-server-port 15005"
  - path: /etc/spire-agent/initial-trust-bundle.pem
    owner: root:root
    permissions: "0644"
    content: |
      -----BEGIN CERTIFICATE-----
      test-only
      -----END CERTIFICATE-----
  - path: /etc/spire-agent/agent.conf
    owner: root:root
    permissions: "0644"
    content: |
      agent {
        data_dir = "/var/lib/spire-agent"
        log_level = "DEBUG"
        server_address = "192.0.2.10"
        server_port = "8081"
        trust_domain = "test.example"
        trust_bundle_path = "/etc/spire-agent/initial-trust-bundle.pem"
        join_token = "fake-join-token"
      }
      plugins {
        KeyManager "disk" {
          plugin_data {
            directory = "/var/lib/spire-agent"
          }
        }
        NodeAttestor "join_token" {
          plugin_data {}
        }
        WorkloadAttestor "unix" {
          plugin_data {}
        }
      }
runcmd:
  - systemctl restart nke-agent.service
`

func TestNVKEPlan(t *testing.T) {
	for _, unit := range []string{"nke-agent", "nvke-agent"} {
		payload := strings.ReplaceAll(agentPayload, "nke-agent", unit)
		plan, err := NVKEPlan([]byte(payload), "homelab://worker", "10.0.0.53")
		if err != nil {
			t.Fatal(err)
		}
		if plan.Unit != unit+".service" || plan.ProviderID != "homelab://worker" ||
			plan.NodeIP != "10.0.0.53" || plan.Files[0].Mode != "0600" || plan.Files[2].Mode != "0640" {
			t.Fatal("NVKE plan did not preserve identity or restrict credential permissions")
		}
	}

	for _, payload := range []string{
		strings.Replace(agentPayload, "systemctl restart", "sudo systemctl restart", 1),
		strings.Replace(agentPayload, "/etc/default/nke-agent", "/etc/sudoers", 1),
		strings.Replace(agentPayload, "NodeAttestor \"join_token\"", "NodeAttestor \"tpm\"", 1),
		strings.Replace(agentPayload, "plugin_data {}", "plugin_cmd = \"/bin/sh\"", 1),
		agentPayload + "  - touch /tmp/unexpected\n",
	} {
		if _, err := NVKEPlan([]byte(payload), "homelab://worker", "10.0.0.53"); err == nil {
			t.Fatal("unsupported NVKE bootstrap data was accepted")
		}
	}
}
