#!/usr/bin/env python3
import pandas as pd, matplotlib.pyplot as plt

CSV = 'results_p2.csv'
DF = pd.read_csv(CSV)
agg = DF.groupby('num_clients')['avg_client_ms'].agg(['count','mean','std']).reset_index()
agg['sem'] = agg['std'] / agg['count']**0.5
agg['ci95'] = 1.96 * agg['sem']
plt.figure()
plt.errorbar(agg['num_clients'], agg['mean'], yerr=agg['ci95'], fmt='o-', capsize=4)
plt.xlabel('Number of clients')
plt.ylabel('Average completion time per client (ms)')
plt.title('Part 2: Avg completion time vs clients (95% CI)')
plt.grid(True)
plt.savefig('p2_plot.png', bbox_inches='tight', dpi=170)
print('Saved p2_plot.png')
