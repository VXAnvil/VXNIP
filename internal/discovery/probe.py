import csv
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess


def read(path):
    return Path(path).read_text().strip()


def nvidia_inventory():
    pci_devices = []
    for device in sorted(Path("/sys/bus/pci/devices").glob("*")):
        try:
            if read(device / "vendor") == "0x10de" and read(device / "class").startswith("0x03"):
                pci_devices.append({"address": device.name, "device_id": read(device / "device")})
        except FileNotFoundError:
            continue

    result = {"pci_devices": pci_devices, "gpus": [], "cuda_driver_max": None}
    if not shutil.which("nvidia-smi"):
        result["status"] = "nvidia-smi unavailable"
        return result
    try:
        query = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader,nounits"],
            check=True, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        result["status"] = "nvidia-smi query failed"
        return result
    rows = list(csv.reader(query.stdout.splitlines(), skipinitialspace=True))
    if any(len(row) != 3 for row in rows):
        result["status"] = "unexpected nvidia-smi output"
        return result
    for name, memory, driver in rows:
        result["gpus"].append({
            "name": name.strip(),
            "vram_mib": int(memory) if memory.strip().isdigit() else None,
            "driver_version": driver.strip(),
        })
    result["status"] = "ok"
    try:
        summary = subprocess.run(
            ["nvidia-smi"], capture_output=True, text=True, timeout=5, check=True,
        )
        version = re.search(r"CUDA Version:\s*([0-9.]+)", summary.stdout)
        if version:
            result["cuda_driver_max"] = version.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def interfaces():
    try:
        output = subprocess.check_output(["ip", "-j", "address", "show"], timeout=5)
        return [
            {"name": interface["ifname"], "addresses": [
                "{}/{}".format(address["local"], address["prefixlen"])
                for address in interface.get("addr_info", [])
            ]}
            for interface in json.loads(output)
        ]
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


# The vxanvil: identity namespace is protocol v1 and stays fixed across renames.
def main():
    identity = read("/etc/machine-id")
    if not re.fullmatch(r"[0-9a-f]{32}", identity) or identity == "0" * 32:
        raise ValueError("a valid /etc/machine-id is required")
    cpu_fields = {}
    for key, _, value in (line.partition(":") for line in read("/proc/cpuinfo").splitlines()):
        cpu_fields.setdefault(key.strip(), value.strip())
    memory = {}
    for line in read("/proc/meminfo").splitlines():
        key, value = line.split(":", 1)
        if key in ("MemTotal", "MemAvailable"):
            memory[key] = int(value.split()[0]) * 1024

    os_release = dict(line.split("=", 1) for line in read("/etc/os-release").splitlines()
                      if line and not line.startswith("#") and "=" in line)
    print(json.dumps({
        "machine_id_hash": hashlib.sha256(("vxanvil:" + identity).encode()).hexdigest(),
        "hostname": platform.node(),
        "os": os_release["PRETTY_NAME"].strip('"'),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "cpu": cpu_fields.get("model name") or cpu_fields.get("Hardware") or platform.machine(),
        "logical_cpus": os.cpu_count(),
        "memory_bytes": memory["MemTotal"],
        "available_memory_bytes": memory.get("MemAvailable", 0),
        "root_free_bytes": shutil.disk_usage("/").free,
        "interfaces": interfaces(),
        "nvidia": nvidia_inventory(),
    }))


if __name__ == "__main__":
    main()
