#!/usr/bin/env python3
"""
SDN Topology for Link Failure Testing
Similar to OSPF topology but uses OpenFlow switches
"""

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info
import time

def build_sdn_topo():
    """Build the same topology as OSPF version but with OVS switches"""
    net = Mininet(
        controller=RemoteController,
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True,
        autoStaticArp=True
    )
    
    info('*** Adding controller\n')
    c0 = net.addController('c0', controller=RemoteController,
                           ip='127.0.0.1', port=6633)
    
    info('*** Adding switches\n')
    switches = []
    for i in range(1, 7):
        s = net.addSwitch(f's{i}', protocols='OpenFlow13')
        switches.append(s)
    
    info('*** Adding hosts\n')
    h1 = net.addHost('h1', ip='10.0.12.2/24', mac='00:00:00:00:01:02')
    h2 = net.addHost('h2', ip='10.0.67.2/24', mac='00:00:00:00:06:02')
    
    info('*** Adding host-switch links\n')
    net.addLink(h1, switches[0], bw=10)  # h1 <-> s1
    net.addLink(h2, switches[5], bw=10)  # h2 <-> s6
    
    info('*** Adding inter-switch links (ring topology)\n')
    net.addLink(switches[0], switches[1], bw=10)  # s1 <-> s2 (cost 10)
    net.addLink(switches[1], switches[2], bw=10)  # s2 <-> s3 (cost 10)
    net.addLink(switches[2], switches[5], bw=10)  # s3 <-> s6 (cost 10)
    net.addLink(switches[5], switches[4], bw=10)  # s6 <-> s5 (cost 10)
    net.addLink(switches[4], switches[3], bw=5)   # s5 <-> s4 (cost 20 via lower bw)
    net.addLink(switches[3], switches[0], bw=10)  # s4 <-> s1 (cost 10)
    
    return net

def link_flap_experiment(net, link_down_duration=5, link_down_time=2, iperf_time=15):
    """
    Run link failure experiment similar to OSPF version
    - Start iperf for iperf_time seconds
    - At link_down_time seconds, bring down s2-s3 link
    - Keep link down for link_down_duration seconds
    - Bring link back up
    """
    h1 = net.get('h1')
    h2 = net.get('h2')
    
    print("\n*** Starting iperf server on h2")
    h2.cmd('iperf -s > h2_iperf_sdn.log 2>&1 &')
    time.sleep(1)
    
    print(f"*** Starting iperf client on h1 ({iperf_time} seconds)")
    h1.cmd(f'iperf -c 10.0.67.2 -t {iperf_time} -i 1 > h1_iperf_sdn.log 2>&1 &')
    
    print(f"*** Waiting {link_down_time} seconds before link failure...")
    time.sleep(link_down_time)
    
    print("\n*** ========================================")
    print("*** BRINGING DOWN s2-s3 link")
    print("*** ========================================")
    net.configLinkStatus('s2', 's3', 'down')
    
    print(f"*** Link will stay down for {link_down_duration} seconds...")
    time.sleep(link_down_duration)
    
    print("\n*** ========================================")
    print("*** BRINGING UP s2-s3 link")
    print("*** ========================================")
    net.configLinkStatus('s2', 's3', 'up')
    
    remaining_time = iperf_time - link_down_time - link_down_duration + 1
    print(f"*** Waiting {remaining_time}s for iperf to complete...")
    time.sleep(remaining_time)
    
    print("\n*** Killing iperf processes")
    h1.cmd('killall iperf')
    h2.cmd('killall iperf')
    time.sleep(1)
    
    print("\n" + "="*60)
    print("*** iperf CLIENT results (h1):")
    print("="*60)
    client_output = h1.cmd('cat h1_iperf_sdn.log || echo "No log file"')
    print(client_output)
    
    print("\n" + "="*60)
    print("*** iperf SERVER results (h2):")
    print("="*60)
    server_output = h2.cmd('cat h2_iperf_sdn.log || echo "No log file"')
    print(server_output)

def show_flow_rules(net):
    """Display flow rules on key switches"""
    print("\n" + "="*60)
    print("*** Flow Rules on s1:")
    print("="*60)
    s1 = net.get('s1')
    print(s1.cmd('ovs-ofctl -O OpenFlow13 dump-flows s1'))
    
    print("\n" + "="*60)
    print("*** Flow Rules on s6:")
    print("="*60)
    s6 = net.get('s6')
    print(s6.cmd('ovs-ofctl -O OpenFlow13 dump-flows s6'))

def main():
    setLogLevel('info')
    
    print("\n" + "="*60)
    print("*** Building SDN Topology")
    print("="*60)
    net = build_sdn_topo()
    net.start()
    
    print("\n*** Waiting for controller to initialize and install flows...")
    time.sleep(5)
    
    print("\n" + "="*60)
    print("*** Testing initial connectivity")
    print("="*60)
    # Test connectivity
    h1 = net.get('h1')
    h2 = net.get('h2')
    result = h1.cmd('ping -c 3 10.0.67.2')
    print(result)
    
    # Show initial flow rules
    show_flow_rules(net)
    
    print("\n" + "="*60)
    print("*** Running Link Failure Experiment")
    print("="*60)
    link_flap_experiment(net)
    
    # Show flow rules after experiment
    print("\n*** Flow rules after link failure recovery:")
    show_flow_rules(net)
    
    # Optional: Enter CLI for manual inspection
    print("\n*** Experiment complete! Entering CLI for inspection.")
    print("*** Available commands:")
    print("    - h1 ping -c 5 h2")
    print("    - sh ovs-ofctl -O OpenFlow13 dump-flows s1")
    print("    - sh ovs-ofctl -O OpenFlow13 dump-flows s6")
    print("    - links")
    print("    - net")
    CLI(net)
    
    net.stop()

if __name__ == '__main__':
    main()
