# Archived lab helpers

These optional, environment-specific tools were moved here from `scripts/` and `examples/`. They are kept for manual reproduction and are not included in management bundles. Provider installation, enrollment and recovery do not invoke them.

| File | Previous location | Purpose |
| --- | --- | --- |
| `create-nvke-lima-vm.py` | `scripts/` | Create an Apple Silicon Lima test VM with managed network settings |
| `check-nvke-baremetal-runtime.sh` | `scripts/` | Probe an independent CRI in a private mount namespace |
| `check-nvke-baremetal-ovs.sh` | `scripts/` | Probe an OVS bridge in a private network namespace |
| `install-nvke-lab-spire-forwards.sh` | `scripts/` | Install user port-forward units for the fixed `kind-vxnip-validation` context |
| `spark-plymouth-timeout.conf` | `examples/` | Example systemd override for a Spark Plymouth boot wait |

Use the new `hack/lab/` paths when replaying old experiments. The tool contents are unchanged. Inspect their platform, privilege and environment requirements before manual use; they are not general installation steps.

For supported installation and diagnostic entry points, use [the NVKE guide](../../docs/nvke-quickstart.md) and [operations](../../docs/operations.md).
