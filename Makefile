SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
GO ?= go
PYTHON ?= python3
BIN := $(CURDIR)/.ci-bin
export PATH := $(BIN):$(PATH)
KUBEBUILDER_ASSETS ?= $(shell cat .ci-bin/envtest-path 2>/dev/null)
export KUBEBUILDER_ASSETS

.PHONY: tools envtest lint test build verify-generated

tools:
	$(PYTHON) .github/scripts/install-ci-tools.py

envtest:
	mkdir -p "$(BIN)"
	set -euo pipefail; source .github/ci-versions.env; GOBIN="$(BIN)" $(GO) install sigs.k8s.io/controller-runtime/tools/setup-envtest@"$$ENVTEST_TOOL_VERSION"
	set -euo pipefail; source .github/ci-versions.env; "$(BIN)/setup-envtest" use "$$ENVTEST_K8S_VERSION" --bin-dir "$(BIN)/envtest" -p path >"$(BIN)/envtest-path.next"
	mv "$(BIN)/envtest-path.next" "$(BIN)/envtest-path"

lint:
	@set -euo pipefail; files=$$(git ls-files '*.go' | xargs gofmt -l); if [[ -n $$files ]]; then printf 'Run gofmt on:\n%s\n' "$$files"; exit 1; fi
	"$(BIN)/golangci-lint" config verify
	"$(BIN)/golangci-lint" run --timeout=5m
	"$(BIN)/actionlint" -shellcheck="$(BIN)/shellcheck"
	@set -euo pipefail; while IFS= read -r -d '' file; do bash -n "$$file"; done < <(find scripts .github -type f -name '*.sh' -print0)
	$(PYTHON) -c 'import ast; from pathlib import Path; [ast.parse(p.read_text(),filename=str(p)) for d in ("scripts", ".github") for p in Path(d).rglob("*.py")]'

test:
	$(PYTHON) .github/scripts/verify-go-tests.py --check-tools
	mkdir -p .ci-output
	PATH="$$KUBEBUILDER_ASSETS:$$PATH" bash -o pipefail -c '$(GO) test -race -json -coverprofile=.ci-output/coverage.out ./... | tee .ci-output/go-test.jsonl'
	$(PYTHON) .github/scripts/verify-go-tests.py .ci-output/go-test.jsonl
	$(PYTHON) scripts/test_vmetal_kubeadm.py
	$(PYTHON) scripts/test_management_tools.py
	$(PYTHON) scripts/test_management_bundle.py
	$(PYTHON) -m unittest discover -s .github/tests -p 'test_*.py' -v

build:
	$(GO) build ./...
	@set -euo pipefail; output=$$(mktemp -d); trap 'rm -rf -- "$$output"' EXIT; for arch in amd64 arm64; do \
	  CGO_ENABLED=0 GOOS=linux GOARCH=$$arch $(GO) build -trimpath -o "$$output/$$arch/vmetal" .; \
	  CGO_ENABLED=0 GOOS=linux GOARCH=$$arch $(GO) build -trimpath -o "$$output/$$arch/vmetal-controller" ./cmd/vmetal-controller; \
	  echo "Built Linux $$arch CLI and controller"; \
	done

verify-generated:
	bash scripts/generate-crds.sh
	git diff --exit-code -- api/v1alpha1/zz_generated.deepcopy.go config/crd
