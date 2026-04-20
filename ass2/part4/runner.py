#!/usr/bin/env python3

import json
import os
import time
import glob
import numpy as np
import sys
import csv
import matplotlib.pyplot as plt
from statistics import median
from topology import create_network

class Runner:
    def __init__(self, config_file='config.json'):
        with open(config_file, 'r') as f:
            self.config = json.load(f)
        
        self.server_ip   = self.config['server_ip']
        self.port        = self.config['port']
        self.num_clients = self.config['num_clients']
        self.c           = self.config['c']
        self.p           = self.config['p']
        self.k           = self.config['k']
        self.trials      = int(self.config.get('trials', 1))

        self.debug_detail = ("--debug-detail" in sys.argv)
        
        print(f"Config: {self.num_clients} clients, c={self.c}, p={self.p}, k={self.k}")
    
    def cleanup_logs(self):
        os.makedirs("logs", exist_ok=True)
        for path in glob.glob("logs/*.log"):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        print("Cleaned old logs")
    
    def parse_logs(self):
        out = {"rogue": [], "normal": []}
        os.makedirs("logs", exist_ok=True)
        for path in glob.glob("logs/*.log"):
            name = os.path.basename(path)
            total_ms = None
            try:
                with open(path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("TOTAL_MS"):
                            parts = line.split()
                            if len(parts) == 2:
                                try:
                                    total_ms = int(parts[1])
                                except ValueError:
                                    pass
            except Exception as e:
                print(f"Warning: failed to parse {path}: {e}")
            if total_ms is None:
                continue
            if "rogue" in name:
                out["rogue"].append(total_ms)
            else:
                out["normal"].append(total_ms)
        return out
    
    def calculate_jfi(self, completion_times):
        """
        Calculate Jain's Fairness Index on utilities u_i = 1 / completion_time_i (ms).
        JFI = (sum u)^2 / (n * sum u^2)
        """
        utils = []
        any_zero = False
        for group in completion_times.values():
            for t in group:
                if t == 0:
                    any_zero = True
                if t and t > 0:
                    utils.append(1.0 / t)
        if not utils:
            return 0.0
        s1 = sum(utils)
        s2 = sum(u*u for u in utils)
        n  = len(utils)
        if any_zero:
            return 1 / (n + 1)
        return (s1 * s1) / (n * s2)

    def _median_across_trials(self, results_list):
        out = {}
        for key in ("rogue", "normal"):
            trial_lists = [res.get(key, []) for res in results_list if isinstance(res.get(key, []), list)]
            if not trial_lists:
                out[key] = []
                continue
            L = min(len(lst) for lst in trial_lists)
            med_list = []
            for idx in range(L):
                vals = [lst[idx] for lst in trial_lists if len(lst) > idx]
                vals = [v for v in vals if isinstance(v, (int, float))]
                if vals:
                    med_list.append(int(median(vals)))
            out[key] = med_list
        return out

    def run_experiment(self, c_value):
        dbg = " --debug-detail" if self.debug_detail else ""
        print(f"Running experiment with c={c_value}")
        self.cleanup_logs()
        net = create_network(num_clients=self.num_clients)
        try:
            server  = net.get('server')
            clients = [net.get(f'client{i+1}') for i in range(self.num_clients)]
            
            print("Starting server...")
            server_proc = server.popen("python3 server.py")
            time.sleep(3)
            
            print("Starting clients...")
            rogue_proc = clients[0].popen(f"python3 client.py --batch-size {c_value} --client-id rogue{dbg}")
            normal_procs = []
            for i in range(1, self.num_clients):
                p = clients[i].popen(f"python3 client.py --batch-size 1 --client-id normal_{i+1}{dbg}")
                normal_procs.append(p)
            
            rogue_proc.wait()
            for p in normal_procs:
                p.wait()
            
            server_proc.terminate()
            server_proc.wait()
            time.sleep(1)
            
            results = self.parse_logs()
            return results
        finally:
            net.stop()
    
    def run_varying_c(self):
        c_values = list(range(1, 21))
        print("Running experiments with varying c values...")
        collected = []
        
        for c in c_values:
            print(f"\n--- Testing c = {c} ---")
            trial_results = []
            for t in range(self.trials):
                print(f"  Trial {t+1}/{self.trials}")
                res = self.run_experiment(c)
                trial_results.append(res)

            agg = self._median_across_trials(trial_results)
            jfi = self.calculate_jfi(agg)

            rogue_ms = float(agg["rogue"][0]) if agg.get("rogue") else float("nan")

            print(f"Aggregated (median) results for c={c}: {agg}")
            print(f"JFI (median-aggregated) for c={c}: {jfi:.6f}")

            collected.append((c, jfi, rogue_ms))
            print(f"Experiment with c={c} completed")
        
        print("All experiments completed")
        self.plot_jfi_vs_c(collected)

        try:
            with open("results_jfi.csv", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["c", "jfi", "rogue_ms"])
                for c, j, t in collected:
                    w.writerow([c, f"{float(j):.6f}", "" if np.isnan(t) else f"{float(t):.6f}"])
            print("Saved results_jfi.csv")
        except Exception as e:
            print("Could not save results_jfi.csv:", e)

    def plot_jfi_vs_c(self, results):

        xs = [int(t[0]) for t in results]
        ys = [float(t[1]) for t in results]

        # JFI vs c
        plt.figure()
        plt.plot(xs, ys, marker='o')
        plt.xlabel("Rogue batch size (c)")
        plt.ylabel("Jain's Fairness Index (JFI)")
        plt.title("JFI vs c (median aggregated)")
        plt.grid(True, which="both", linestyle="--", linewidth=0.5)
        plt.tight_layout()
        plt.savefig("plot_JFI.png", dpi=150)
        plt.show()
        print("Saved plot_JFI.png")

        # T vs c
        zs = [float(t[2]) for t in results]
        plt.figure()
        plt.plot(xs, zs, marker='o')
        plt.xlabel("Rogue batch size (c)")
        plt.ylabel("Time taken to complete (ms)")
        plt.title("Rogue completion time vs c (median aggregated)")
        plt.grid(True, which="both", linestyle="--", linewidth=0.5)
        plt.tight_layout()
        plt.savefig("plot_T.png", dpi=150)
        print("Saved plot_T.png")
        return

    def load_results_csv(self, path="results_jfi.csv"):
        rows = []
        if not os.path.exists(path):
            print(f"{path} not found.")
            return rows
        try:
            with open(path, "r") as f:
                reader = csv.DictReader(f)
                cols = reader.fieldnames or []
                three_col = {"c", "jfi", "rogue_ms"}.issubset(set(cols))
                for row in reader:
                    try:
                        c  = int(row["c"])
                        j  = float(row["jfi"])
                        if three_col:
                            z = float(row["rogue_ms"]) if row["rogue_ms"] not in (None, "", "nan", "NaN") else float("nan")
                            rows.append((c, j, z))
                        else:
                            rows.append((c, j))
                    except Exception:
                        pass
            print(f"Loaded {len(rows)} rows from {path}.")
        except Exception as e:
            print("Failed to read CSV:", e)
        return rows

def main():
    runner = Runner()
    
    mode = None
    args = sys.argv[1:]
    if '--mode' in args:
        i = args.index('--mode')
        if i + 1 < len(args):
            mode = args[i + 1].lower()
    if mode == 'plot':
        rows = runner.load_results_csv("results_jfi.csv")
        runner.plot_jfi_vs_c(rows)
        return
    runner.run_varying_c()

if __name__ == '__main__':
    main()
