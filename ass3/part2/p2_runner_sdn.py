#!/usr/bin/env python3
"""
Part 2 SDN Link Failure Test Runner
Builds the Mininet topology from `p2_topo.CustomTopo`, optionally starts ryu-manager
and runs an iperf UDP/TCP test while flapping a chosen inter-switch link. Logs saved to /tmp.
"""
import argparse
import os
import sys
import time
import subprocess
from datetime import datetime
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.link import TCLink
from mininet.log import setLogLevel, info

from p2_topo import CustomTopo


def hex_dpid(n: int) -> str:
    return f"{int(n):016x}"


def build_topology():
    """Build topology matching p2_topo with OpenFlow 1.3"""
    topo = CustomTopo()
    # Use OVSSwitch with OpenFlow13 protocol
    net = Mininet(
        topo=topo,
        switch=lambda name, **kwargs: OVSSwitch(name, protocols='OpenFlow13', **kwargs),
        link=TCLink,
        controller=None,
        autoSetMacs=True,
        autoStaticArp=True,
        build=False
    )
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6633)
    net.build()
    net.start()
    
    # Wait for switches to connect and controller to initialize
    info('*** Waiting for switches to connect...\n')
    time.sleep(2)
    
    return net


def start_ryu_controller(script_path, log_file):
    ryu_manager_path = '/home/threesamyak/micromamba/envs/forryu/bin/ryu-manager'
    cmd = [ryu_manager_path, '--observe-links', 'ryu.topology.switches', script_path]
    log_fd = open(log_file, 'w')
    proc = subprocess.Popen(cmd, stdout=log_fd, stderr=subprocess.STDOUT,
                            cwd=os.path.dirname(os.path.dirname(__file__)), preexec_fn=os.setsid)
    time.sleep(2)
    if proc.poll() is not None:
        log_fd.close()
        raise RuntimeError('Ryu controller failed to start')
    return proc, log_fd


def link_down_up(net, s1, s2, intf1, intf2, down=True):
    action = 'down' if down else 'up'
    info(f'*** Link {action}: {s1}:{intf1} <-> {s2}:{intf2}\n')
    net.get(s1).cmd(f'ip link set {intf1} {action}')
    net.get(s2).cmd(f'ip link set {intf2} {action}')


def run_iperf_with_flap(net, args):
    h1 = net.get('h1')
    h2 = net.get('h2')
    
    # Get actual IP addresses
    h1_ip = h1.IP()
    h2_ip = h2.IP()
    
    info(f'*** Host IPs: h1={h1_ip}, h2={h2_ip}\n')
    
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    client_log = f'/tmp/p2_h1_iperf_{ts}.log'
    server_log = f'/tmp/p2_h2_iperf_{ts}.log'
    info('*** Starting iperf server on h2\n')
    h2.cmd(f'iperf -s > {server_log} 2>&1 &')
    time.sleep(1)
    info(f'*** Starting iperf client on h1 (to {h2_ip})\n')
    # default to UDP for bonus tests, TCP otherwise (user can control)
    proto_flag = '-u' if args.udp else ''
    h1.cmd(f'iperf {proto_flag} -c {h2_ip} -t {args.iperf_duration} -i 1 > {client_log} 2>&1 &')
    time.sleep(args.failure_time)
    # flap
    link_down_up(net, args.link_switch1, args.link_switch2, args.link_intf1, args.link_intf2, down=True)
    time.sleep(args.failure_duration)
    link_down_up(net, args.link_switch1, args.link_switch2, args.link_intf1, args.link_intf2, down=False)
    remaining = args.iperf_duration - args.failure_time - args.failure_duration
    if remaining > 0:
        time.sleep(remaining + 1)
    # read logs
    with open(client_log, 'r') as f:
        client = f.read()
    with open(server_log, 'r') as f:
        server = f.read()
    return client, server, client_log, server_log


def main():
    parser = argparse.ArgumentParser(description='Part 2 SDN Link Failure Runner')
    parser.add_argument('--start-controller', action='store_true', help='Start Ryu controller automatically')
    parser.add_argument('--controller-script', default='part2/p2_l2spf.py', help='Controller script to run')
    parser.add_argument('--iperf-duration', type=int, default=15)
    parser.add_argument('--failure-time', type=int, default=3)
    parser.add_argument('--failure-duration', type=int, default=5)
    parser.add_argument('--link-switch1', default='s2')
    parser.add_argument('--link-switch2', default='s4')
    parser.add_argument('--link-intf1', default='s2-eth2')
    parser.add_argument('--link-intf2', default='s4-eth1')
    parser.add_argument('--udp', action='store_true', help='Use UDP iperf client')
    parser.add_argument('--output-dir', default='/tmp')
    args = parser.parse_args()

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    controller_log = os.path.join(args.output_dir, f'p2_controller_{timestamp}.log')
    summary_log = os.path.join(args.output_dir, f'p2_summary_{timestamp}.log')

    controller_proc = None
    controller_fd = None
    net = None
    try:
        if args.start_controller:
            controller_proc, controller_fd = start_ryu_controller(args.controller_script, controller_log)
        net = build_topology()
        # Give LLDP a moment to discover topology
        info('*** Waiting for LLDP topology discovery (10s)...\n')
        time.sleep(10)
        
        # Test connectivity first
        h1 = net.get('h1')
        h2 = net.get('h2')
        info('*** Testing initial connectivity with ping...\n')
        result = h1.cmd(f'ping -c 3 -W 2 {h2.IP()}')
        info(result)
        if '3 received' in result or ', 0% packet loss' in result:
            info('✅ Initial connectivity OK\n')
        else:
            info('⚠️  Connectivity issue detected, proceeding anyway...\n')
        
        client_out, server_out, client_log, server_log = run_iperf_with_flap(net, args)
        with open(summary_log, 'w') as f:
            f.write('IPERF CLIENT:\n')
            f.write(client_out + '\n')
            f.write('\nIPERF SERVER:\n')
            f.write(server_out + '\n')
            f.write(f'Client log: {client_log}\n')
            f.write(f'Server log: {server_log}\n')
            f.write(f'Controller log: {controller_log if controller_proc else "(not started)"}\n')
        info(f'*** Summary written to {summary_log}\n')
    finally:
        if net:
            net.stop()
        if controller_proc:
            try:
                os.killpg(os.getpgid(controller_proc.pid), 15)
            except Exception:
                pass
            controller_fd.close()


if __name__ == '__main__':
    setLogLevel('info')
    main()
