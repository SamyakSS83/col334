#!/usr/bin/env python3
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import OVSBridge
from mininet.link import TCLink
from mininet.clean import cleanup

class WordCountTopo(Topo):
    def build(self):
        s1 = self.addSwitch('s1')
        h1 = self.addHost('h1', ip='10.0.0.1/24')
        h2 = self.addHost('h2', ip='10.0.0.2/24')
        self.addLink(h1, s1, cls=TCLink, bw=100)
        self.addLink(h2, s1, cls=TCLink, bw=100)

def make_net():
    cleanup()  # clean stale interfaces first
    return Mininet(topo=WordCountTopo(), controller=None, switch=OVSBridge,
                   autoSetMacs=True, autoStaticArp=True, build=True)
