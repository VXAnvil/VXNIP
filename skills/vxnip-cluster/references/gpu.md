# Optional NVIDIA GPU workers

When the user requests GPU workers, set `gpu: true` on those bare-metal worker connections. The existing driver and NVIDIA Container Toolkit must work before planning; the executor does not install/upgrade drivers or change shared Docker/containerd or `/etc/nvidia-container-runtime/config.toml`. This path was derived from two ARM64 DGX Spark GB10 machines; CPU-only setup remains unchanged.

Example (use real trusted SSH aliases, not these placeholders):

```json
{
  "manager": {"ssh": "manager"},
  "workers": [
    {"ssh": "spark-one", "gpu": true, "prepare": {"disable_swap": true, "cloud_init": true}},
    {"ssh": "spark-two", "gpu": true, "prepare": {"disable_swap": true, "cloud_init": true}}
  ]
}
```

Preparation flags authorize specific host changes; include them only within the user's authorization. LAN remains the default. Select Tailscale explicitly for remote hosts.

## What the executor configures

Before allocation, each planned GPU worker gets an import into `/etc/vxnip-nvke/containerd.toml`. It enables CDI only for the isolated runtime, keeps the `nvidia` handler and permits only the device-plugin bootstrap annotation `cdi.k8s.io/vxnip-device-plugin`. Generated device descriptions live under that site's private `cdi/` directory rather than the shared CDI output directory.

After fresh Host/Machine/Node binding checks, NVIDIA device plugin v0.20.2 runs only on the selected GPU Nodes. It uses `DEVICE_LIST_STRATEGY=cdi-cri`; the plugin bootstraps device/library access through an explicit CDI annotation. This works with hosts that set `accept-nvidia-visible-devices-envvar-when-unprivileged=false`, without changing that policy or making the plugin privileged.

CDI generation requires the official driver's read-only `/driver-root` mount, `NVIDIA_DRIVER_ROOT=/`, the host `NVIDIA_CDI_HOOK_PATH`, and a writable private output directory mounted at `/var/run/cdi` inside the plugin. Omitting the driver-root mount can let NVML initialize while CDI generation still fails to locate versioned driver libraries.

## Acceptance and workloads

Capacity must match the UUIDs discovered on each physical host. Final verification creates scheduler-selected Pods requesting one `nvidia.com/gpu` each, runs a tiny CUDA Driver API kernel, and compares the returned physical UUIDs against discovery. Multiple GPUs are occupied concurrently so every UUID must appear exactly once. A device-plugin Pod being Running or a Node capacity being nonzero is not sufficient. Probe namespaces are removed by exact UID, and private results retain Node/Pod/image identities.

The preinstalled driver JITs a small PTX kernel in a Python container; no CUDA compiler, model or inference image is installed on the host. The container image IDs are recorded. This is a compute/scheduling check, not NCCL, bandwidth, model, physical reboot or long-term renewal validation.

GPU workloads request `resources.limits["nvidia.com/gpu"]: 1` and tolerate `vxnip.nvke.dev/experiment=true:NoSchedule`. The `nvidia` RuntimeClass is available. Keep the experiment taint; do not remove it merely to make a Pod schedule.

## Sources

- [NVIDIA device-plugin v0.20.2](https://github.com/NVIDIA/k8s-device-plugin/tree/v0.20.2): CDI-CRI allocation and the Helm driver's read-only root/CDI mounts.
- [NVIDIA Toolkit v1.20.2 image device selection](https://github.com/NVIDIA/nvidia-container-toolkit/blob/v1.20.2/internal/config/image/cuda_image.go): explicit annotation requests and the separate unprivileged environment-variable policy.
- [containerd v2.2.5 CRI configuration](https://github.com/containerd/containerd/blob/v2.2.5/docs/cri/config.md): CDI specification directories and runtime annotation selection.
