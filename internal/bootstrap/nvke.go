package bootstrap

import (
	"bytes"
	"encoding/base64"
	"fmt"
	"io"
	"regexp"
	"strings"

	"gopkg.in/yaml.v3"
)

type AgentPlan struct {
	Unit       string `json:"unit"`
	ProviderID string `json:"providerID"`
	NodeIP     string `json:"nodeIP"`
	Files      []File `json:"files"`
}

const (
	oldAgentEnv = "/etc/default/nke-agent"
	newAgentEnv = "/etc/default/nvke-agent"
	oldAgentCA  = "/etc/nke/controller-ca.crt"
	newAgentCA  = "/etc/nvke/controller-ca.crt"
	spireConfig = "/etc/spire-agent/agent.conf"
	spireBundle = "/etc/spire-agent/initial-trust-bundle.pem"
)

// This is the AGENT_ARGS shape emitted by the pinned NVKE infra renderer.
var agentArgs = regexp.MustCompile(`^AGENT_ARGS="--secure-server-port [0-9]{1,5}( --nvke-controller-client-ca-bundle=/etc/(nke|nvke)/controller-ca\.crt)?( --grpc-auth-mode=tls-jwt --dsx-identity-exchange-url=https://[a-zA-Z0-9.:/_%-]+)?"\n?$`)

// This deployment profile uses SPIRE's built-in join-token attestor.
var spireJoinTokenConfig = regexp.MustCompile(`^agent \{ data_dir = "/var/lib/spire-agent" log_level = "DEBUG" server_address = "[a-zA-Z0-9.:_-]+" server_port = "[0-9]+" trust_domain = "[a-zA-Z0-9.-]+" trust_bundle_path = "/etc/spire-agent/initial-trust-bundle.pem" join_token = "[a-zA-Z0-9_.:-]+" \} plugins \{ KeyManager "disk" \{ plugin_data \{ directory = "/var/lib/spire-agent" \} \} NodeAttestor "join_token" \{ plugin_data \{\} \} WorkloadAttestor "unix" \{ plugin_data \{\} \} \}$`)

func joinTokenProfile(content string) bool {
	var lines []string
	for _, line := range strings.Split(content, "\n") {
		if !strings.HasPrefix(strings.TrimSpace(line), "#") {
			lines = append(lines, line)
		}
	}
	return spireJoinTokenConfig.MatchString(strings.Join(strings.Fields(strings.Join(lines, " ")), " "))
}

func NVKEPlan(data []byte, providerID, nodeIP string) (AgentPlan, error) {
	var doc struct {
		Files []struct {
			File     `yaml:",inline"`
			Encoding string `yaml:"encoding"`
		} `yaml:"write_files"`
		Commands []string `yaml:"runcmd"`
	}
	if !bytes.HasPrefix(data, []byte("#cloud-config\n")) {
		return AgentPlan{}, fmt.Errorf("NVKE bootstrap must start with #cloud-config")
	}
	decoder := yaml.NewDecoder(bytes.NewReader(data))
	decoder.KnownFields(true)
	if decoder.Decode(&doc) != nil || decoder.Decode(new(any)) != io.EOF || len(doc.Commands) != 1 {
		return AgentPlan{}, fmt.Errorf("NVKE bootstrap requires agent files and one service restart")
	}
	plan := AgentPlan{ProviderID: providerID, NodeIP: nodeIP}
	var envPath, caPath string
	switch doc.Commands[0] {
	case "systemctl restart nke-agent.service":
		plan.Unit, envPath, caPath = "nke-agent.service", oldAgentEnv, oldAgentCA
	case "systemctl restart nvke-agent.service":
		plan.Unit, envPath, caPath = "nvke-agent.service", newAgentEnv, newAgentCA
	default:
		return AgentPlan{}, fmt.Errorf("NVKE bootstrap contains an unsupported command")
	}
	files := map[string]string{}
	for _, entry := range doc.Files {
		file := entry.File
		if _, exists := files[file.Path]; exists ||
			file.Owner != "root:root" || file.Mode != "0644" || file.Content == "" {
			return AgentPlan{}, fmt.Errorf("NVKE bootstrap contains invalid file metadata")
		}
		switch file.Path {
		case envPath:
			if entry.Encoding != "" || !agentArgs.MatchString(file.Content) {
				return AgentPlan{}, fmt.Errorf("NVKE bootstrap has unsupported agent arguments")
			}
			file.Mode = "0600"
		case caPath:
			if entry.Encoding != "b64" {
				return AgentPlan{}, fmt.Errorf("NVKE agent CA must be base64 encoded")
			}
			content, err := base64.StdEncoding.DecodeString(file.Content)
			if err != nil || !bytes.HasPrefix(content, []byte("-----BEGIN CERTIFICATE-----")) {
				return AgentPlan{}, fmt.Errorf("invalid NVKE controller CA")
			}
			file.Content = string(content)
		case spireConfig, spireBundle:
			if entry.Encoding != "" {
				return AgentPlan{}, fmt.Errorf("unsupported SPIRE file encoding")
			}
			if file.Path == spireConfig {
				if !joinTokenProfile(file.Content) {
					return AgentPlan{}, fmt.Errorf("NVKE bootstrap requires the built-in SPIRE join-token profile")
				}
				file.Mode = "0640"
			} else if !strings.HasPrefix(file.Content, "-----BEGIN CERTIFICATE-----") {
				return AgentPlan{}, fmt.Errorf("invalid SPIRE trust bundle")
			}
		default:
			return AgentPlan{}, fmt.Errorf("NVKE bootstrap contains an unsupported file path")
		}
		files[file.Path] = file.Content
		plan.Files = append(plan.Files, file)
	}
	if files[envPath] == "" || (files[spireConfig] == "") != (files[spireBundle] == "") {
		return AgentPlan{}, fmt.Errorf("NVKE bootstrap is missing agent or SPIRE configuration")
	}
	if strings.Contains(files[envPath], "--nvke-controller-client-ca-bundle=") != (files[caPath] != "") ||
		(strings.Contains(files[envPath], "--nvke-controller-client-ca-bundle=") && !strings.Contains(files[envPath], "="+caPath)) {
		return AgentPlan{}, fmt.Errorf("NVKE agent arguments and controller CA do not match")
	}
	return plan, nil
}
