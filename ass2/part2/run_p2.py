#!/usr/bin/env python3
# Minimal single experiment runner for Part 2
import json, time, subprocess, os
from statistics import mean
from topo_wordcount import make_net

with open('config.json') as f:
    cfg = json.load(f)
num_clients = int(cfg.get('num_clients',1))

net = make_net(); net.start()
h1 = net.get('h1'); h2 = net.get('h2')
srv = h2.popen('python3 server.py', shell=True)
time.sleep(0.3)
clients = [h1.popen('python3 client.py --quiet', shell=True) for _ in range(num_clients)]
times = []
for c in clients:
    out = c.communicate()[0]
    if isinstance(out, bytes): out = out.decode()
    for line in out.splitlines():
        if line.startswith('ELAPSED_MS:'):
            times.append(int(line.split(':',1)[1]))
srv.terminate(); time.sleep(0.2); net.stop()

if times:
    print(f'NUM_CLIENTS:{num_clients} AVG_CLIENT_TIME_MS:{mean(times):.2f}')
else:
    print('No times collected')
