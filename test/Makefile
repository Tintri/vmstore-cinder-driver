.PHONY: help test test-unit test-integration test-all clean start stop reset logs install check lint format

# Default target
.DEFAULT_GOAL := help

# Colors for output
BLUE := \033[0;34m
GREEN := \033[0;32m
YELLOW := \033[0;33m
RED := \033[0;31m
NC := \033[0m # No Color

help: ## Show this help message
	@echo "$(BLUE)VMstore Cinder Driver - Test Makefile$(NC)"
	@echo ""
	@echo "$(GREEN)Available targets:$(NC)"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(YELLOW)%-20s$(NC) %s\n", $$1, $$2}'

install: ## Install test dependencies
	@echo "$(BLUE)Installing test dependencies...$(NC)"
	pip install -r requirements.txt

check: ## Check if test environment is ready
	@echo "$(BLUE)Checking test environment...$(NC)"
	@command -v docker >/dev/null 2>&1 || { echo "$(RED)Error: docker is not installed$(NC)" >&2; exit 1; }
	@command -v pytest >/dev/null 2>&1 || { echo "$(RED)Error: pytest is not installed. Run 'make install'$(NC)" >&2; exit 1; }
	@echo "$(GREEN)✓ Test environment is ready$(NC)"

start: ## Start test containers (NFS + WireMock)
	@echo "$(BLUE)Starting test containers...$(NC)"
	@./scripts/start-test-env.sh

stop: ## Stop test containers
	@echo "$(BLUE)Stopping test containers...$(NC)"
	@./scripts/stop-test-env.sh

reset: ## Reset test environment (clean restart)
	@echo "$(BLUE)Resetting test environment...$(NC)"
	@./scripts/reset-test-env.sh

logs: ## View container logs
	@./scripts/view-logs.sh

lint: ## Run linters (flake8, pylint)
	@echo "$(BLUE)Running linters...$(NC)"
	@flake8 ../*.py --count --select=E9,F63,F7,F82 --show-source --statistics || true
	@flake8 ../*.py --count --exit-zero --max-complexity=10 --max-line-length=127 --statistics || true
	@echo "$(GREEN)Linting complete$(NC)"

format: ## Format code with autopep8 (if installed)
	@echo "$(BLUE)Formatting code...$(NC)"
	@command -v autopep8 >/dev/null 2>&1 && autopep8 --in-place --recursive ../*.py || echo "$(YELLOW)autopep8 not installed, skipping$(NC)"

clean: stop ## Clean test artifacts and cache
	@echo "$(BLUE)Cleaning test artifacts...$(NC)"
	find .. -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find .. -type f -name "*.pyc" -delete 2>/dev/null || true
	find .. -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf htmlcov/ .coverage .pytest_cache/ 2>/dev/null || true
	@echo "$(GREEN)Clean complete$(NC)"

clean-all: clean ## Clean everything including Docker volumes
	@echo "$(BLUE)Cleaning everything including volumes...$(NC)"
	docker compose down -v 2>/dev/null || true

status: ## Show status of test containers
	@echo "$(BLUE)Container status:$(NC)"
	@docker compose ps

shell-wiremock: ## Open shell in WireMock container
	@docker compose exec wiremock /bin/sh

shell-nfs: ## Open shell in NFS container
	@docker compose exec nfs-server /bin/sh

# Development helpers
dev-loop: start ## Start development inner loop (start containers + watch for changes)
	@echo "$(GREEN)Environment ready. Make your changes and run 'make test'!$(NC)"

test: ## Run standalone driver validation tests
	@echo "$(BLUE)Running standalone driver validation...$(NC)"
	@python3 scripts/standalone_driver_test.py
	@echo "$(GREEN)✓ Tests complete$(NC)"

test-syntax: ## Validate Python syntax only
	@echo "$(BLUE)Checking Python syntax...$(NC)"
	@python3 -m py_compile ../nfs.py ../api.py ../utils.py ../options.py
	@echo "$(GREEN)✓ Syntax validation complete$(NC)"

validate: test-syntax lint ## Validate code (syntax + linting)
	@echo "$(GREEN)✓ Validation complete$(NC)"
