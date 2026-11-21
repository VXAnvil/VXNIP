package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"strings"

	"github.com/VXAnvil/cluster-api-provider-vxanvil/internal/discovery"
)

const usage = `Usage: vxanvil [-state PATH] COMMAND [ARGS]

  register NAME [USER@]HOST   Discover and register a Linux machine
  list                        Show all registered machines
  inspect NAME                Show the last successful hardware observation
  refresh NAME                Repeat SSH discovery for a registered machine
  reserve NAME CLUSTER        Reserve an available machine for a cluster
  release NAME CLUSTER        Release a reservation held by that cluster

Commands return JSON. SSH aliases from your OpenSSH config are supported.
`

func run(ctx context.Context, args []string, out io.Writer, backend discovery.Discoverer) error {
	flags := flag.NewFlagSet("vxanvil", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	path := flags.String("state", "", "inventory path")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			_, err := io.WriteString(out, usage)
			return err
		}
		return err
	}
	args = flags.Args()
	if len(args) == 0 {
		return errors.New("a command is required")
	}
	command, args := args[0], args[1:]
	counts := map[string]int{"list": 0, "inspect": 1, "refresh": 1, "register": 2, "reserve": 2, "release": 2}
	count, known := counts[command]
	if !known || len(args) != count {
		return fmt.Errorf("invalid command or argument count for %q", command)
	}
	for _, arg := range args {
		if strings.TrimSpace(arg) == "" || strings.TrimSpace(arg) != arg {
			return errors.New("arguments must be nonempty without leading or trailing whitespace")
		}
	}
	if *path == "" {
		dir, err := os.UserConfigDir()
		if err != nil {
			return err
		}
		*path = filepath.Join(dir, "vmetal", "inventory.json")
	}
	state := store{path: filepath.Clean(*path)}
	result, err := execute(ctx, state, backend, command, args)
	if err != nil {
		return err
	}
	encoder := json.NewEncoder(out)
	encoder.SetIndent("", "  ")
	return encoder.Encode(result)
}

func execute(ctx context.Context, state store, backend discovery.Discoverer, command string, args []string) (any, error) {
	switch command {
	case "list":
		return state.load()
	case "inspect", "refresh":
		machines, err := state.load()
		if err != nil {
			return nil, err
		}
		entry, err := machines.get(args[0])
		if err != nil || command == "inspect" {
			return entry, err
		}
		return observe(ctx, state, backend, args[0], entry.Target)
	case "register":
		return observe(ctx, state, backend, args[0], args[1])
	case "reserve", "release":
		var result machine
		err := state.update(func(machines inventory) error {
			var err error
			result, err = machines.setReservation(args[0], args[1], command == "reserve")
			return err
		})
		return result, err
	default:
		return nil, fmt.Errorf("unknown command %q", command)
	}
}

func observe(ctx context.Context, state store, backend discovery.Discoverer, name, target string) (machine, error) {
	var result machine
	discovered, err := backend.Discover(ctx, target)
	if err != nil {
		return result, err
	}
	err = state.update(func(machines inventory) error {
		var err error
		result, err = machines.observe(name, target, discovered)
		return err
	})
	return result, err
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	if err := run(ctx, os.Args[1:], os.Stdout, discovery.SSH{}); err != nil {
		fmt.Fprintln(os.Stderr, "vmetal:", err)
		fmt.Fprintln(os.Stderr, "See vxanvil -h for usage.")
		os.Exit(1)
	}
}
