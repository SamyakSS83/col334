# demo_runner.py
import json, os, time, pathlib
from topo_wordcount import make_net

K = int(os.environ.get("K", "5"))
P = int(os.environ.get("P", "0"))

# load base config and override p
cfg = json.loads(pathlib.Path("config.json").read_text())
cfg["p"] = P
pathlib.Path("demo_config.json").write_text(json.dumps(cfg))

net = make_net(); net.start()
h1, h2 = net.get('h1'), net.get('h2')

# start server with demo config
srv = h2.popen("./bin/server --config demo_config.json", shell=True)
time.sleep(0.5)

# run client once (no --quiet): prints word frequencies + ELAPSED_MS
print(h1.cmd(f"./bin/client --config demo_config.json --k {K}"))

srv.terminate(); time.sleep(0.2); net.stop()
