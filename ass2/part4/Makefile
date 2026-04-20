.PHONY: run-fcfs plot ensure_logs clean lint

run-rr: ensure_logs
	sudo PYTHONNOUSERSITE=1 python3 runner.py --mode fcfs --debug-detail

plot: ensure_logs
	sudo PYTHONNOUSERSITE=1 python3 runner.py --mode plot

ensure_logs:
	mkdir -p logs

lint:
	python3 -m py_compile server.py client.py runner.py topology.py
