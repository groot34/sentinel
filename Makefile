.PHONY: setup test baseline run eval clean help serve

help:
	@echo "Sentinel Incident Investigator - Make Targets:"
	@echo "  setup     - Install dependencies"
	@echo "  test      - Run test suite"
	@echo "  baseline  - Run baseline evaluation"
	@echo "  run       - Run advanced Sentinel investigator"
	@echo "  serve     - Start the Sentinel API server (localhost:8000)"
	@echo "  eval      - Run comparative evaluation benchmark"
	@echo "  clean     - Remove temporary and cache files"

setup:
	pip install -r requirements.txt

test:
	pytest tests/ -v

baseline:
	python -m eval.run_eval --mode baseline

run:
	python -m agents.orchestrator incidents/inc_01_n_plus_one_query

serve:
	python -m api

eval:
	python -m eval.run_sentinel_eval

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
