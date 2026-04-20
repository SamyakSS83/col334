PY=python3

all: run

run:
	$(PY) run_p2.py

experiments:
	$(PY) experiments_p2.py

plot: experiments
	$(PY) plot_p2.py

clean:
	rm -f results_p2.csv p2_plot.png

.PHONY: all run experiments plot clean
