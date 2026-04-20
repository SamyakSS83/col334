from mininet.topo import Topo
from mininet.net import Mininet
from mininet.log import setLogLevel, info
from mininet.cli import CLI
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink   # ✅ import TCLink

class CustomTopo(Topo):
    def build(self):
        bw = 10  # bandwidth in Mbps
        # Add switches
        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')
        s3 = self.addSwitch('s3')
        s4 = self.addSwitch('s4')
        s5 = self.addSwitch('s5')
        s6 = self.addSwitch('s6')

        # Add hosts
        h1 = self.addHost('h1')
        h2 = self.addHost('h2')

        # Connect hosts
        self.addLink(h1, s1)
        self.addLink(h2, s6)

        # Connect switches with bandwidth limits
        self.addLink(s1, s2, bw=bw)
        self.addLink(s1, s3, bw=bw)
        self.addLink(s2, s4, bw=bw)
        self.addLink(s3, s5, bw=bw)
        self.addLink(s4, s6, bw=bw)
        self.addLink(s5, s6, bw=bw)

def run():
    topo = CustomTopo()
    net = Mininet(
        topo=topo,
        switch=OVSSwitch,
        link=TCLink,        # ✅ use TCLink to enforce bandwidth
        build=False,
        controller=None,
        autoSetMacs=True,
        autoStaticArp=True
    )
    net.addController('c0', controller=RemoteController, ip="127.0.0.1", protocol='tcp', port=6633)
    net.build()
    net.start()
    info('*** Running CLI\n')
    CLI(net)
    info('*** Stopping network\n')
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    run()
