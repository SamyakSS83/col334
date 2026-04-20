#!/usr/bin/env python3

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel
from mininet.link import TCLink
from mininet.clean import cleanup

DEFAULT_CLIENTS = 10

BANDWIDTH = 1
DELAY = "5ms"
QUEUE_PKTS = 50

class SimpleTopo(Topo):
    def __init__(self, num_clients=DEFAULT_CLIENTS):
        Topo.__init__(self)

        switch = self.addSwitch('s1', cls=OVSSwitch)
        server = self.addHost('server', ip='10.0.0.100')
        
        clients = []
        for i in range(num_clients):
            client = self.addHost(f'client{i+1}', ip=f'10.0.0.{i+1}')
            clients.append(client)
        
        self.addLink(
            server, switch,
            bw=BANDWIDTH,
            delay=DELAY,
            max_queue_size=QUEUE_PKTS,
            use_htb=True
        )

        for client in clients:
            self.addLink(
                client, switch,
                bw=BANDWIDTH,
                delay=DELAY,
                max_queue_size=QUEUE_PKTS,
                use_htb=True
            )

def create_network(num_clients=DEFAULT_CLIENTS):
    cleanup()

    topo = SimpleTopo(num_clients)
    net = Mininet(topo=topo, switch=OVSSwitch, link=TCLink, autoSetMacs=True, waitConnected=True)
    net.start()
    return net

if __name__ == '__main__':
    setLogLevel('info')
    
    print(f"Creating network with {DEFAULT_CLIENTS} clients")
    print(f"All links bandwidth: {BANDWIDTH} Mbps")
    print(f"All links delay: {DELAY}")
    print(f"All links queue: {QUEUE_PKTS} packets")

    
    net = create_network()
    
    print("Network created successfully!")
    print("Hosts:", [h.name for h in net.hosts])
    print("Links:", [(link.intf1.node, link.intf2.node) for link in net.links])
    
    CLI(net)
    net.stop()
