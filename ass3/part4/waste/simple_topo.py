#!/usr/bin/env python3
"""
Simple SDN Topology for Part 4 Testing
6-switch ring topology with 2 hosts
"""

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
import time

def create_topology():
    """Create the network topology"""
    
    net = Mininet(controller=RemoteController, switch=OVSSwitch, link=TCLink)
    
    print("\n*** Adding controller")
    c0 = net.addController('c0', port=6653)
    
    print("*** Adding switches")
    s1 = net.addSwitch('s1', protocols='OpenFlow13')
    s2 = net.addSwitch('s2', protocols='OpenFlow13')
    s3 = net.addSwitch('s3', protocols='OpenFlow13')
    s4 = net.addSwitch('s4', protocols='OpenFlow13')
    s5 = net.addSwitch('s5', protocols='OpenFlow13')
    s6 = net.addSwitch('s6', protocols='OpenFlow13')
    
    print("*** Adding hosts")
    h1 = net.addHost('h1', ip='10.0.12.2/24', defaultRoute='via 10.0.12.1')
    h2 = net.addHost('h2', ip='10.0.67.2/24', defaultRoute='via 10.0.67.1')
    
    print("*** Adding links")
    # Host-switch links
    net.addLink(h1, s1, bw=10)
    net.addLink(h2, s6, bw=10)
    
    # Switch-switch links (ring topology)
    net.addLink(s1, s2, bw=10)  # Cost 10
    net.addLink(s2, s3, bw=10)  # Cost 10  
    net.addLink(s3, s6, bw=10)  # Cost 10
    net.addLink(s6, s5, bw=10)  # Cost 10
    net.addLink(s5, s4, bw=5)   # Cost 20 (lower bandwidth)
    net.addLink(s4, s1, bw=10)  # Cost 10
    
    print("\n*** Starting network")
    net.start()
    
    print("\n*** Waiting 10 seconds for topology discovery and flow installation...")
    time.sleep(10)
    
    print("\n============================================================")
    print("*** Network Ready!")
    print("============================================================")
    print("Available commands:")
    print("  h1 ping -c 3 h2              # Test connectivity")
    print("  h2 iperf -s &                # Start iperf server")
    print("  h1 iperf -c 10.0.67.2 -t 15 -i 1 &  # Start iperf client")
    print("  link s2 s3 down              # Simulate link failure")
    print("  link s2 s3 up                # Restore link")
    print("  dpctl dump-flows             # Show flow tables")
    print("============================================================\n")
    
    CLI(net)
    
    print("\n*** Stopping network")
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    create_topology()
