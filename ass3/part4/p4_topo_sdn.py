#!/usr/bin/env python3
import time
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info

def hex_dpid(n: int) -> str:
    return f"{int(n):016x}"

def set_if(node, ifname, ip_cidr=None, mac=None):
    node.cmd(f'ip link set dev {ifname} down')
    node.cmd(f'ip addr flush dev {ifname}')
    if mac:
        node.cmd(f'ip link set dev {ifname} address {mac}')
    if ip_cidr:
        node.cmd(f'ip addr add {ip_cidr} dev {ifname}')
    node.cmd(f'ip link set dev {ifname} up')

def build():
    net = Mininet(
        controller=None, build=False, link=TCLink,
        autoSetMacs=False, autoStaticArp=False
    )

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

    info('*** Host <-> switch links (fixed names)\n')
    net.addLink(h1, s1, intfName1='h1-eth1', intfName2='s1-eth1', bw=10)  # 10.0.12.0/24

    info('*** Inter-switch ring links (fixed names)\n')
    net.addLink(s1, s2, intfName1='s1-eth2', intfName2='s2-eth1', bw=10)  # 10.0.13.0/24
    net.addLink(s2, s3, intfName1='s2-eth2', intfName2='s3-eth1', bw=10)  # 10.0.23.0/24
    net.addLink(s3, s6, intfName1='s3-eth2', intfName2='s6-eth1', bw=10)  # 10.0.36.0/24
    net.addLink(s4, s1, intfName1='s4-eth1', intfName2='s1-eth3', bw=10)  # 10.0.14.0/24
    net.addLink(s5, s4, intfName1='s5-eth1', intfName2='s4-eth2', bw=20)  # 10.0.45.0/24
    net.addLink(s6, s5, intfName1='s6-eth2', intfName2='s5-eth2', bw=10)  # 10.0.56.0/24
    
    # Add h2 link LAST so it gets the correct port number (eth3 = port 3)
    net.addLink(h2, s6, intfName1='h2-eth1', intfName2='s6-eth3', bw=10)  # 10.0.67.0/24

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

    info('*** SDN switches do not need IP config - controller handles everything\n')

    info('*** Waiting 10 seconds for topology discovery (LLDP)...\n')
    time.sleep(10)
    info('*** Network ready!\n')
    info('\nUseful commands:\n')
    info('  h1 ping -c 3 h2                    - Test connectivity\n')
    info('  h2 iperf -s &                      - Start iperf server\n')
    info('  h1 iperf -c 10.0.67.2 -t 15 -i 1   - Run iperf test\n')
    info('  link s2 s3 down                    - Simulate link failure\n')
    info('  link s2 s3 up                      - Restore link\n\n')

    CLI(net)
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    build()
