package bootstrap

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/vmetal-lab/cluster-api-provider-vmetal/internal/discovery"
)

type Request struct {
	Action    string     `json:"action"`
	MachineID string     `json:"machineID"`
	OwnerUID  string     `json:"ownerUID"`
	Plan      *Plan      `json:"plan,omitempty"`
	Agent     *AgentPlan `json:"agent,omitempty"`
}

type Runner interface {
	Run(context.Context, string, Request) error
}

type SSH struct{}

func (SSH) Run(ctx context.Context, target string, request Request) error {
	ctx, cancel := context.WithTimeout(ctx, 12*time.Minute)
	defer cancel()
	cmd, err := discovery.SSHCommand(ctx, target, "sudo -n /usr/local/sbin/vmetal-kubeadm")
	if err != nil {
		return err
	}
	data, err := json.Marshal(request)
	if err != nil {
		return err
	}
	cmd.Stdin = bytes.NewReader(data)
	// The kubeadm output can contain credentials. The remote helper keeps it
	// in a root-only log and returns an exit code without echoing the payload.
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("remote bootstrap %s on %s: %w (check the helper and root-only log)", request.Action, target, err)
	}
	return nil
}
