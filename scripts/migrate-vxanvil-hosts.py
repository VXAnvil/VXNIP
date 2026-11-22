#!/usr/bin/env python3
"""Plan and resume migration of released legacy hosts to the VXAnvil inventory."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

OLD_GROUP = "homelab.infrastructure.cluster.x-k8s.io"
OLD_HOSTS = "homelabhosts." + OLD_GROUP
NEW_HOSTS = "vxanvilhosts.vxanvil.infrastructure.cluster.x-k8s.io"
IDENTITY_NAMESPACE = "vxanvil:"  # Protocol v1, frozen independently of display names.


def kubectl(config, *args, body=None):
    result = subprocess.run(
        ["kubectl", "--kubeconfig", config, "--request-timeout=15s", *args],
        input=None if body is None else json.dumps(body), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=45,
    )
    return json.loads(result.stdout) if result.stdout.strip().startswith(("{", "[")) else result.stdout.strip()


def write_plan(path, plan):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        json.dump(plan, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(output.name, path)


def management_uid(config):
    return kubectl(config, "get", "namespace", "kube-system", "-o", "json")["metadata"]["uid"]


def require_released(config):
    resources = kubectl(config, "get", "crd", "-o", "json")["items"]
    for crd in resources:
        group = crd["spec"]["group"]
        kind = crd["spec"]["names"]["kind"]
        if group not in (OLD_GROUP, "infrastructure.cluster.x-k8s.io"):
            continue
        if kind in ("HomelabMachine", "HomelabCluster"):
            objects = kubectl(config, "get", crd["metadata"]["name"], "-A", "-o", "json")["items"]
            if objects:
                raise ValueError("Release the legacy clusters and Machines before host migration.")


def probe(target, expected):
    if not re.fullmatch(r"([a-zA-Z0-9_][a-zA-Z0-9_.-]*@)?[a-zA-Z0-9_][a-zA-Z0-9_.:-]*", target):
        raise ValueError("Invalid SSH target")
    identity = subprocess.check_output(
        ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
         "-o", "ConnectTimeout=8", "--", target, "cat /etc/machine-id"],
        text=True, timeout=20,
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{32}", identity) or identity == "0" * 32:
        raise ValueError("Invalid Linux machine ID")
    if hashlib.sha256(("nvke-homelab:" + identity).encode()).hexdigest() != expected:
        raise ValueError("Legacy identity does not match the trusted SSH host")
    return hashlib.sha256((IDENTITY_NAMESPACE + identity).encode()).hexdigest()


def plan_migration(source, target):
    require_released(source)
    crds = kubectl(source, "get", "crd", "-o", "json")["items"]
    hosts = []
    if any(c["metadata"]["name"] == OLD_HOSTS for c in crds):
        hosts = kubectl(source, "get", OLD_HOSTS, "-o", "json")["items"]
    target_hosts = kubectl(target, "get", NEW_HOSTS, "-o", "json")["items"]
    entries = []
    for host in hosts:
        if host.get("status", {}).get("claim") or host["metadata"].get("deletionTimestamp"):
            raise ValueError("A legacy host is claimed or deleting")
        spec = dict(host["spec"])
        new_id = probe(spec["sshTarget"], spec["machineID"])
        name = "host-" + new_id[:32]
        if any(h["spec"]["machineID"] == new_id for h in target_hosts):
            raise ValueError(f"Target already contains {name}. Resolve its allocation before migrating.")
        spec["machineID"] = new_id
        entries.append({
            "oldName": host["metadata"]["name"], "oldUID": host["metadata"]["uid"],
            "oldSpec": host["spec"], "newName": name, "newSpec": spec,
            "deleted": False, "created": False,
        })
    return {"version": 1, "sourceUID": management_uid(source),
            "targetUID": management_uid(target), "hosts": entries}


def apply_migration(source, target, path, plan):
    if plan["version"] != 1 or plan["sourceUID"] != management_uid(source) or plan["targetUID"] != management_uid(target):
        raise ValueError("Migration plan belongs to another management site or version")
    require_released(source)
    for entry in plan["hosts"]:
        observed = probe(entry["oldSpec"]["sshTarget"], entry["oldSpec"]["machineID"])
        if entry["newSpec"] != dict(entry["oldSpec"], machineID=observed) or entry["newName"] != "host-" + observed[:32]:
            raise ValueError("Migration mapping changed after planning")
        old = kubectl(source, "get", OLD_HOSTS, entry["oldName"], "--ignore-not-found", "-o", "json")
        if old:
            if entry["deleted"] or old["metadata"]["uid"] != entry["oldUID"] or old["spec"] != entry["oldSpec"]:
                raise ValueError("Legacy host changed after planning")
            if old.get("status", {}).get("claim") or old["metadata"].get("deletionTimestamp"):
                # A deleting host from an interrupted attempt can be awaited below.
                if old.get("status", {}).get("claim"):
                    raise ValueError("Legacy host was claimed after planning")
            if not old["metadata"].get("deletionTimestamp"):
                # resourceVersion prevents deleting a host claimed since this read.
                uri = f"/apis/{OLD_GROUP}/v1alpha1/homelabhosts/{entry['oldName']}"
                options = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {
                    "uid": entry["oldUID"], "resourceVersion": old["metadata"]["resourceVersion"]}}
                kubectl(source, "delete", "--raw", uri, "-f", "-", body=options)
            kubectl(source, "wait", "--for=delete", f"{OLD_HOSTS}/{entry['oldName']}", "--timeout=25s")
        entry["deleted"] = True
        write_plan(path, plan)
        # Revalidate the physical identity just before creating the new inventory.
        observed = probe(entry["oldSpec"]["sshTarget"], entry["oldSpec"]["machineID"])
        expected = dict(entry["oldSpec"], machineID=observed)
        if expected != entry["newSpec"] or entry["newName"] != "host-" + observed[:32]:
            raise ValueError("Physical host or migration mapping changed during migration")
        new = kubectl(target, "get", NEW_HOSTS, entry["newName"], "--ignore-not-found", "-o", "json")
        if new:
            if new["spec"] != entry["newSpec"] or new["metadata"].get("annotations", {}).get("vxanvil.nvke.dev/migrated-from-uid") != entry["oldUID"]:
                raise ValueError("Target identity is owned by another registration")
        else:
            obj = {"apiVersion": "vxanvil.infrastructure.cluster.x-k8s.io/v1alpha1",
                   "kind": "VXAnvilHost", "metadata": {
                       "name": entry["newName"], "annotations": {
                           "vxanvil.nvke.dev/migrated-from-uid": entry["oldUID"]}},
                   "spec": entry["newSpec"]}
            kubectl(target, "create", "-f", "-", body=obj)
        entry["created"] = True
        write_plan(path, plan)
        print(f"{entry['oldName']} -> {entry['newName']}")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, help="Legacy management kubeconfig")
    parser.add_argument("--target-kubeconfig")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    target = args.target_kubeconfig or args.kubeconfig
    if args.apply:
        with Path(str(args.plan) + ".lock").open("a") as lock:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            plan = json.loads(args.plan.read_text())
            apply_migration(args.kubeconfig, target, args.plan, plan)
    else:
        if args.plan.exists():
            raise ValueError("Plan already exists. Use another path or resume it with --apply.")
        plan = plan_migration(args.kubeconfig, target)
        with args.plan.open("x") as output:
            json.dump(plan, output, indent=2)
            output.write("\n")
        print(f"{len(plan['hosts'])} released hosts planned. Review {args.plan} before --apply.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        # SSH stderr is already displayed by SSH. Avoid dumping kubeconfig or payloads.
        raise SystemExit(f"Migration stopped: {type(error).__name__}: {error if isinstance(error, ValueError) else 'check the management API, SSH and controller availability'}")
