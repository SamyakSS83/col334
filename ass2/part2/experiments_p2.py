
import csv, json, os, time, subprocess
from statistics import mean
from topo_wordcount import make_net

CLIENT_COUNTS = list(range(1,33,4)) 
CLIENT_COUNTS.append(32) 
REPEATS = 5
OUT = 'results_p2.csv'

with open('config.json') as f:
    base_cfg = json.load(f)

# ensure words file exists
if not os.path.exists(base_cfg['filename']):
    with open(base_cfg['filename'],'w') as f:
        f.write('cat,bat,cat,dog,dog,emu,emu,emu,ant\n')

with open(OUT,'w',newline='') as f:
    w = csv.writer(f); w.writerow(['num_clients','run','avg_client_ms'])

def run_once(n):
    cfg = dict(base_cfg); cfg['num_clients'] = n
    net = make_net(); net.start()
    h1 = net.get('h1'); h2 = net.get('h2')
    srv = h2.popen('python3 server.py', shell=True)
    time.sleep(0.3)
    clients = [h1.popen('python3 client.py --quiet', shell=True) for _ in range(n)]
    times = []
    for c in clients:
        out = c.communicate()[0]
        if isinstance(out, bytes): out = out.decode()
        for line in out.splitlines():
            if line.startswith('ELAPSED_MS:'):
                times.append(int(line.split(':',1)[1]))
    srv.terminate(); time.sleep(0.2); net.stop()
    return times
    
def main():
    with open(OUT,'a',newline='') as f:
        w = csv.writer(f)
        for n in CLIENT_COUNTS:
            for r in range(1, REPEATS+1):
                times = run_once(n)
                if times:
                    avg = sum(times)/len(times)
                    w.writerow([n,r,f'{avg:.2f}'])
                    print(f'n={n} run={r} avg={avg:.2f}')
                else:
                    print(f'n={n} run={r} no data')
    print('Saved', OUT)

if __name__ == '__main__':
    main()
