# Dev tasks for linux-health-check.
#
# This file is for the workstation only — it is never copied to a server. The
# deployment is `healthcheck.py` + `hc/` run by the system python3 with no
# third-party packages; `make deploy-check` proves that still holds.
#
# Run `make` on its own for the list of targets.

.DEFAULT_GOAL := help
.PHONY: help install test lint typecheck check fix text report deploy-check clean clean-all deploy

UV      := uv
PYTHON  := /usr/bin/python3
# The complete set of paths that go to a server. Everything else is dev-only.
RUNTIME := healthcheck.py hc healthcheck.conf.example

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Deploy to a server (explicit host required):"
	@echo "    make deploy HOST=root@yourserver"

install:  ## Install the dev toolchain into .venv
	$(UV) sync

test:  ## Run the test suite
	$(UV) run pytest

lint:  ## Lint with ruff
	$(UV) run ruff check .

typecheck:  ## Type-check with mypy
	$(UV) run mypy .

check: test lint typecheck  ## Run everything: tests, lint, types
	@echo ""
	@echo "  All checks passed."

fix:  ## Apply ruff's safe autofixes
	$(UV) run ruff check . --fix

text:  ## Preview the report in the terminal (sends no email, writes no state)
	$(UV) run python healthcheck.py text

report:  ## Write an HTML preview to reports/preview.html (sends no email)
	@mkdir -p reports
	$(UV) run python healthcheck.py report > reports/preview.html
	@echo "  Wrote reports/preview.html"

deploy-check:  ## Verify the shipping files run on a bare system python3
	@echo "  Copying only $(RUNTIME) to a scratch dir..."
	@rm -rf .deploy-check && mkdir -p .deploy-check
	@cp -r $(RUNTIME) .deploy-check/
	@find .deploy-check -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	@echo "  Importing with $(PYTHON) -S (site-packages disabled)..."
	@cd .deploy-check && $(PYTHON) -S -c "import healthcheck, hc.checks, hc.alerts, hc.report, hc.mailer, hc.bootstrap, hc.crontab"
	@rm -rf .deploy-check
	@echo "  OK — the deployed file set is self-contained and stdlib-only."

clean:  ## Remove tool caches (leaves reports/ and state/ alone)
	rm -rf .pytest_cache .mypy_cache .ruff_cache .deploy-check
	find . -name __pycache__ -type d -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true

clean-all: clean  ## Also remove the dev virtualenv
	rm -rf .venv

deploy:  ## Copy the runtime files to a server (requires HOST=user@host)
	@test -n "$(HOST)" || { \
		echo "  HOST is not set. Usage: make deploy HOST=root@yourserver"; exit 1; }
	@echo "  Deploying $(RUNTIME) to $(HOST):/opt/healthcheck/"
	ssh $(HOST) mkdir -p /opt/healthcheck
	scp -r $(RUNTIME) $(HOST):/opt/healthcheck/
	@echo ""
	@echo "  Copied. On the server, still to do:"
	@echo "    cd /opt/healthcheck && python3 healthcheck.py bootstrap"
	@echo "    cp healthcheck.conf.example healthcheck.conf && \$$EDITOR healthcheck.conf"
	@echo "    python3 healthcheck.py crontab 07:00"
