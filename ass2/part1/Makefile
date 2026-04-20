CXX = g++
# add src to include path so our local json_parser.h is found
CXXFLAGS = -std=c++17 -I./src -Wall -O2

# Default target
all: build

# Build target
build: bin server client 

bin:
	mkdir -p bin

server: src/server.cpp
	$(CXX) $(CXXFLAGS) -o bin/server src/server.cpp 

client: src/client.cpp
	$(CXX) $(CXXFLAGS) -o bin/client src/client.cpp

# Run a single iteration
run: build
	python3 demo_runner.py

# Run experiments and generate plot
plot: build
	python3 run_experiments.py
	python3 plot_results.py

# Clean build artifacts
clean:
	rm -rf bin/

.PHONY: all build run plot clean
