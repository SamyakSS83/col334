#!/usr/bin/env python3
# main.py — orchestrates: build topo → start FRR/OSPF → wait → flap & iperf → (optional CLI)

import argparse, json
from mininet.cli import CLI
from mininet.log import setLogLevel, info
from p4_topo import build, H1_IP, H2_IP
from p4_ospf import start_frr_ospf, wait_for_convergence, stop_frr, generate_meta_ospf
from pathlib import Path
import random
import time
import os
import re
try:
    import matplotlib.pyplot as plt
except Exception:
    plt = None
import shutil
import os
import re
try:
    import matplotlib.pyplot as plt
except Exception:
    plt = None

def if_down_up(net, edge, down=True):
    """Bring both sides of a router-router link down/up."""
    ri, rj = net.get(edge["s_i"]), net.get(edge["s_j"])
    ifs = (edge["i_if"], edge["j_if"])
    action = "down" if down else "up"
    ri.cmd(f"ip link set {ifs[0]} {action}")
    rj.cmd(f"ip link set {ifs[1]} {action}")

def start_iperf(h1, h2, h1_ip, h2_ip, total_seconds, prefer_iperf3=True):
    """Start server on h2, client on h1. Returns (server_log, client_log)."""
    # fixed names (will be moved to ospf_logs by caller)
    s_log = "h2_ospf_iperf.log"
    c_log = "h1_ospf_iperf.log"
    have_iperf3 = prefer_iperf3 and ("iperf3" in h1.cmd("which iperf3"))
    if have_iperf3:
        h2.cmd(f"iperf3 -s -1 > {s_log} 2>&1 &")
        time.sleep(0.5)
        ip = h2_ip.split("/")[0]
        h1.cmd(f"iperf3 -c {ip} -t {int(total_seconds)} -i 1 > {c_log} 2>&1 &")
    else:
        h2.cmd(f"iperf -s > {s_log} 2>&1 &")
        time.sleep(0.5)
        ip = h2_ip.split("/")[0]
        h1.cmd(f"iperf -c {ip} -t {int(total_seconds)} -i 1 > {c_log} 2>&1 &")
    return s_log, c_log


def link_flap_exp(net, e, h1_ip, h2_ip, ospf_logs_dir, iperf_time = 30, link_down_duration = 5, wait_before_link_down = 4):
    """
    Run link flap experiment with OSPF packet capture
    Captures OSPF LSAs on multiple routers to analyze convergence
    """
    h1, h2 = net.get("h1"), net.get("h2")
    
    # Start tcpdump on multiple routers to capture OSPF packets (protocol 89)
    # We capture on s1 and s2 to see LSA propagation
    pcap_files = []
    try:
        for router_name in ['s1', 's2']:
            r = net.get(router_name)
            pcap = os.path.join(ospf_logs_dir, f'ospf_{router_name}.pcap')
            # Capture OSPF packets (protocol 89) with verbose packet info
            r.cmd(f'tcpdump -U -i any -s 0 proto 89 -w {pcap} > /dev/null 2>&1 &')
            pcap_files.append((router_name, pcap))
            print(f"*** Started OSPF packet capture on {router_name}: {pcap}")
    except Exception as e:
        print(f"*** Warning: Could not start tcpdump: {e}")

    s_log, c_log = start_iperf(h1, h2, h1_ip, h2_ip, iperf_time)
    # move logs into ospf_logs later; caller will collect
    print(f"*** iperf running: client log {c_log}, server log {s_log}")

    # Record timing
    experiment_start = time.time()
    time.sleep(wait_before_link_down)

    key = (e["s_i"], e["s_j"], e["i_if"], e["j_if"])
    link_down_time = time.time() - experiment_start
    print(f"*** LINK DOWN at t={link_down_time:.3f}s: {e['s_i']}:{e['i_if']} <-> {e['s_j']}:{e['j_if']} for {link_down_duration}s")
    if_down_up(net, e, down=True) ## code to toggle the link
    
    time.sleep(link_down_duration)
    
    link_up_time = time.time() - experiment_start
    print(f"*** LINK UP at t={link_up_time:.3f}s: {e['s_i']}:{e['i_if']} <-> {e['s_j']}:{e['j_if']}")
    if_down_up(net, e, down=False)

    print("*** Flaps done; waiting for iperf to finish…")
    time.sleep(iperf_time-link_down_duration-wait_before_link_down+5)

    c_out = h1.cmd(f"tail -n +1 {c_log} || true")
    s_out = h2.cmd(f"tail -n +1 {s_log} || true")
    
    # stop tcpdump on all routers
    for router_name, _ in pcap_files:
        try:
            r = net.get(router_name)
            r.cmd('pkill -f tcpdump || true')
        except Exception:
            pass

    return c_log, s_log, c_out, s_out, link_down_time, link_up_time, pcap_files


def parse_iperf_throughput(iperf_client_log):
    """Parse iperf client log for per-second throughput (Mbits/sec)."""
    if not os.path.exists(iperf_client_log):
        return []
    results = []
    with open(iperf_client_log, 'r') as f:
        for line in f:
            m = re.search(r"(\d+\.?\d*)-\s*(\d+\.?\d*)\s*sec\s+[0-9\.]+\s+MBytes\s+([0-9\.]+)\s+Mbits/sec", line)
            if m:
                end = float(m.group(2))
                val = float(m.group(3))
                results.append((end, val))
    return results


def wait_for_routes_stabilization(net, router_name='s1', intf=None, poll_interval=0.5, timeout=30):
    """Poll 'ip route' on router until two consecutive dumps match or timeout.
    Returns seconds elapsed until stable, and the final routing dump.
    """
    r = net.get(router_name)
    start = time.time()
    prev = None
    elapsed = 0.0
    while elapsed < timeout:
        cur = r.cmd('ip route')
        if prev is not None and cur == prev:
            return time.time() - start, cur
        prev = cur
        time.sleep(poll_interval)
        elapsed = time.time() - start
    return elapsed, prev



def main():
    ap = argparse.ArgumentParser(description="Mininet + FRR OSPF with link flaps and iperf.")
    ap.add_argument("--input-file", required=True, help="config json for OSPF")
    ap.add_argument("--subnet-start", default="10.10", help="pool start as 'A.B' (default 10.10)")
    ap.add_argument("--converge-timeout", type=int, default=60, help="Seconds to wait for initial convergence")
    ap.add_argument("--flap-iters", type=int, default=1, help="How many flap cycles")
    ap.add_argument("--stabilize", type=int, default=40, help="Seconds to wait after bringing link UP")
    ap.add_argument("--no-cli", action="store_true", help="Exit after test (no Mininet CLI)")
    ap.add_argument("--router-bw", type=int, default=10, help="bw (Mbps) for router-router links")
    ap.add_argument("--h1-bw", type=int, default=100, help="bw (Mbps) for h1↔s1 link")
    ap.add_argument("--h2-bw", type=int, default=50, help="bw (Mbps) for h2↔sN link")
    args = ap.parse_args()

    with open(args.input_file) as f:
        config = json.load(f)

    a_str, b_str = args.subnet_start.split(".")
    start_a, start_b = int(a_str), int(b_str)

    # 1) Topology
    net = build()
    meta_ospf = generate_meta_ospf(config)
    try:
        # 2) FRR
        start_frr_ospf(net, meta_ospf)

        # 3) Convergence
        print(f"*** Waiting for OSPF convergence (<= {args.converge_timeout}s)…")
        ok = wait_for_convergence(net, meta_ospf, timeout=args.converge_timeout)
        if ok:
            print("✅ OSPF converged (routes present)")
        else:
            print("⚠️  OSPF did not converge within timeout; continuing anyway.")

        #CLI(net)

        # Create logs dir for OSPF experiment
        ospf_logs = os.path.join('.', 'ospf_logs')
        os.makedirs(ospf_logs, exist_ok=True)

        # 4) Link flap experiment
        e = None
        for x in meta_ospf["edges"]:
            if x["s_i"] == "s1" and x["s_j"] == "s2":
                e = x
        c_log, s_log, c_out, s_out, link_down_time, link_up_time, pcap_files = link_flap_exp(
            net, e, h1_ip=H1_IP, h2_ip=H2_IP, ospf_logs_dir=ospf_logs)

        # Move iperf logs into ospf_logs (if present)
        try:
            if os.path.exists(c_log):
                shutil.move(c_log, os.path.join(ospf_logs, 'h1_ospf_iperf.log'))
                c_log = os.path.join(ospf_logs, 'h1_ospf_iperf.log')
            if os.path.exists(s_log):
                shutil.move(s_log, os.path.join(ospf_logs, 'h2_ospf_iperf.log'))
                s_log = os.path.join(ospf_logs, 'h2_ospf_iperf.log')
        except Exception:
            pass

        print("\n==== iperf CLIENT (h1) ====\n" + c_out)
        print("\n==== iperf SERVER (h2) ====\n" + s_out)

        # Parse iperf and create plot
        throughputs = parse_iperf_throughput(c_log)
        if throughputs:
            times, mbps = zip(*throughputs)
            if plt:
                plt.figure()
                plt.plot(times, mbps, marker='o')
                plt.axvline(x=link_down_time, color='r', linestyle='--', label='link down')
                plt.axvline(x=link_up_time, color='g', linestyle='--', label='link up')
                plt.xlabel('Time (s)')
                plt.ylabel('Throughput (Mbps)')
                plt.title('OSPF iperf per-second throughput')
                plt.legend()
                plt.grid(True)
                plt.savefig(os.path.join(ospf_logs, 'throughput.png'))
                plt.close()

        # Detect routing stabilization on s1
        stab_time, final_routes = wait_for_routes_stabilization(net, router_name='s1', timeout=args.stabilize)

        # Parse iperf throughput to infer convergence from iperf traces as well
        iperf_convergence_time = None
        if throughputs:
            times, mbps = zip(*throughputs)
            # Baseline before link-down
            # Ignore first interval (t=0-1)
            baseline_samples = [v for t, v in throughputs if t > 1.0 and t < link_down_time]
            baseline = sum(baseline_samples)/len(baseline_samples) if baseline_samples else max(mbps)
            threshold = 0.9 * baseline
            for t, v in throughputs:
                if t >= link_up_time and v >= threshold:
                    iperf_convergence_time = t - link_up_time
                    break

        # Save routing dump and convergence info
        conv_file = os.path.join(ospf_logs, 'convergence.log')
        with open(conv_file, 'w') as f:
            f.write('=' * 80 + '\n')
            f.write('OSPF CONVERGENCE LOG\n')
            f.write('=' * 80 + '\n\n')
            
            f.write('TIMING EVENTS:\n')
            f.write(f'  Link Failure at: t = {link_down_time:.3f}s\n')
            f.write(f'  Link Recovery at: t = {link_up_time:.3f}s\n')
            f.write(f'  Link Down Duration: {link_up_time - link_down_time:.3f}s\n\n')
            
            f.write('OSPF PACKET CAPTURES:\n')
            for router_name, pcap_file in pcap_files:
                f.write(f'  {router_name}: {pcap_file}\n')
            f.write('\nTo analyze OSPF LSAs:\n')
            for router_name, pcap_file in pcap_files:
                f.write(f'  tcpdump -r {pcap_file} -v\n')
                f.write(f'  tshark -r {pcap_file} -Y "ospf.msg == 4" -T fields -e frame.time_relative -e ospf.msg\n')
            f.write('\nLook for OSPF LSA Update (type 4) messages around link failure/recovery times\n\n')
            
            f.write('CONVERGENCE ANALYSIS:\n')
            f.write(f'  Routing table stabilization (polling): {stab_time:.3f}s\n')
            if iperf_convergence_time is not None:
                f.write(f'  iperf throughput recovery (90% baseline): {iperf_convergence_time:.3f}s\n')
            else:
                f.write('  iperf convergence: not detected in throughput trace\n')
            f.write('\nFinal routing table dump:\n')
            f.write(final_routes or 'NO ROUTES')

        print('\n*** OSPF convergence analysis written to', conv_file)
        print(f'\n*** OSPF CONVERGENCE RESULT:')
        print(f'    Routing table stabilization: {stab_time:.3f}s')
        if iperf_convergence_time is not None:
            print(f'    iperf-based convergence: {iperf_convergence_time:.3f}s')
        else:
            print(f'    iperf-based convergence: not detected')
        
        print('\n*** OSPF PACKET CAPTURES:')
        for router_name, pcap_file in pcap_files:
            print(f'    {router_name}: {pcap_file}')
        print('\n    Analyze with: tcpdump -r <pcap_file> -v')
        print('    Filter LSAs: tshark -r <pcap_file> -Y "ospf.msg == 4"')

        # 5) Optional CLI for inspection
        if not args.no_cli:
            print("\n*** Examples:")
            print("  s1 ip route")
            print("  s2 vtysh -c 'show ip ospf neighbor'")
            print("  h1 ping -c 3 h2")
            CLI(net)
    finally:
        # Cleanup
        stop_frr(net, meta_ospf)
        net.stop()

if __name__ == "__main__":
    setLogLevel('info')
    main()
