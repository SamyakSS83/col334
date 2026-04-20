#!/usr/bin/env python3
import pandas as pd
import matplotlib.pyplot as plt
from pathlib import Path
import math

df = pd.read_csv("results.csv")
# Aggregate
agg = df.groupby("k")["elapsed_ms"].agg(["mean", "std", "count"]).reset_index()
# 95% CI using normal approx (n=5 is small, but acceptable for this assignment)
agg["sem"] = agg["std"] / agg["count"].pow(0.5)
agg["ci95"] = 1.96 * agg["sem"]

plt.figure()
plt.errorbar(agg["k"], agg["mean"], yerr=agg["ci95"], fmt='o-', capsize=4)
plt.xlabel("k (words per request)")
plt.ylabel("Completion time (ms)")
plt.title("Word Download Completion Time vs k (avg ± 95% CI, n=5)")
plt.grid(True)
plt.savefig("p1_plot.png", bbox_inches="tight", dpi=180)
print("Saved plot.png")
