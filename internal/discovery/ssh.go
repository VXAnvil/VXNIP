package discovery

import (
	"bytes"
	"context"
	_ "embed"
	"encoding/json"
	"fmt"
	"os/exec"
	"regexp"
	"strings"
	"time"
)

//go:embed probe.py
var probe string

type Hardware struct {
	ID           string          `json:"machine_id_hash"`
	Hostname     string          `json:"hostname"`
	OS           string          `json:"os"`
	Kernel       string          `json:"kernel"`
	Architecture string          `json:"architecture"`
	CPU          string          `json:"cpu"`
	LogicalCPUs  int             `json:"logical_cpus"`
	MemoryBytes  uint64          `json:"memory_bytes"`
	FreeRAMBytes uint64          `json:"available_memory_bytes"`
	RootFree     uint64          `json:"root_free_bytes"`
	Interfaces   json.RawMessage `json:"interfaces"`
	NVIDIA       json.RawMessage `json:"nvidia"`
}

type Discoverer interface {
	Discover(context.Context, string) (Hardware, error)
}

type SSH struct{}

var (
	destination = regexp.MustCompile(`^([a-zA-Z0-9_][a-zA-Z0-9_.-]*@)?[a-zA-Z0-9_][a-zA-Z0-9_.:-]*$`)
	machineID   = regexp.MustCompile(`^[a-f0-9]{64}$`)
)

func ValidTarget(target string) bool { return destination.MatchString(target) }

func ValidMachineID(id string) bool { return machineID.MatchString(id) }

func SSHCommand(ctx context.Context, target, command string) (*exec.Cmd, error) {
	if !ValidTarget(target) {
		return nil, fmt.Errorf("invalid SSH destination %q", target)
	}
	return exec.CommandContext(ctx, "ssh", "-T", "-o", "BatchMode=yes",
		"-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8",
		"-o", "ClearAllForwardings=yes", "--", target, command), nil
}

func (SSH) Discover(ctx context.Context, target string) (Hardware, error) {
	var result Hardware
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	cmd, err := SSHCommand(ctx, target, "python3 -")
	if err != nil {
		return result, err
	}
	cmd.Stdin = bytes.NewBufferString(probe)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	output, err := cmd.Output()
	if ctx.Err() != nil {
		return result, fmt.Errorf("discovery of %s: %w", target, ctx.Err())
	}
	if err != nil {
		return result, fmt.Errorf("discovery of %s: %w: %s", target, err, bytes.TrimSpace(stderr.Bytes()))
	}
	if err := json.Unmarshal(output, &result); err != nil {
		return result, fmt.Errorf("decode hardware inventory from %s: %w", target, err)
	}
	if !ValidMachineID(result.ID) || result.OS == "" || result.LogicalCPUs < 1 {
		return result, fmt.Errorf("incomplete hardware inventory from %s", target)
	}
	return result, nil
}

// FailureDetails classifies probe failures without publishing remote stderr,
// which can contain operator-specific data.
func FailureDetails(err error) (string, string) {
	message := strings.ToLower(err.Error())
	switch {
	case strings.Contains(message, "host key verification failed"),
		strings.Contains(message, "remote host identification has changed"):
		return "SSHHostKeyMismatch", "Verify the host fingerprint and update the Provider known_hosts entry through a trusted channel."
	case strings.Contains(message, "permission denied ("),
		strings.Contains(message, "no supported authentication methods"):
		return "SSHAuthenticationFailed", "Authorize the Provider public key for the registered SSH user and verify the configured identity file."
	case strings.Contains(message, "deadline exceeded"), strings.Contains(message, "timed out"):
		return "SSHTimeout", "Check the SSH route, firewall and node availability."
	case strings.Contains(message, "no route to host"), strings.Contains(message, "connection refused"),
		strings.Contains(message, "could not resolve hostname"), strings.Contains(message, "network is unreachable"):
		return "SSHUnreachable", "Check the registered address, DNS, routing and SSH listener."
	case strings.Contains(message, "permission denied"), strings.Contains(message, "not found"):
		return "RemotePrerequisiteFailed", "Check Python 3 and execute permissions for the SSH user, including /usr/bin."
	case strings.Contains(message, "decode hardware"), strings.Contains(message, "incomplete hardware"):
		return "InventoryInvalid", "The SSH probe returned an invalid inventory. Check Python 3 and shell startup output."
	default:
		return "ProbeFailed", "SSH discovery failed. Check the Provider journal and verify the registered SSH target."
	}
}
