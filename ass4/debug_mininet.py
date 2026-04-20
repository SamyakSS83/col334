#!/usr/bin/env python3
"""Quick test with just one iteration to debug the Mininet setup"""

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.link import TCLink
from mininet.node import RemoteController
from mininet.log import setLogLevel
from mininet.node import Controller
import time

class CustomTopo(Topo):
    def build(self, loss, delay, jitter):
        h1 = self.addHost('h1')
        h2 = self.addHost('h2')
        s1 = self.addSwitch('s1')
        self.addLink(h1, s1, loss=loss, delay=f'{delay}ms', jitter=f'{jitter}ms')
        self.addLink(h2, s1, loss=0)

if __name__ == "__main__":
    setLogLevel('info')
    
    SERVER_IP = "10.0.0.1"
    SERVER_PORT = 6555
    SWS = 65536
    
    print(f"\n{'='*60}")
    print("Quick debug test - Single transfer")
    print(f"{'='*60}\n")
    
    topo = CustomTopo(loss=1, delay=20, jitter=0)
    net = Mininet(topo=topo, link=TCLink, controller=Controller)
    net.start()
    
    h1 = net.get('h1')
    h2 = net.get('h2')
    
    print(f"h1 IP: {h1.IP()}")
    print(f"h2 IP: {h2.IP()}")
    
    # Test connectivity
    print("\nTesting connectivity...")
    result = h2.cmd(f"ping -c 2 {h1.IP()}")
    print(result)
    
    # Check working directories and files
    print("\n--- Checking h1 (server) ---")
    print(f"Working dir: {h1.cmd('pwd').strip()}")
    print(f"Python scripts: {h1.cmd('ls -lh p1_*.py').strip()}")
    print(f"Data file: {h1.cmd('ls -lh data.txt 2>&1').strip()}")
    
    print("\n--- Checking h2 (client) ---")
    print(f"Working dir: {h2.cmd('pwd').strip()}")
    print(f"Python scripts: {h2.cmd('ls -lh p1_*.py').strip()}")
    
    # Start server
    print(f"\n--- Starting server on {SERVER_IP}:{SERVER_PORT} ---")
    h1.cmd(f"python3 p1_server.py {SERVER_IP} {SERVER_PORT} {SWS} > /tmp/server.log 2>&1 &")
    time.sleep(2)
    
    # Check if server is running
    ps_result = h1.cmd("ps aux | grep p1_server | grep -v grep")
    print(f"Server process: {ps_result.strip()}")
    
    # Start client
    print(f"\n--- Starting client ---")
    start = time.time()
    client_output = h2.cmd(f"python3 p1_client.py {SERVER_IP} {SERVER_PORT}")
    elapsed = time.time() - start
    
    print(f"\nClient output:\n{client_output}")
    print(f"Transfer time: {elapsed:.2f}s")
    
    # Check for received file
    print(f"\n--- Checking for received_data.txt ---")
    print(f"In current dir: {h2.cmd('ls -lh received_data.txt 2>&1').strip()}")
    print(f"In /tmp: {h2.cmd('ls -lh /tmp/received_data.txt 2>&1').strip()}")
    print(f"In /root: {h2.cmd('ls -lh /root/received_data.txt 2>&1').strip()}")
    
    # Get server log
    server_log = h1.cmd("cat /tmp/server.log")
    print(f"\n--- Server log ---\n{server_log}")
    
    net.stop()
    print("\n" + "="*60)
    print("Test complete")
    print("="*60)
