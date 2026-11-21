package main

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestVXAnvilReadsExistingInventory(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	dir, err := os.UserConfigDir()
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "vmetal", "inventory.json")
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	entry := `{"legacy-host":{"ssh_target":"ubuntu@worker","state":"RESERVED","cluster":"original-cluster","observed_at":"2026-11-20T00:00:00Z","hardware":{"machine_id_hash":"` + strings.Repeat("a", 64) + `"}}}`
	if err := os.WriteFile(path, []byte(entry), 0600); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	if err := run(context.Background(), []string{"inspect", "legacy-host"}, &output, nil); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(output.String(), "original-cluster") {
		t.Fatal("lost existing reservation", output.String())
	}
	if _, err := os.Stat(filepath.Join(dir, "vxanvil")); !os.IsNotExist(err) {
		t.Fatal("created a separate renamed inventory")
	}
	after, err := os.ReadFile(path)
	if err != nil || string(after) != entry {
		t.Fatal("read-only CLI changed the old inventory")
	}
}
