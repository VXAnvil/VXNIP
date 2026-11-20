#!/usr/bin/env python3
"""Fail closed when required CI jobs or an exact release revision lack approval."""
import argparse
import json
import os
import re
import sys
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CI_JOBS = frozenset(("lint", "test", "build"))
RELEASE_JOBS = CI_JOBS | {"gate"}


class PolicyError(ValueError):
    pass


def require_gate(needs):
    if set(needs) != CI_JOBS:
        raise PolicyError("The gate must depend on lint, test and build")
    results = {name: value.get("result") for name, value in needs.items()}
    if any(value != "success" for value in results.values()):
        raise PolicyError("Required CI jobs did not succeed: " + json.dumps(results, sort_keys=True))
    return results


def latest_trusted_run(runs, repository, commit):
    trusted = [run for run in runs if
               run.get("head_sha") == commit and
               (run.get("head_repository") or {}).get("full_name", "").lower() == repository.lower() and
               (run.get("event") == "workflow_dispatch" or
                (run.get("event") == "push" and run.get("head_branch") == "main"))]
    return max(trusted, key=lambda run: (run["id"], run.get("run_attempt", 1))) if trusted else None


def require_release_jobs(jobs):
    latest = {}
    for job in jobs:
        name = job["name"]
        order = (job.get("run_attempt", 1), job["id"])
        if name not in latest or order > latest[name][0]:
            latest[name] = (order, job)
    bad = sorted(name for name in RELEASE_JOBS if name not in latest or
                 latest[name][1].get("status") != "completed" or
                 latest[name][1].get("conclusion") != "success")
    if bad:
        raise PolicyError("The CI run did not pass every required job: " + ", ".join(bad))


class GitHub:
    def __init__(self, token, base):
        self.token = token
        self.base = base.rstrip("/")

    def collection(self, path, key):
        items = []
        separator = "&" if "?" in path else "?"
        for page in range(1, 101):
            request = Request(f"{self.base}{path}{separator}per_page=100&page={page}", headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "User-Agent": "vmetal-ci-policy",
            })
            with urlopen(request, timeout=30) as response:
                batch = json.load(response)[key]
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise PolicyError("GitHub pagination limit exceeded")

    def runs(self, repository, commit):
        query = urlencode({"head_sha": commit})
        return self.collection(f"/repos/{repository}/actions/workflows/ci.yml/runs?{query}", "workflow_runs")

    def jobs(self, repository, run):
        return self.collection(f"/repos/{repository}/actions/runs/{run['id']}/jobs?filter=all", "jobs")


def wait_for_release(api, repository, commit, timeout=900, interval=15,
                     clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + timeout
    while True:
        run = latest_trusted_run(api.runs(repository, commit), repository, commit)
        if run and run.get("status") == "completed":
            if run.get("conclusion") != "success":
                raise PolicyError(f"CI run {run['id']} concluded {run.get('conclusion')}; release denied")
            require_release_jobs(api.jobs(repository, run))
            return run
        if clock() >= deadline:
            raise PolicyError(f"No successful main/manual CI run for exact commit {commit}; release denied")
        print(f"Waiting for CI on {commit}", flush=True)
        sleep(max(0, min(interval, deadline - clock())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("gate")
    release = commands.add_parser("release")
    release.add_argument("--commit", required=True)
    release.add_argument("--timeout", type=int, default=900)
    release.add_argument("--interval", type=int, default=15)
    args = parser.parse_args()
    if args.command == "gate":
        results = require_gate(json.loads(os.environ["NEEDS_JSON"]))
        print("CI gate passed: " + json.dumps(results, sort_keys=True))
        return
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GH_TOKEN", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not token:
        raise PolicyError("GITHUB_REPOSITORY and GH_TOKEN are required")
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit) or args.timeout < 0 or args.interval <= 0:
        raise PolicyError("Use a full commit SHA and non-negative timeout with a positive interval")
    api = GitHub(token, os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    run = wait_for_release(api, repository, args.commit, args.timeout, args.interval)
    link = run["html_url"]
    print(f"Release CI approved: {args.commit} ({link})")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(f"Verified CI for `{args.commit}`: [run {run['id']}]({link}).\n")


if __name__ == "__main__":
    try:
        main()
    except (PolicyError, URLError, KeyError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
