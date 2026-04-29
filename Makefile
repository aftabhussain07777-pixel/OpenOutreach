.DEFAULT_GOAL := help
.PHONY: help logs test docker-test stop build up up-view install setup run admin view clean reset

help:
	@perl -nle'print $& if m{^[a-zA-Z_-]+:.*?## .*$$}' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-25s\033[0m %s\n", $$1, $$2}'

install: ## install all Python dependencies (local dev)
	pip install uv 2>/dev/null || true
	uv pip install -r requirements/local.txt

setup: install ## install deps + Playwright browsers + migrate + bootstrap CRM
	playwright install --with-deps chromium
	python manage.py migrate --no-input
	python manage.py setup_crm

run: ## run the daemon
	python manage.py rundaemon

test: ## run the test suite
	.venv/bin/pytest

admin: ## start the Django Admin web server
	@echo ""
	@echo "  Django Admin: http://localhost:8000/admin/"
	@echo "  No superuser yet? Run: python manage.py createsuperuser"
	@echo ""
	python manage.py runserver

# Docker targets
logs: ## follow the logs of the service
	docker compose -f local.yml logs -f

docker-test: ## run tests in Docker
	docker compose -f local.yml run --remove-orphans app py.test -vv -p no:cacheprovider

stop: ## stop all services defined in Docker Compose
	docker compose -f local.yml stop

build: ## build all services defined in Docker Compose
	docker compose -f local.yml build

up: ## run the defined service in Docker Compose
	docker compose -f local.yml up --build -d
	docker compose -f local.yml logs -f

up-view: ## run the defined service in Docker Compose and open vinagre
	docker compose -f local.yml up --build -d
	sleep 3
	$(MAKE) view
	docker compose -f local.yml logs -f app

view: ## open vinagre to view the app
	@sh -c 'vinagre vnc://127.0.0.1:5900 > /dev/null 2>&1 &'

clean: ## clean up temporary files and cache
	@echo "Cleaning temporary files and cache..."
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@find . -type f -name "*.pyo" -delete 2>/dev/null || true
	@find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .cache 2>/dev/null || true
	@rm -rf /tmp/openoutreach-diagnostics 2>/dev/null || true
	@echo "Clean complete."

reset: clean ## reset database and migrations (WARNING: deletes all data)
	@echo "WARNING: This will delete all data in the database."
	@read -p "Are you sure? [y/N] " -n 1 -r; \
	echo; \
	if [[ $$REPLY =~ ^[Yy]$$ ]]; then \
		rm -f db.sqlite3; \
		find linkedin/migrations -name "*.py" ! -name "__init__.py" -delete; \
		find crm/migrations -name "*.py" ! -name "__init__.py" -delete; \
		find chat/migrations -name "*.py" ! -name "__init__.py" -delete; \
		rm -rf linkedin/migrations/__pycache__ crm/migrations/__pycache__ chat/migrations/__pycache__; \
		echo "Database and migrations removed. Run 'make setup' to reinitialize."; \
	else \
		echo "Reset cancelled."; \
	fi
