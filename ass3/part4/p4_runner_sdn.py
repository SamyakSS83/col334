#!/usr/bin/env python3
"""
SDN Link Failure Test Runner for Part 4
Orchestrates: Start Ryu controller → Build topology → Wait for convergence → Link flap + iperf → Save logs
Includes convergence time logging for analysis
"""

import argparse
import json
import time
import subprocess
import signal
import os
import sys
import shutil
from datetime import datetime
from pathlib import Path

from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info, error
import re
try:
    import matplotlib.pyplot as plt
except Exception:
    plt = None

# Host IPs from config
H1_IP = "10.0.12.2/24"
H2_IP = "10.0.67.2/24"

# Convergence time tracking
convergence_log_data = []
link_failure_time = None
link_recovery_time = None

def hex_dpid(n: int) -> str:
    """Convert integer to hex DPID string"""
    return f"{int(n):016x}"

def build_topology():
    """Build the SDN topology matching p3_topo-2.py"""
    net = Mininet(
        controller=None, build=False, link=TCLink,
        autoSetMacs=False, autoStaticArp=False
    )

    info('*** Adding Remote Controller (expecting Ryu at 127.0.0.1:6633)\n')
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6633)

    info('*** Add OVS switches s1..s6 with DPIDs 1..6\n')
    s1 = net.addSwitch('s1', cls=OVSSwitch, dpid=hex_dpid(1), failMode='secure', protocols='OpenFlow13')
    s2 = net.addSwitch('s2', cls=OVSSwitch, dpid=hex_dpid(2), failMode='secure', protocols='OpenFlow13')
    s3 = net.addSwitch('s3', cls=OVSSwitch, dpid=hex_dpid(3), failMode='secure', protocols='OpenFlow13')
    s4 = net.addSwitch('s4', cls=OVSSwitch, dpid=hex_dpid(4), failMode='secure', protocols='OpenFlow13')
    s5 = net.addSwitch('s5', cls=OVSSwitch, dpid=hex_dpid(5), failMode='secure', protocols='OpenFlow13')
    s6 = net.addSwitch('s6', cls=OVSSwitch, dpid=hex_dpid(6), failMode='secure', protocols='OpenFlow13')

    info('*** Add hosts\n')
    h1 = net.addHost('h1', ip='10.0.12.2/24', mac='00:00:00:00:01:02')
    h2 = net.addHost('h2', ip='10.0.67.2/24', mac='00:00:00:00:06:02')

    info('*** Host <-> switch links\n')
    net.addLink(h1, s1, intfName1='h1-eth1', intfName2='s1-eth1', bw=10)

    info('*** Inter-switch ring links\n')
    net.addLink(s1, s2, intfName1='s1-eth2', intfName2='s2-eth1', bw=10)
    net.addLink(s2, s3, intfName1='s2-eth2', intfName2='s3-eth1', bw=10)
    net.addLink(s3, s6, intfName1='s3-eth2', intfName2='s6-eth1', bw=10)
    net.addLink(s4, s1, intfName1='s4-eth1', intfName2='s1-eth3', bw=10)
    net.addLink(s5, s4, intfName1='s5-eth1', intfName2='s4-eth2', bw=5)
    net.addLink(s6, s5, intfName1='s6-eth2', intfName2='s5-eth2', bw=10)
    
    # Add h2 link LAST so it gets the correct port number (eth3 = port 3)
    net.addLink(h2, s6, intfName1='h2-eth1', intfName2='s6-eth3', bw=10)

    info('*** Build & start\n')
    net.build()
    net.start()

    info('*** Configure hosts: IP/MAC + default routes\n')
    h1.cmd('ip addr flush dev h1-eth1')
    h1.cmd('ip addr add 10.0.12.2/24 dev h1-eth1')
    h1.cmd('ip link set h1-eth1 address 00:00:00:00:01:02 up')
    h1.cmd('ip route add default via 10.0.12.1 dev h1-eth1')

    h2.cmd('ip addr flush dev h2-eth1')
    h2.cmd('ip addr add 10.0.67.2/24 dev h2-eth1')
    h2.cmd('ip link set h2-eth1 address 00:00:00:00:06:02 up')
    h2.cmd('ip route add default via 10.0.67.1 dev h2-eth1')

    return net

def start_ryu_controller(log_file):
    """Start Ryu controller in background, returns process handle"""
    info(f'*** Starting Ryu controller (logs to {log_file})\n')
    
    # Use full path to ryu-manager from micromamba environment
    # This works even when script is run with sudo
    ryu_manager_path = '/home/threesamyak/micromamba/envs/forryu/bin/ryu-manager'
    
    # Run from the ass3 directory so config path works
    controller_cmd = [
        ryu_manager_path,
        '--observe-links',
        'ryu.topology.switches',
        'part4/p4_l3spf_lf.py'
    ]
    
    log_fd = open(log_file, 'w')
    proc = subprocess.Popen(
        controller_cmd,
        stdout=log_fd,
        stderr=subprocess.STDOUT,
        cwd='/home/threesamyak/col334/ass3',
        preexec_fn=os.setsid  # Create new process group for clean kill
    )
    
    info(f'*** Ryu controller started (PID {proc.pid})\n')
    info('*** Waiting 2 seconds for controller to initialize...\n')
    time.sleep(2)
    
    # Check if controller is still running
    if proc.poll() is not None:
        error('ERROR: Ryu controller failed to start!\n')
        log_fd.close()
        with open(log_file, 'r') as f:
            error(f.read())
        sys.exit(1)
    
    return proc, log_fd

def wait_for_topology_discovery(net, timeout=2):
    """Wait for LLDP-based topology discovery"""
    info(f'*** Waiting up to {timeout}s for topology discovery (LLDP)...\n')
    time.sleep(timeout)
    
    # Verify connectivity with a quick ping
    h1 = net.get('h1')
    h2 = net.get('h2')
    info('*** Testing initial connectivity with ping...\n')
    result = h1.cmd(f'ping -c 3 -W 2 10.0.67.2')
    
    if '3 received' in result or '3 packets transmitted, 3 received' in result:
        info('✅ Initial connectivity established\n')
        return True
    else:
        info('⚠️  Initial ping test had packet loss (may be normal during discovery)\n')
        return True  # Continue anyway

def link_down_up(net, switch1, switch2, intf1, intf2, down=True):
    """Bring both sides of a switch-switch link down/up"""
    s1 = net.get(switch1)
    s2 = net.get(switch2)
    action = "down" if down else "up"
    
    info(f'*** Link {action.upper()}: {switch1}:{intf1} <-> {switch2}:{intf2}\n')
    s1.cmd(f'ip link set {intf1} {action}')
    s2.cmd(f'ip link set {intf2} {action}')

def run_iperf_with_link_flap(net, args, test_start_time, convergence_log):
    """
    Run iperf test with link failure during transmission
    Records convergence times by monitoring controller logs (flow changes)
    """
    h1 = net.get('h1')
    h2 = net.get('h2')
    
    # Use fixed log filenames inside sdn_logs (overwritten each run)
    logs_dir = os.path.dirname(convergence_log)
    client_log = os.path.join(logs_dir, 'h1_sdn_iperf.log')
    server_log = os.path.join(logs_dir, 'h2_sdn_iperf.log')

    info(f'\n*** Starting iperf test (duration={args.iperf_duration}s)\n')
    info(f'    Client log: {client_log}\n')
    info(f'    Server log: {server_log}\n')

    # Start iperf server on h2
    info('*** Starting iperf server on h2...\n')
    h2.cmd(f'iperf -s > {server_log} 2>&1 &')
    time.sleep(1)

    # Start iperf client on h1 (1s sampling)
    info(f'*** Starting iperf client on h1 (to 10.0.67.2)...\n')
    h1.cmd(f'iperf -c 10.0.67.2 -t {args.iperf_duration} -i 1 > {client_log} 2>&1 &')
    
    # Wait before link failure
    info(f'*** Waiting {args.failure_time}s before link failure...\n')
    time.sleep(args.failure_time)
    
    # Record link failure time relative to test start
    link_down_time = time.time() - test_start_time
    info(f'\n*** LINK FAILURE at t={link_down_time:.2f}s: {args.link_switch1}:{args.link_intf1} <-> {args.link_switch2}:{args.link_intf2}\n')
    link_down_up(net, args.link_switch1, args.link_switch2, 
                 args.link_intf1, args.link_intf2, down=True)

    # Wait during failure
    info(f'*** Link down for {args.failure_duration}s...\n')
    time.sleep(args.failure_duration)
    
    # Restore the link
    link_up_time = time.time() - test_start_time
    info(f'\n*** LINK RECOVERY at t={link_up_time:.2f}s: {args.link_switch1}:{args.link_intf1} <-> {args.link_switch2}:{args.link_intf2}\n')
    link_down_up(net, args.link_switch1, args.link_switch2,
                 args.link_intf1, args.link_intf2, down=False)
    
    # Wait for iperf to complete
    remaining = args.iperf_duration - args.failure_time - args.failure_duration
    if remaining > 0:
        info(f'*** Waiting {remaining}s for iperf to complete...\n')
        time.sleep(remaining)
    
    # Read log contents
    info('*** Reading iperf logs...\n')
    time.sleep(0.5)  # Give logs time to flush
    
    try:
        with open(client_log, 'r') as f:
            client_content = f.read()
    except:
        client_content = "ERROR: Could not read client log"
    
    try:
        with open(server_log, 'r') as f:
            server_content = f.read()
    except:
        server_content = "ERROR: Could not read server log"
    
    # Create convergence analysis log
    with open(convergence_log, 'w') as f:
        f.write("=" * 80 + "\n")
        f.write("SDN CONVERGENCE TIME ANALYSIS\n")
        f.write(f"Test Start: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 80 + "\n\n")
        
        f.write("TIMING EVENTS:\n")
        f.write(f"  Link Failure at: t = {link_down_time:.3f}s\n")
        f.write(f"  Link Recovery at: t = {link_up_time:.3f}s\n")
        f.write(f"  Link Down Duration: {link_up_time - link_down_time:.3f}s\n\n")
        
        f.write("=" * 80 + "\n")
        f.write("FLOW TABLE CHANGE ANALYSIS:\n")
        f.write("=" * 80 + "\n")
        f.write("Check controller.log for [FLOW_CHANGE] entries\n")
        f.write("Flow changes show when routing rules are updated during convergence\n")
        f.write("Look for timestamps between link failure and recovery to measure convergence time\n\n")
        
        f.write("=" * 80 + "\n")
        f.write("CONVERGENCE INFERENCE FROM IPERF:\n")
        f.write("=" * 80 + "\n")
        f.write("Look for bandwidth drop during [" + f"{link_down_time:.1f}s, {link_up_time:.1f}s" + "]\n")
        f.write("Convergence time = time until bandwidth returns to normal\n\n")
        
        f.write("CLIENT IPERF OUTPUT:\n")
        f.write("-" * 80 + "\n")
        f.write(client_content + "\n\n")

    # Parse per-second throughput from client iperf log and save a plot + inference
    throughputs = parse_iperf_throughput(client_log)
    throughputs = throughputs[1:]  # Ignore first interval (t=0-1)
    if throughputs:
        times, mbps = zip(*throughputs)
        plot_file = os.path.join(logs_dir, 'throughput.png')
        if plt:
            plt.figure()
            plt.plot(times, mbps, marker='o')
            plt.axvline(x=link_down_time, color='r', linestyle='--', label='link down')
            plt.axvline(x=link_up_time, color='g', linestyle='--', label='link up')
            plt.xlabel('Time (s)')
            plt.ylabel('Throughput (Mbps)')
            plt.title('SDN iperf per-second throughput')
            plt.legend()
            plt.grid(True)
            plt.savefig(plot_file)
            plt.close()

        # Compute baseline throughput from samples before link_down_time (ignore first interval at t=0-1)
        baseline_samples = [v for t, v in throughputs if t > 1.0 and t < link_down_time]
        baseline = sum(baseline_samples)/len(baseline_samples) if baseline_samples else max(mbps)
        threshold = 0.9 * baseline
        convergence_time = None
        for t, v in throughputs:
            if t >= link_up_time and v >= threshold:
                convergence_time = t - link_up_time
                break

        # Downtime convergence detection: time from link-down until throughput falls below a drop threshold
        drop_threshold = 0.5 * baseline
        downtime_convergence = None
        for t, v in throughputs:
            if t >= link_down_time and v <= drop_threshold:
                downtime_convergence = t - link_down_time
                break

        # Append final inference to convergence_log
        with open(convergence_log, 'a') as f:
            f.write('\n')
            f.write('IPERF-BASED CONVERGENCE INFERENCE:\n')
            f.write(f'  baseline throughput (pre-failure): {baseline:.2f} Mbps\n')
            f.write(f'  convergence threshold: {threshold:.2f} Mbps (90% baseline)\n')
            if convergence_time is not None:
                f.write(f'  time from link-up until throughput >= threshold: {convergence_time:.3f}s\n')
            else:
                f.write('  Could not detect convergence to threshold in the iperf trace\n')
            if downtime_convergence is not None:
                f.write(f'  time from link-down until throughput fell below {drop_threshold:.2f} Mbps: {downtime_convergence:.3f}s\n')
            else:
                f.write('  Could not detect a clear downtime drop to the chosen threshold in the iperf trace\n')

        info('*** Throughput plot and convergence inference written.\n')
        # Print final answer
        info('*** SDN CONVERGENCE RESULT:\n')
        if convergence_time is not None:
            info(f'    Convergence time after link recovery: {convergence_time:.3f}s\n')
        else:
            info('    Convergence not detected in iperf throughput trace.\n')
        if downtime_convergence is not None:
            info(f'    Time from link-down until throughput dropped: {downtime_convergence:.3f}s\n')
        else:
            info('    Downtime drop not clearly detected in iperf trace.\n')
    
    return client_content, server_content, client_log, server_log

def parse_iperf_throughput(iperf_client_log):
    """Parse iperf (old iperf) per-second lines from client log and return list of (time, Mbps).
    Supports typical iperf -i 1 output lines like:
      [  3]  0.0- 1.0 sec  1.23 MBytes  10.3 Mbits/sec
    """
    if not os.path.exists(iperf_client_log):
        return []
    times = []
    results = []
    with open(iperf_client_log, 'r') as f:
        for line in f:
            # Try to find lines with 'Mbits/sec' or 'Kbits/sec'
            if 'Mbits/sec' in line or 'Kbits/sec' in line or 'bits/sec' in line:
                # extract the interval prefix (e.g., '0.0- 1.0') and the throughput value
                m = re.search(r"(\d+\.?\d*)-\s*(\d+\.?\d*)\s*sec\s+([0-9\.]+)\s+MBytes\s+([0-9\.]+)\s+Mbits/sec", line)
                if m:
                    start = float(m.group(1))
                    # use end time as sample time
                    end = float(m.group(2))
                    val = float(m.group(4))
                    results.append((end, val))
                    continue
                # fallback to generic extraction of number before 'Mbits/sec'
                m2 = re.search(r"([0-9\.]+)\s+Mbits/sec", line)
                if m2:
                    # try to get time from previous bracketed field
                    tmatch = re.search(r"\[\s*\d+\]\s*(\d+\.\d+)-(\d+\.\d+)\s*sec", line)
                    if tmatch:
                        t = float(tmatch.group(2))
                    else:
                        t = len(results) + 1
                    results.append((t, float(m2.group(1))))
    return results


def wait_for_flow_stabilization(net, out_file, poll_interval=0.5, timeout=20):
    """Poll switch flow dumps until two consecutive dumps match (no change), or timeout.
    Returns seconds elapsed from start of polling until stabilization, or timeout value.
    """
    start = time.time()
    prev = None
    elapsed = 0.0
    while elapsed < timeout:
        # dump flows to a temp file
        with open(out_file + '.tmp', 'w') as f:
            for sw in ['s1','s2','s3','s4','s5','s6']:
                f.write(f"--- {sw} ---\n")
                f.write(net.get(sw).cmd('ovs-ofctl -O OpenFlow13 dump-flows ' + sw))
                f.write('\n')
        # read back and compare
        with open(out_file + '.tmp', 'r') as f:
            cur = f.read()
        if prev is not None and cur == prev:
            # stable
            shutil.move(out_file + '.tmp', out_file)
            return time.time() - start
        prev = cur
        time.sleep(poll_interval)
        elapsed = time.time() - start
    # timeout
    try:
        shutil.move(out_file + '.tmp', out_file)
    except Exception:
        pass
    return elapsed

def main():
    parser = argparse.ArgumentParser(description='SDN Link Failure Test Runner for Part 4')
    parser.add_argument('--iperf-duration', type=int, default=30,
                        help='Total iperf test duration (seconds)')
    parser.add_argument('--failure-time', type=int, default=4,
                        help='Time before link failure (seconds)')
    parser.add_argument('--failure-duration', type=int, default=5,
                        help='Link down duration (seconds)')
    parser.add_argument('--link-switch1', default='s2',
                        help='First switch for link failure')
    parser.add_argument('--link-switch2', default='s3',
                        help='Second switch for link failure')
    parser.add_argument('--link-intf1', default='s2-eth2',
                        help='Interface on first switch')
    parser.add_argument('--link-intf2', default='s3-eth1',
                        help='Interface on second switch')
    parser.add_argument('--no-cli', action='store_true',
                        help='Exit after test (no Mininet CLI)')
    parser.add_argument('--output-dir', default='.',
                        help='Directory for output logs (default: current directory)')
    
    args = parser.parse_args()
    
    # Create sdn_logs directory
    sdn_logs_dir = os.path.join(args.output_dir, 'sdn_logs')
    os.makedirs(sdn_logs_dir, exist_ok=True)
    
    # Use fixed names (no timestamp) - each run overwrites
    controller_log = os.path.join(sdn_logs_dir, 'controller.log')
    convergence_log = os.path.join(sdn_logs_dir, 'convergence.log')
    
    controller_proc = None
    controller_log_fd = None
    net = None
    
    try:
        # 1. Start Ryu controller
        controller_proc, controller_log_fd = start_ryu_controller(controller_log)
        
        # 2. Build topology
        info('\n' + '=' * 80 + '\n')
        info('BUILDING MININET TOPOLOGY\n')
        info('=' * 80 + '\n')
        net = build_topology()
        
        # 3. Wait for topology discovery
        info('\n' + '=' * 80 + '\n')
        info('WAITING FOR TOPOLOGY DISCOVERY\n')
        info('=' * 80 + '\n')
        wait_for_topology_discovery(net)
        
        # Record start time
        test_start_time = time.time()
        
        # 4. Run iperf with link failure
        info('\n' + '=' * 80 + '\n')
        info('RUNNING IPERF TEST WITH LINK FAILURE\n')
        info('=' * 80 + '\n')
        client_out, server_out, client_log_path, server_log_path = run_iperf_with_link_flap(net, args, test_start_time, convergence_log)
        
        # Print results
        info('\n' + '=' * 80 + '\n')
        info('TEST COMPLETE - RESULTS\n')
        info('=' * 80 + '\n\n')
        info('=== IPERF CLIENT (h1) ===\n')
        info(client_out + '\n')
        info('\n=== IPERF SERVER (h2) ===\n')
        info(server_out + '\n')
        info('\n=== FILES GENERATED ===\n')
        info(f'Controller log: {controller_log}\n')
        info(f'Convergence log: {convergence_log}\n')
        info(f'Client iperf: {client_log_path}\n')
        info(f'Server iperf: {server_log_path}\n')
        
        # 6. Optional CLI
        if not args.no_cli:
            info('\n' + '=' * 80 + '\n')
            info('ENTERING MININET CLI\n')
            info('=' * 80 + '\n')
            info('\nUseful commands:\n')
            info('  h1 ping -c 3 h2\n')
            info('  s1 ovs-ofctl -O OpenFlow13 dump-flows s1\n')
            info('  link s2 s3 down  (simulate failure)\n')
            info('  link s2 s3 up    (restore)\n\n')
            CLI(net)
    
    except KeyboardInterrupt:
        info('\n*** Interrupted by user\n')
    
    except Exception as e:
        error(f'\n*** Error: {e}\n')
        import traceback
        traceback.print_exc()
    
    finally:
        # Cleanup
        info('\n*** Cleaning up...\n')
        
        if net:
            info('*** Stopping Mininet...\n')
            net.stop()
        
        if controller_proc:
            info('*** Stopping Ryu controller...\n')
            try:
                # Kill entire process group
                os.killpg(os.getpgid(controller_proc.pid), signal.SIGTERM)
                controller_proc.wait(timeout=5)
            except:
                # Force kill if needed
                try:
                    os.killpg(os.getpgid(controller_proc.pid), signal.SIGKILL)
                except:
                    pass
        
        if controller_log_fd:
            controller_log_fd.close()
        
        info('*** Done!\n')

if __name__ == '__main__':
    setLogLevel('info')
    main()
