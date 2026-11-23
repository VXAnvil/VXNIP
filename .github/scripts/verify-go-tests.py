#!/usr/bin/env python3
"""Require real API integration tests instead of accepting skipped envtest runs."""
import argparse
import json
import os
import re
from pathlib import Path

MODULE = "github.com/VXAnvil/cluster-api-provider-vxnip"
REQUIRED = {
    (MODULE + "/cmd/vxnip-controller", "TestManagerResumesAndReleasesClaims"),
    (MODULE + "/internal/controllers", "TestAllocationWithAPIServer"),
    (MODULE + "/internal/controllers", "TestReleasedLegacyHostMigrationWithAPIServer"),
}


def verify(lines):
    results = {}
    cached = set()
    for line in lines:
        event = json.loads(line)
        if event.get("Action") == "output" and not event.get("Test") and re.match(r"^ok\s+.*\s+\(cached\)", event.get("Output", "")):
            cached.add(event["Package"])
        if event.get("Action") in ("pass", "fail", "skip") and event.get("Test"):
            results[(event["Package"], event["Test"])] = event["Action"]
    missing = sorted(f"{package}/{test}: {'cached' if package in cached else results.get((package, test), 'not run')}" for package, test in REQUIRED if results.get((package, test)) != "pass" or package in cached)
    if missing:
        raise ValueError("Required API integration tests did not pass: " + "; ".join(missing))
    print(f"All {len(REQUIRED)} required API integration tests executed and passed")


def check_tools():
    where = os.environ.get("KUBEBUILDER_ASSETS")
    if not where:
        raise ValueError("Set KUBEBUILDER_ASSETS or run make envtest first")
    for binary in ("etcd", "kube-apiserver", "kubectl"):
        path = Path(where) / binary
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f"Missing executable envtest asset: {binary}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?", type=Path)
    parser.add_argument("--check-tools", action="store_true")
    args = parser.parse_args()
    if args.check_tools:
        check_tools()
    elif args.report:
        with args.report.open() as report:
            verify(report)
    else:
        parser.error("supply a report or --check-tools")


if __name__ == "__main__":
    main()
