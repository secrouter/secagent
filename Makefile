.PHONY: help install dev lint type test verify docs-html sbom audit-deps docker docker-base docker-agent docker-analysis ikos-scan analyzer-dotnet analyzer-rust clean

# Prefer an explicit Python 3.11+ interpreter (secagent requires >=3.11); the bare
# `python3` is often an older system build (e.g. 3.9 on macOS). Override with `PY=...`.
PY ?= $(shell command -v python3.11 2>/dev/null || command -v python3.12 2>/dev/null || command -v python3.13 2>/dev/null || command -v python3 2>/dev/null)

help:
	@echo "secagent make targets:"
	@echo "  install   install package (core deps)"
	@echo "  dev       install with all extras + dev tools"
	@echo "  lint      ruff check"
	@echo "  type      mypy"
	@echo "  test      pytest"
	@echo "  verify    lint + type + test + doctor (the CI gate)"
	@echo "  docker    build base + agent images"
	@echo "  docker-analysis  build the optional IKOS analysis image (UC3, heavy)"
	@echo "  ikos-scan        IKOS-scan a C/C++ repo: make ikos-scan REPO=/path [SCAN_OPTS=...]"

install:
	$(PY) -m pip install -e .

dev:
	$(PY) -m pip install -e ".[docs,review,tokenizer,dev]"

lint:
	ruff check src tests

type:
	mypy

test:
	pytest

verify: lint type test
	secagent doctor

docs-html:
	$(PY) -m sphinx -b html docs docs/_build/html
	@echo "built: docs/_build/html/index.html"

# Supply chain (CMMC-5): generate a CycloneDX SBOM + scan dependencies for CVEs.
sbom:
	cyclonedx-py environment -o sbom.json
	@echo "wrote: sbom.json"

audit-deps:
	pip-audit --progress-spinner off

docker: docker-base docker-agent

docker-base:
	docker build -f docker/base.Dockerfile -t secagent-base:latest .

docker-agent: docker-base
	docker build -f docker/agent.Dockerfile -t secagent-agent:latest .

# Opt-in: UC3 C/C++ static-analysis image (IKOS + secagent). Not part of `make docker`.
docker-analysis:
	docker build -f docker/analysis.Dockerfile -t secagent-analysis:latest .

# Run a whole-repo IKOS scan of a C/C++ project via the analysis image (build it first with
# `make docker-analysis`). The project needs a compile_commands.json — build it with
# -DCMAKE_EXPORT_COMPILE_COMMANDS=ON; the scan auto-discovers it under the repo.
#   make ikos-scan REPO=/path/to/code
#   make ikos-scan REPO=/path/to/code SCAN_OPTS="--domain interval --no-pointer -j8"
ikos-scan:
	@test -n "$(REPO)" || { echo "usage: make ikos-scan REPO=/path/to/code [SCAN_OPTS=...]"; exit 2; }
	docker run --rm --user "$$(id -u):$$(id -g)" -e HOME=/tmp \
		-v "$(abspath $(REPO)):/repo" secagent-analysis:latest \
		analyze scan /repo -o /repo/secagent-analysis $(SCAN_OPTS)

# Opt-in: heavy C# analyzer (Roslyn/MSBuild). Used by `secagent analyze deep`. Not part of
# `make docker`. Override the base for a FIPS posture:
#   make analyzer-dotnet DOCKER_BUILD_ARGS="--build-arg DOTNET_SDK=registry.redhat.io/ubi9/dotnet-80"
# See docs/design/heavy-analysis-pipeline.md.
analyzer-dotnet:
	docker build $(DOCKER_BUILD_ARGS) -f docker/analyzer-dotnet.Dockerfile -t secagent-analyzer-dotnet:latest .

# Opt-in: heavy Rust analyzer (rust-analyzer -> SCIP -> secagent-analysis/v1). Used by
# `secagent analyze deep` for Rust. Not part of `make docker`.
analyzer-rust:
	docker build $(DOCKER_BUILD_ARGS) -f docker/analyzer-rust.Dockerfile -t secagent-analyzer-rust:latest .

clean:
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
