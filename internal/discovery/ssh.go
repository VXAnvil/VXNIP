package discovery

import (
	"bytes"
	"context"
	_ "embed"
	"encoding/json"
	"fmt"
	"os/exec"
	"regexp"
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
