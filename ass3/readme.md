Hello

General commands
```bash
pingall
h1 ping h2 -c <num>

h2 iperf -s &
h1 iperf -c h2 -t 15 -P 2
```
How to run part1

on terminal A
```bash
ryu-manager part1/<filename>
```

on terminal B

```bash
sudo python3 part1/p1_topo.py
```


How to run part2

terminal A

```bash
ryu-manager --observe-links ryu.topology.switches part2/p2_l2spf.py
```

terminal B

```bash
sudo python3 part2/p2_topo.py
```
for bonus:


h1 iperf -u -c 10.0.0.2 -b 10M -t 25 -i 1  
h1 iperf -u -c 10.0.0.2 -b 10M -t 25 -i 1 > /tmp/h1_iperf_udp_10_1M.log 2>&1 &


CONVERGENCE_DELAY_MS=100 sudo -E python3 part4/p4_runner_sdn.py --no-cli


rest all is bullshit and only for my reference

Thanks dear namit,
Kindly flirt in this channel

