package discovery

import (
	"errors"
	"strings"
	"testing"
)

func TestFailureDetailsRedactsRemoteOutput(t *testing.T) {
	for _, tc := range []struct{ input, reason string }{
		{"Permission denied (publickey).", "SSHAuthenticationFailed"},
		{"Host key verification failed.", "SSHHostKeyMismatch"},
		{"context deadline exceeded", "SSHTimeout"},
		{"connect: No route to host", "SSHUnreachable"},
		{"/bin/bash: Permission denied", "RemotePrerequisiteFailed"},
		{"python3: not found", "RemotePrerequisiteFailed"},
		{"decode hardware inventory", "InventoryInvalid"},
	} {
		t.Run(tc.reason, func(t *testing.T) {
			reason, message := FailureDetails(errors.New(tc.input + " secret-123"))
			if reason != tc.reason || strings.Contains(message, "secret-123") {
				t.Fatalf("%s: %s %s", tc.input, reason, message)
			}
		})
	}
}
