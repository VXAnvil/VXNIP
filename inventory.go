package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"time"

	"github.com/VXAnvil/VXNIP/internal/discovery"
)

type machine struct {
	Target     string             `json:"ssh_target"`
	State      string             `json:"state"`
	Cluster    string             `json:"cluster,omitempty"`
	ObservedAt time.Time          `json:"observed_at"`
	Hardware   discovery.Hardware `json:"hardware"`
}

type inventory map[string]machine

func (machines inventory) get(name string) (machine, error) {
	entry, exists := machines[name]
	if !exists {
		return entry, fmt.Errorf("machine %q is not registered", name)
	}
	return entry, nil
}

func (machines inventory) observe(name, target string, discovered discovery.Hardware) (machine, error) {
	entry, exists := machines[name]
	if exists && (entry.Target != target || entry.Hardware.ID != discovered.ID) {
		return machine{}, fmt.Errorf("machine %q is registered with a different destination or identity", name)
	}
	for otherName, other := range machines {
		if otherName != name && (other.Target == target || other.Hardware.ID == discovered.ID) {
			return machine{}, fmt.Errorf("machine is already registered as %q", otherName)
		}
	}
	if !exists {
		entry = machine{Target: target, State: "AVAILABLE"}
	}
	entry.Hardware = discovered
	entry.ObservedAt = time.Now().UTC()
	machines[name] = entry
	return entry, nil
}

func (machines inventory) setReservation(name, cluster string, reserve bool) (machine, error) {
	entry, err := machines.get(name)
	if err != nil {
		return entry, err
	}
	if entry.Cluster != "" && entry.Cluster != cluster {
		return entry, fmt.Errorf("machine %q is reserved by cluster %q", name, entry.Cluster)
	}
	entry.State, entry.Cluster = "AVAILABLE", ""
	if reserve {
		entry.State, entry.Cluster = "RESERVED", cluster
	}
	machines[name] = entry
	return entry, nil
}

type store struct{ path string }

func (s store) load() (inventory, error) {
	data, err := os.ReadFile(s.path)
	if errors.Is(err, os.ErrNotExist) {
		return make(inventory), nil
	}
	if err != nil {
		return nil, err
	}
	var machines inventory
	if err := json.Unmarshal(data, &machines); err != nil {
		return nil, fmt.Errorf("read inventory: %w", err)
	}
	if machines == nil {
		return nil, fmt.Errorf("inventory must be a JSON object")
	}
	for name, entry := range machines {
		validState := entry.State == "AVAILABLE" && entry.Cluster == "" ||
			entry.State == "RESERVED" && entry.Cluster != ""
		if !validState || !discovery.ValidTarget(entry.Target) || !discovery.ValidMachineID(entry.Hardware.ID) {
			return nil, fmt.Errorf("invalid stored record for machine %q", name)
		}
	}
	return machines, nil
}

func (s store) update(change func(inventory) error) error {
	dir := filepath.Dir(s.path)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	lock, err := os.OpenFile(s.path+".lock", os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer lock.Close()
	for {
		err = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX)
		if !errors.Is(err, syscall.EINTR) {
			break
		}
	}
	if err != nil {
		return err
	}

	machines, err := s.load()
	if err != nil {
		return err
	}
	if err := change(machines); err != nil {
		return err
	}
	temp, err := os.CreateTemp(dir, ".inventory-*")
	if err != nil {
		return err
	}
	defer os.Remove(temp.Name())
	defer temp.Close()
	encoder := json.NewEncoder(temp)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(machines); err != nil {
		return err
	}
	if err := temp.Sync(); err != nil {
		return err
	}
	if err := temp.Close(); err != nil {
		return err
	}
	return os.Rename(temp.Name(), s.path)
}
