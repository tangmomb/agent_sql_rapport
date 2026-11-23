UV ?= uv
EVAL_ARGS ?=

.PHONY: setup lint test eval
setup:
	$(UV) run --locked python scripts/setup_db.py

lint:
	$(UV) run --locked ruff check .
	$(UV) run --locked ruff format --check .

test:
	$(UV) run --locked pytest

eval:
	$(UV) run --locked python -m sql_report_agent.evaluation $(EVAL_ARGS)
