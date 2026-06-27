PORT ?= 8080

.PHONY: install run selftest sim

install:
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

run:
	.venv/bin/uvicorn bot.app:app --host 0.0.0.0 --port $(PORT)

selftest:
	.venv/bin/python selftest.py

# Self-score with the challenge-provided judge_simulator.py (drop it in the repo root first;
# it is not included here, and it needs an LLM key set inside the file).
sim:
	.venv/bin/python judge_simulator.py
