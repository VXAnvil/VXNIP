#!/usr/bin/env python3
"""Install pinned, checksum-verified lint tools without system packages."""
import hashlib
import io
import os
from pathlib import Path
import platform
import shutil
import tarfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]


def install(name, version, system, arch, destination):
    if name == "actionlint":
        repository = "rhysd/actionlint"
        archive = f"actionlint_{version}_{system}_{arch}.tar.gz"
        checksums = f"actionlint_{version}_checksums.txt"
    else:
        repository = "golangci/golangci-lint"
        archive = f"golangci-lint-{version}-{system}-{arch}.tar.gz"
        checksums = f"golangci-lint-{version}-checksums.txt"
    base = f"https://github.com/{repository}/releases/download/v{version}"
    with urlopen(f"{base}/{checksums}", timeout=90) as response:
        rows = [line.split() for line in response.read().decode().splitlines()]
    expected = [row[0] for row in rows if len(row) == 2 and row[1].lstrip("*") == archive]
    if len(expected) != 1:
        raise ValueError(f"Missing or ambiguous checksum for {archive}")
    with urlopen(f"{base}/{archive}", timeout=180) as response:
        content = response.read()
    if hashlib.sha256(content).hexdigest() != expected[0]:
        raise ValueError(f"Checksum mismatch for {archive}")
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as package:
        files = [m for m in package.getmembers() if m.isfile() and Path(m.name).name == name]
        if len(files) != 1:
            raise ValueError(f"Expected exactly one {name} binary")
        temporary = destination / (name + ".next")
        with package.extractfile(files[0]) as source, temporary.open("wb") as output:
            shutil.copyfileobj(source, output)
        temporary.chmod(0o755)
        os.replace(temporary, destination / name)
    print(f"Installed {name} {version} ({system}/{arch})")


def main():
    versions = dict(line.split("=", 1) for line in (ROOT / ".github/ci-versions.env").read_text().splitlines() if line and not line.startswith("#"))
    system = platform.system().lower()
    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    if system not in ("linux", "darwin") or not arch:
        raise ValueError("Lint tools support Linux/macOS AMD64 and ARM64")
    destination = ROOT / ".ci-bin"
    destination.mkdir(exist_ok=True)
    for name, key in (("golangci-lint", "GOLANGCI_LINT_VERSION"), ("actionlint", "ACTIONLINT_VERSION")):
        install(name, versions[key], system, arch, destination)


if __name__ == "__main__":
    main()
