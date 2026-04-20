from mininet.topo import Topo
from mininet.net import Mininet
from mininet.log import setLogLevel, info
from mininet.cli import CLI
from mininet.node import RemoteController, OVSSwitch

class Ring6(Topo):
    def build(self):
        switches = [self.addSwitch(f's{i}') for i in range(1,7)]
        h1 = self.addHost('h1')
        h2 = self.addHost('h2')
        self.addLink(h1, switches[0])
        self.addLink(h2, switches[3])
        for i in range(6):
            self.addLink(switches[i], switches[(i+1)%6])

topos = { 'ring6': Ring6 }
def run():
    """Create the network, start it, and enter the CLI."""
    topo = Ring6()
    net = Mininet(topo=topo, switch=OVSSwitch, build=False, controller=None,
              autoSetMacs=True, autoStaticArp=True)
    net.addController('c0', controller=RemoteController, ip="127.0.0.1", protocol='tcp', port=6633)
    net.build()
    net.start()
    info('*** Running CLI\n')
    CLI(net)


    info('*** Stopping network\n')
    net.stop()

# Example command to run: sudo python3 part1/p1_topo.py
if __name__ == '__main__':
    # Set log level to display Mininet output
    setLogLevel('info')
    run()

