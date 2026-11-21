package main

import (
	"bytes"
	"context"
	"errors"
	"path/filepath"
	"strings"
	"testing"

	"github.com/VXAnvil/cluster-api-provider-vxanvil/internal/discovery"
)

type mockDiscovery func(context.Context, string) (discovery.Hardware, error)

func (m mockDiscovery) Discover(ctx context.Context, target string) (discovery.Hardware, error) {
	return m(ctx, target)
}

func TestMachineLifecycle(t *testing.T) {
	path := filepath.Join(t.TempDir(), "inventory.json")
	id := strings.Repeat("a", 64)
	backend := mockDiscovery(func(_ context.Context, target string) (discovery.Hardware, error) {
		return discovery.Hardware{ID: id, Hostname: target}, nil
	})
	invoke := func(wantError string, args ...string) string {
		t.Helper()
		var out bytes.Buffer
		err := run(context.Background(), append([]string{"-state", path}, args...), &out, backend)
		if wantError == "" && err != nil {
			t.Fatal(err)
		}
		if wantError != "" && (err == nil || !strings.Contains(err.Error(), wantError)) {
			t.Fatalf("got %v, want error containing %q", err, wantError)
		}
		return out.String()
	}
	invoke("", "register", "desktop", "ubuntu@desktop")
	invoke("already registered", "register", "duplicate", "another-alias")
	invoke("", "reserve", "desktop", "lab")
	invoke("", "reserve", "desktop", "lab")
	invoke("reserved by cluster", "reserve", "desktop", "other")
	invoke("reserved by cluster", "release", "desktop", "other")
	invoke("", "refresh", "desktop")
	if got := invoke("", "inspect", "desktop"); !strings.Contains(got, `"cluster": "lab"`) {
		t.Fatalf("refresh lost reservation: %s", got)
	}
	id = strings.Repeat("b", 64)
	invoke("different destination or identity", "refresh", "desktop")
	invoke("", "release", "desktop", "lab")
	invoke("", "release", "desktop", "lab")
	if got := invoke("", "list"); !strings.Contains(got, `"state": "AVAILABLE"`) ||
		!strings.Contains(got, strings.Repeat("a", 64)) || strings.Contains(got, `"cluster"`) {
		t.Fatalf("unexpected persisted inventory: %s", got)
	}
}

func TestFailedDiscoveryLeavesNoRecord(t *testing.T) {
	state := store{path: filepath.Join(t.TempDir(), "inventory.json")}
	offline := errors.New("host unreachable")
	backend := mockDiscovery(func(context.Context, string) (discovery.Hardware, error) {
		return discovery.Hardware{}, offline
	})
	_, err := observe(context.Background(), state, backend, "desktop", "ubuntu@desktop")
	if !errors.Is(err, offline) {
		t.Fatalf("got %v, want discovery error", err)
	}
	machines, err := state.load()
	if err != nil || len(machines) != 0 {
		t.Fatalf("got %v, %v, want an empty inventory", machines, err)
	}
}

func TestConcurrentReservations(t *testing.T) {
	state := store{path: filepath.Join(t.TempDir(), "inventory.json")}
	if err := state.update(func(machines inventory) error {
		_, err := machines.observe("desktop", "desktop", discovery.Hardware{ID: strings.Repeat("a", 64)})
		return err
	}); err != nil {
		t.Fatal(err)
	}

	start := make(chan struct{})
	results := make(chan error, 2)
	for _, cluster := range []string{"first", "second"} {
		go func() {
			<-start
			_, err := execute(context.Background(), state, nil, "reserve", []string{"desktop", cluster})
			results <- err
		}()
	}
	close(start)
	first, second := <-results, <-results
	if (first == nil) == (second == nil) {
		t.Fatalf("expected exactly one reservation to succeed, got %v and %v", first, second)
	}
	machines, err := state.load()
	if err != nil || machines["desktop"].State != "RESERVED" {
		t.Fatalf("got %v, %v, want a persisted reservation", machines, err)
	}
}
