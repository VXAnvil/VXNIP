package bootstrap

import (
	"bytes"
	"fmt"
	"io"
	"strings"

	"gopkg.in/yaml.v3"
)

type File struct {
	Path    string `json:"path" yaml:"path"`
	Owner   string `json:"-" yaml:"owner"`
	Mode    string `json:"mode" yaml:"permissions"`
	Content string `json:"content" yaml:"content"`
}

type Plan struct {
	Command string `json:"command"`
	Config  string `json:"config"`
	Files   []File `json:"files"`
}

// KubeadmPlan supports the default CABPK cloud-config for init and worker join.
// Reject extra cloud-init operations instead of executing arbitrary shell.
func KubeadmPlan(data []byte, providerID string) (Plan, error) {
	var config struct {
		Files    []File   `yaml:"write_files"`
		Commands []string `yaml:"runcmd"`
	}
	if !bytes.HasPrefix(data, []byte("#cloud-config\n")) &&
		!bytes.HasPrefix(data, []byte("## template: jinja\n#cloud-config\n")) {
		return Plan{}, fmt.Errorf("bootstrap data is not supported cloud-config")
	}
	decoder := yaml.NewDecoder(bytes.NewReader(data))
	decoder.KnownFields(true)
	if decoder.Decode(&config) != nil || decoder.Decode(new(any)) != io.EOF {
		return Plan{}, fmt.Errorf("bootstrap requires cloud-config with only write_files and runcmd")
	}
	if len(config.Commands) != 1 {
		return Plan{}, fmt.Errorf("bootstrap requires exactly one standard kubeadm command")
	}
	command := strings.Join(strings.Fields(config.Commands[0]), " ")
	plan := Plan{Files: []File{}}
	var kubeadmPath string
	switch command {
	case "kubeadm init --config /run/kubeadm/kubeadm.yaml && echo success > /run/cluster-api/bootstrap-success.complete":
		plan.Command, kubeadmPath = "init", "/run/kubeadm/kubeadm.yaml"
	case "kubeadm join --config /run/kubeadm/kubeadm-join-config.yaml && echo success > /run/cluster-api/bootstrap-success.complete":
		plan.Command, kubeadmPath = "join", "/run/kubeadm/kubeadm-join-config.yaml"
	default:
		return Plan{}, fmt.Errorf("unsupported kubeadm command in bootstrap data")
	}
	seen := map[string]bool{}
	for _, file := range config.Files {
		if seen[file.Path] || file.Owner != "root:root" ||
			(file.Mode != "0600" && file.Mode != "0640") {
			return Plan{}, fmt.Errorf("unsupported bootstrap file metadata")
		}
		seen[file.Path] = true
		switch {
		case file.Path == kubeadmPath:
			rendered, err := setProviderID(file.Content, plan.Command, providerID)
			if err != nil {
				return Plan{}, err
			}
			plan.Config = rendered
		case file.Path == "/run/cluster-api/placeholder":
			continue
		case plan.Command == "init" && isCertificate(file.Path):
			plan.Files = append(plan.Files, file)
		default:
			return Plan{}, fmt.Errorf("bootstrap contains an unsupported file")
		}
	}
	if plan.Config == "" {
		return Plan{}, fmt.Errorf("kubeadm config is missing")
	}
	return plan, nil
}

func isCertificate(path string) bool {
	switch path {
	case "/etc/kubernetes/pki/ca.crt", "/etc/kubernetes/pki/ca.key",
		"/etc/kubernetes/pki/etcd/ca.crt", "/etc/kubernetes/pki/etcd/ca.key",
		"/etc/kubernetes/pki/front-proxy-ca.crt", "/etc/kubernetes/pki/front-proxy-ca.key",
		"/etc/kubernetes/pki/sa.key", "/etc/kubernetes/pki/sa.pub":
		return true
	}
	return false
}

func setProviderID(content, command, providerID string) (string, error) {
	decoder := yaml.NewDecoder(strings.NewReader(content))
	var output bytes.Buffer
	encoder := yaml.NewEncoder(&output)
	defer encoder.Close()
	want := "JoinConfiguration"
	if command == "init" {
		want = "InitConfiguration"
	}
	seen := map[string]bool{}
	for {
		var doc map[string]any
		err := decoder.Decode(&doc)
		if err == io.EOF {
			break
		}
		if err != nil || doc["apiVersion"] != "kubeadm.k8s.io/v1beta4" {
			return "", fmt.Errorf("expected kubeadm.k8s.io/v1beta4 configuration")
		}
		kind, _ := doc["kind"].(string)
		if seen[kind] || (kind != want && (command != "init" || kind != "ClusterConfiguration")) {
			return "", fmt.Errorf("unexpected kubeadm configuration kind")
		}
		seen[kind] = true
		if kind == want {
			if kind == "JoinConfiguration" && doc["controlPlane"] != nil {
				return "", fmt.Errorf("control-plane join is not supported")
			}
			registration, _ := doc["nodeRegistration"].(map[string]any)
			if registration == nil {
				registration = map[string]any{}
				doc["nodeRegistration"] = registration
			}
			args, _ := registration["kubeletExtraArgs"].([]any)
			withoutProviderID := make([]any, 0, len(args)+1)
			for _, arg := range args {
				fields, ok := arg.(map[string]any)
				if !ok {
					return "", fmt.Errorf("invalid kubelet extra argument")
				}
				if fields["name"] != "provider-id" {
					withoutProviderID = append(withoutProviderID, fields)
				}
			}
			registration["kubeletExtraArgs"] = append(withoutProviderID, map[string]string{
				"name": "provider-id", "value": providerID,
			})
		}
		if err := encoder.Encode(doc); err != nil {
			return "", fmt.Errorf("could not render kubeadm configuration")
		}
	}
	if !seen[want] || (command == "init" && !seen["ClusterConfiguration"]) {
		return "", fmt.Errorf("kubeadm configuration is incomplete")
	}
	return output.String(), nil
}
