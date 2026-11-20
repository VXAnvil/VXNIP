# Contributing to VMetal

Open a pull request against `main`. CI checks formatting, lint, tests and Linux AMD64/ARM64 builds. The final check is named **`gate`**; failed, cancelled, skipped or missing required jobs cannot pass it. CI uses GitHub-hosted runners and does not connect to real hosts or GPUs.

## Run the checks locally

Use Go matching `go.mod`, Python 3, Make and OpenSSH. Tool versions are pinned in `.github/ci-versions.env`; lint tools (including ShellCheck) use release asset digests pinned in `.github/ci-tool-checksums.txt`.

```bash
make tools
make lint
make envtest
make test
make build
make verify-generated
```

`make test` requires envtest and verifies that all three API integration tests actually executed. Set `KUBEBUILDER_ASSETS` to an existing compatible asset directory to use an offline installation. Packaging regression tests run on Linux; on macOS that suite is skipped. Go test output and coverage go to the ignored `.ci-output/` directory. CI retains that output for seven days; there is no coverage percentage gate.

Keep changes focused. Include the problem, resulting behavior and relevant verification in the PR. Fix generated-file drift with `make verify-generated` and commit the intended generated changes. User-facing installation commands are in [the setup guide](docs/nvke-quickstart.md).

## Architecture decisions

Use [ADRs](docs/adrs/README.md) for significant choices about interfaces, dependencies, ownership or lifecycle. Submit the record as `proposed` and discuss it in the PR. Merging a proposed record does not accept the decision. Routine fixes do not need an ADR. Detailed designs can be linked from a record when needed; internal machine-specific experiment logs stay outside the repository.

## Publishing images

Every pushed branch revision continues to publish a multi-platform image under its commit SHA. Version-tag promotion requires a successful `CI` run for the **exact tagged commit**, from a `main` push or an explicit manual CI run in this repository. All four CI jobs must have succeeded. PR-only results, other commits and older green runs followed by a newer failure do not authorize promotion.

For a new release, tag a tested revision containing the CI and release policy workflows. The tag becomes the image tag exactly as pushed, including a numeric tag without `v`. Promotion preserves the source image digest. Previously published images are not modified by installing these checks.

The tag workflow also accepts a manual run with an existing Git tag and `dry_run=true` (the default). This verifies CI without changing registry tags. To test a branch revision, manually run `CI` on that branch first. A commit predating CI cannot be released by the new policy without an eligible CI result. Historical workflow definitions in old Git revisions remain historical; use the current manual workflow when verifying them.

## Enable the main merge requirement (repository owner)

Workflow checks and branch protection are separate. An administrator must enable protection after `gate` has reported once. For a new, unprotected `main`, the owner can run:

```bash
gh api --method PUT repos/xdu31/cluster-api-provider-vmetal/branches/main/protection \
  --input .github/main-protection.json
```

The file requires GitHub Actions' `gate`, an up-to-date branch, one approval and resolved conversations; it also applies to administrators and disables force pushes/deletion. Review existing rules before replacing an existing protection configuration. Private repositories require a GitHub plan supporting protected branches. This repository's visibility is unchanged by the command.
