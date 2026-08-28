.PHONY: setup test baseline run eval clean help

help:
	@echo "Sentinel Incident Investigator - Make Targets:"
	@echo "  setup     - Install dependencies"
	@echo "  test      - Run test suite"
	@echo "  baseline  - Run baseline evaluation"
	@echo "  run       - Run advanced Sentinel investigator"
	@echo "  eval      - Run comparative evaluation benchmark"
	@echo "  clean     - Remove temporary and cache files"

setup:
	pip install -r requirements.txt

test:
	pytest tests/ -v

baseline:
	python eval/run_eval.py --mode baseline

run:
	python agents/orchestrator.py

eval:
	python eval/run_eval.py --mode compare

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
