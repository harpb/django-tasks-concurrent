.PHONY: help install test lint format clean build publish precommit install-hooks uninstall-hooks

help: ## Show this help message
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

install: ## Install dependencies with UV
	uv sync --group dev

test: ## Run tests
	uv run pytest -v

test-cov: ## Run tests with coverage
	uv run pytest --cov=django_tasks_concurrent --cov-report=term-missing

lint: ## Lint code with Ruff
	uv run ruff check .

format: ## Format code with Ruff
	uv run ruff format .
	uv run ruff check --fix .

clean: ## Clean build artifacts
	rm -rf dist/ build/ *.egg-info .pytest_cache .ruff_cache .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

build: clean ## Build package
	uv build

publish: build ## Publish to PyPI
	uv run twine upload dist/*

# ============================================================================
# Pre-commit
# ============================================================================

precommit: ## Run pre-commit checks (auto-fixes and validates code)
	@bash .githooks/pre-commit.sh

# ============================================================================
# Git Hooks Installation
# ============================================================================

install-hooks: ## Install git pre-commit hook
	@echo '#!/bin/sh' > .git/hooks/pre-commit
	@echo 'make precommit' >> .git/hooks/pre-commit
	@chmod +x .git/hooks/pre-commit
	@echo "Git pre-commit hook installed!"

uninstall-hooks: ## Uninstall git pre-commit hook
	@rm -f .git/hooks/pre-commit
	@echo "Git pre-commit hook removed!"

# Default target
.DEFAULT_GOAL := help