PORT ?= 8080

.PHONY: install run selftest sim

install:
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

run:
	.venv/bin/uvicorn bot.app:app --host 0.0.0.0 --port $(PORT)

selftest:
	.venv/bin/python selftest.py

# Run the official judge against a running bot (needs an LLM key inside judge_simulator.py).
sim:
	.venv/bin/python judge_simulator.py
