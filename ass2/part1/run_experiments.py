# STARTER CODE ONLY. EDIT AS DESIRED
#!/usr/bin/env python3
import re
import time
import csv
from pathlib import Path
from topo_wordcount import make_net
import json, os, pathlib

# Config
K_VALUES = [1, 2, 5, 10, 20, 50, 100]   
RUNS_PER_K = 5
SERVER_CMD = "./bin/server --config config.json"
CLIENT_CMD_TMPL = "./bin/client --config config.json --quiet"

RESULTS_CSV = Path("results.csv")

def modify_config(key, value):
    cfg = json.loads(pathlib.Path("config.json").read_text())
    cfg[key] = value
    pathlib.Path("config.json").write_text(json.dumps(cfg))

def main():
    # Prepare CSV
    if not RESULTS_CSV.exists():
        with RESULTS_CSV.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["k", "run", "elapsed_ms"])

    net = make_net()
    net.start()

    h1 = net.get('h1')  # client
    h2 = net.get('h2')  # server

    # Ensure words.txt exists (shared FS)
    if not Path("words.txt").exists():
        Path("words.txt").write_text("cat,bat,cat,dog,dog,emu,emu,emu,ant\n")

    # Start server
    srv = h2.popen(SERVER_CMD, shell=True, stdout=None, stderr=None)
    time.sleep(0.5)  # give it a moment to bind

    try:
        for k in K_VALUES:
            for r in range(1, RUNS_PER_K + 1):
                modify_config("k", k) # should implement this function
                cmd = CLIENT_CMD_TMPL
                out = h1.cmd(cmd)
                # parse ELAPSED_MS
                m = re.search(r"ELAPSED_MS:(\d+)", out)
                if not m:
                    print(f"[warn] No ELAPSED_MS found for k={k} run={r}. Raw:\n{out}")
                    continue
                ms = int(m.group(1))
                with RESULTS_CSV.open("a", newline="") as f:
                    csv.writer(f).writerow([k, r, ms])
                print(f"k={k} run={r} elapsed_ms={ms}")
    finally:
        srv.terminate()
        time.sleep(0.2)
        net.stop()

if __name__ == "__main__":
    main()
