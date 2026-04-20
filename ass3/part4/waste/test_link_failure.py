#!/usr/bin/env python3
"""
Part 4 SDN Link Failure Testing Script  
Orchestrates controller, topology, TCP iperf, and link failures with detailed convergence analysis
"""

import os
import sys
import time
import signal
import subprocess
import threading
import json
from datetime import datetime

class Part4LinkFailureTest:
    def __init__(self, config_file='p4_config.json'):
        self.config_file = config_file
        self.topology_proc = None
        self.test_results = []
        self.lock = threading.Lock()
        self.test_start_time = None
        
    def log(self, message, level="INFO"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with self.lock:
            msg = f"[{timestamp}] [{level}] {message}"
            print(msg)
            self.test_results.append(msg)
    
    def wait_for_controller(self, timeout=10):
        """Wait for controller to be ready on port 6633"""
        self.log("Waiting for controller to be ready on port 6633...")
        import socket
        start = time.time()
        while time.time() - start < timeout:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                result = sock.connect_ex(('127.0.0.1', 6633))
                sock.close()
                if result == 0:
                    self.log("✅ Controller is ready!")
                    return True
            except:
                pass
            time.sleep(0.5)
        self.log("⚠️  Controller not detected, proceeding anyway...", "WARN")
        return False
    
    def start_topology(self):
        """Start Mininet topology with OVS switches for SDN"""
        self.log("Starting Part 4 SDN topology...")
        
        # Use p3_topo-2.py directly
        topo_script_path = os.path.join(os.path.dirname(__file__), 'p3_topo-2.py')
        
        if not os.path.exists(topo_script_path):
            # Create topology script if it doesn't exist
            self.log("Creating topology script...", "WARN")
            topo_script = """
#!/usr/bin/env python3
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel
import time

class Part4Topo(Topo):
    def build(self):
        # 6 switches in ring topology
        s1 = self.addSwitch('s1', dpid='0000000000000001', protocols='OpenFlow13')
        s2 = self.addSwitch('s2', dpid='0000000000000002', protocols='OpenFlow13')
        s3 = self.addSwitch('s3', dpid='0000000000000003', protocols='OpenFlow13')
        s4 = self.addSwitch('s4', dpid='0000000000000004', protocols='OpenFlow13')
        s5 = self.addSwitch('s5', dpid='0000000000000005', protocols='OpenFlow13')
        s6 = self.addSwitch('s6', dpid='0000000000000006', protocols='OpenFlow13')
        
        # 2 hosts
        h1 = self.addHost('h1', ip='10.0.12.2/24', mac='00:00:00:00:01:02')
        h2 = self.addHost('h2', ip='10.0.67.2/24', mac='00:00:00:00:06:02')
        
        # Host connections
        self.addLink(h1, s1, intfName1='h1-eth0', intfName2='s1-eth1')
        self.addLink(h2, s6, intfName1='h2-eth0', intfName2='s6-eth3')
        
        # Ring topology links with bandwidth
        self.addLink(s1, s2, intfName1='s1-eth2', intfName2='s2-eth1', bw=10)
        self.addLink(s2, s3, intfName1='s2-eth2', intfName2='s3-eth1', bw=10)
        self.addLink(s3, s6, intfName1='s3-eth2', intfName2='s6-eth1', bw=10)
        self.addLink(s6, s5, intfName1='s6-eth2', intfName2='s5-eth1', bw=10)
        self.addLink(s5, s4, intfName1='s5-eth2', intfName2='s4-eth2', bw=20)
        self.addLink(s4, s1, intfName1='s4-eth1', intfName2='s1-eth3', bw=10)

setLogLevel('info')
topo = Part4Topo()
net = Mininet(
    topo=topo,
    switch=OVSSwitch,
    link=TCLink,
    controller=lambda name: RemoteController(name, ip='127.0.0.1', port=6633),
    autoSetMacs=False,
    autoStaticArp=False
)
net.start()
print("TOPOLOGY_READY", flush=True)
time.sleep(10)  # Wait for LLDP discovery
print("LLDP_DISCOVERED", flush=True)

# Keep running
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    net.stop()
"""
            with open(topo_script_path, 'w') as f:
                f.write(topo_script)
            os.chmod(topo_script_path, 0o755)
        
        self.topology_proc = subprocess.Popen(
            ['sudo', 'python3', topo_script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        
        # Wait for topology ready
        for line in iter(self.topology_proc.stdout.readline, ''):
            self.log(f"TOPOLOGY: {line.strip()}", "DEBUG")
            if "LLDP_DISCOVERED" in line:
                break
            elif "TOPOLOGY_READY" in line:
                time.sleep(10)  # Additional wait for LLDP if not already done
                break
        
        self.log("Topology started and LLDP discovery complete")
    
    def run_iperf_tcp_test(self, duration=30, failure_at=10, recovery_at=20):
        """Run TCP iperf test with link failure during connection"""
        self.log(f"Starting TCP iperf test (duration={duration}s, fail@{failure_at}s, recover@{recovery_at}s)")
        self.test_start_time = time.time()
        
        # Start iperf server on h2
        self.log("Starting iperf TCP server on h2...")
        server_cmd = ['sudo', 'mnexec', '-a', '2', 'iperf', '-s', '-i', '1']
        server_proc = subprocess.Popen(
            server_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        
        time.sleep(2)
        
        # Start iperf TCP client on h1
        self.log("Starting iperf TCP client on h1 (10.0.12.2 → 10.0.67.2)...")
        client_cmd = ['sudo', 'mnexec', '-a', '1', 'iperf', '-c', '10.0.67.2',
                      '-t', str(duration), '-i', '1', '-f', 'm']
        
        client_proc = subprocess.Popen(
            client_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        
        # Track throughput data
        throughputs = []
        link_down_time = None
        link_up_time = None
        
        def trigger_link_failure():
            nonlocal link_down_time, link_up_time
            
            # Wait for baseline
            time.sleep(failure_at)
            elapsed = time.time() - self.test_start_time
            self.log(f"⚠️  ⚠️  ⚠️  TRIGGERING LINK FAILURE (s2-s3) at t={elapsed:.1f}s ⚠️  ⚠️  ⚠️", "WARN")
            link_down_time = elapsed
            
            # Bring down primary path link s2-s3
            subprocess.run(['sudo', 'ip', 'link', 'set', 's2-eth2', 'down'], capture_output=True)
            subprocess.run(['sudo', 'ip', 'link', 'set', 's3-eth1', 'down'], capture_output=True)
            
            self.log("🔴 Link s2-s3 brought DOWN (primary path broken)")
            self.log("   Expected: Controller should detect via LLDP timeout and switch to s1→s4→s5→s6")
            
            # Wait for recovery time
            time.sleep(recovery_at - failure_at)
            elapsed = time.time() - self.test_start_time
            self.log(f"✅ ✅ ✅ RECOVERING LINK (s2-s3) at t={elapsed:.1f}s ✅ ✅ ✅", "INFO")
            link_up_time = elapsed
            
            subprocess.run(['sudo', 'ip', 'link', 'set', 's2-eth2', 'up'], capture_output=True)
            subprocess.run(['sudo', 'ip', 'link', 'set', 's3-eth1', 'up'], capture_output=True)
            
            self.log("🟢 Link s2-s3 brought UP (primary path restored)")
        
        failure_thread = threading.Thread(target=trigger_link_failure, daemon=True)
        failure_thread.start()
        
        # Parse iperf output
        for line in iter(client_proc.stdout.readline, ''):
            if not line:
                break
            
            elapsed = time.time() - self.test_start_time
            
            # Parse throughput
            if 'bits/sec' in line.lower() and '-' in line and 'sec' in line:
                parts = line.strip().split()
                try:
                    # Find throughput value
                    for i, part in enumerate(parts):
                        if 'bits/sec' in part.lower():
                            throughput_str = parts[i-1]
                            throughput = float(throughput_str)
                            if 'Kbits' in part:
                                throughput /= 1000
                            
                            throughputs.append((elapsed, throughput))
                            
                            # Highlight significant events
                            if link_down_time and abs(elapsed - link_down_time) < 2:
                                self.log(f"📉 t={elapsed:.1f}s | Throughput: {throughput:.2f} Mbits/sec (at failure)", "WARN")
                            elif link_up_time and abs(elapsed - link_up_time) < 2:
                                self.log(f"📈 t={elapsed:.1f}s | Throughput: {throughput:.2f} Mbits/sec (at recovery)", "INFO")
                            else:
                                self.log(f"📊 t={elapsed:.1f}s | Throughput: {throughput:.2f} Mbits/sec")
                            break
                except (ValueError, IndexError):
                    pass
        
        client_proc.wait()
        server_proc.terminate()
        failure_thread.join(timeout=1)
        
        self.log("TCP iperf test completed")
        
        # Analyze results
        self.analyze_tcp_results(throughputs, failure_at, recovery_at, link_down_time, link_up_time)
    
    def analyze_tcp_results(self, throughputs, failure_at, recovery_at, link_down_time, link_up_time):
        """Detailed analysis of TCP throughput during link failure"""
        self.log("\n" + "="*70)
        self.log("PART 4 SDN LINK FAILURE TEST RESULTS")
        self.log("="*70)
        
        if not throughputs:
            self.log("❌ No throughput data collected", "ERROR")
            return
        
        # Phase 1: Baseline (before failure)
        baseline = [tp for t, tp in throughputs if t < failure_at - 1]
        avg_baseline = sum(baseline) / len(baseline) if baseline else 0
        self.log(f"\n📊 Phase 1 - Baseline (before failure):")
        self.log(f"   Average throughput: {avg_baseline:.2f} Mbits/sec")
        self.log(f"   Samples: {len(baseline)}")
        
        # Phase 2: During failure (detection and convergence)
        if link_down_time:
            failure_window = [(t, tp) for t, tp in throughputs 
                             if link_down_time <= t < recovery_at]
            
            self.log(f"\n⚠️  Phase 2 - Link failure & convergence:")
            self.log(f"   Link failed at: t={link_down_time:.1f}s")
            
            # Find when throughput drops
            drop_detected = None
            for t, tp in throughputs:
                if t > link_down_time and tp < avg_baseline * 0.3:
                    drop_detected = t
                    self.log(f"   📉 Throughput drop detected at: t={t:.1f}s (delay: {t - link_down_time:.1f}s)")
                    break
            
            # Find convergence (when throughput recovers to reasonable level)
            converged = None
            for t, tp in throughputs:
                if drop_detected and t > drop_detected + 2 and tp > avg_baseline * 0.7:
                    converged = t
                    self.log(f"   ✅ Converged to alternate path at: t={t:.1f}s")
                    self.log(f"   ⏱️  SDN CONVERGENCE TIME: {t - link_down_time:.1f}s")
                    self.log(f"   📈 Recovered throughput: {tp:.2f} Mbits/sec ({tp/avg_baseline*100:.1f}% of baseline)")
                    break
            
            if not converged:
                self.log(f"   ⚠️  No convergence detected (throughput remained low)", "WARN")
            
            # Average during failure window
            if failure_window:
                avg_during_failure = sum(tp for _, tp in failure_window) / len(failure_window)
                self.log(f"   Average during failure window: {avg_during_failure:.2f} Mbits/sec")
        
        # Phase 3: After recovery
        if link_up_time:
            recovery_window = [(t, tp) for t, tp in throughputs if t > recovery_at]
            if recovery_window:
                avg_recovered = sum(tp for _, tp in recovery_window) / len(recovery_window)
                self.log(f"\n🟢 Phase 3 - After link recovery:")
                self.log(f"   Link recovered at: t={link_up_time:.1f}s")
                self.log(f"   Average throughput: {avg_recovered:.2f} Mbits/sec")
                self.log(f"   Recovery ratio: {avg_recovered/avg_baseline*100:.1f}% of baseline")
        
        # Overall statistics
        self.log(f"\n" + "-"*70)
        self.log("📈 OVERALL STATISTICS")
        self.log("-"*70)
        self.log(f"Total test duration: {throughputs[-1][0]:.1f}s")
        self.log(f"Total samples: {len(throughputs)}")
        self.log(f"Overall average: {sum(tp for _, tp in throughputs) / len(throughputs):.2f} Mbits/sec")
        self.log(f"Maximum throughput: {max(tp for _, tp in throughputs):.2f} Mbits/sec")
        self.log(f"Minimum throughput: {min(tp for _, tp in throughputs):.2f} Mbits/sec")
        
        # Calculate packet loss periods
        zero_throughput = [(t, tp) for t, tp in throughputs if tp < 0.1]
        if zero_throughput:
            self.log(f"⚠️  Zero/near-zero throughput intervals: {len(zero_throughput)}")
            self.log(f"   Total downtime: ~{len(zero_throughput)}s")
        
        # Write detailed results
        timestamp = int(time.time())
        log_file = f"/tmp/part4_sdn_link_failure_{timestamp}.log"
        csv_file = f"/tmp/part4_sdn_throughput_{timestamp}.csv"
        
        with open(log_file, 'w') as f:
            f.write('\n'.join(self.test_results))
        
        with open(csv_file, 'w') as f:
            f.write("Time(s),Throughput(Mbps),Event\n")
            for t, tp in throughputs:
                event = ""
                if link_down_time and abs(t - link_down_time) < 0.5:
                    event = "LINK_DOWN"
                elif link_up_time and abs(t - link_up_time) < 0.5:
                    event = "LINK_UP"
                f.write(f"{t:.2f},{tp:.2f},{event}\n")
        
        self.log(f"\n📝 Detailed log: {log_file}")
        self.log(f"📊 Throughput CSV: {csv_file}")
        self.log(f"\n💡 Use: gnuplot or Excel to plot {csv_file}")
    
    def cleanup(self):
        """Clean up processes"""
        self.log("\nCleaning up...")
        
        if self.topology_proc:
            self.topology_proc.terminate()
            time.sleep(1)
            subprocess.run(['sudo', 'mn', '-c'], capture_output=True)
        
        if self.controller_proc:
            self.controller_proc.terminate()
        
        time.sleep(2)
        self.log("✅ Cleanup completed")
    
    def run_full_test(self, duration=30, failure_at=10, recovery_at=20):
        """Run complete Part 4 SDN link failure test"""
        try:
            self.wait_for_controller()
            self.start_topology()
            self.run_iperf_tcp_test(duration, failure_at, recovery_at)
        except KeyboardInterrupt:
            self.log("⚠️  Test interrupted by user", "WARN")
        except Exception as e:
            self.log(f"❌ Test failed with error: {e}", "ERROR")
            import traceback
            traceback.print_exc()
        finally:
            self.cleanup()

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Part 4 SDN Link Failure Test with TCP iperf')
    parser.add_argument('--config', default='p4_config.json',
                       help='Config file (default: p4_config.json)')
    parser.add_argument('--duration', type=int, default=30,
                       help='Test duration in seconds (default: 30)')
    parser.add_argument('--failure-at', type=int, default=10,
                       help='When to trigger link failure (default: 10s)')
    parser.add_argument('--recovery-at', type=int, default=20,
                       help='When to recover link (default: 20s)')
    
    args = parser.parse_args()
    
    print("="*70)
    print("        Part 4 SDN Link Failure Testing Script")
    print("        TCP iperf with Link Down/Up During Connection")
    print("="*70)
    print("⚠️  IMPORTANT: Start your Ryu controller first!")
    print("    Run in separate terminal:")
    print("    $ cd part4")
    print("    $ ryu-manager --observe-links ryu.topology.switches p3_controller.py")
    print("="*70)
    print(f"Config:           {args.config}")
    print(f"Test duration:    {args.duration}s")
    print(f"Link failure at:  {args.failure_at}s")
    print(f"Link recovery at: {args.recovery_at}s")
    print(f"\nTopology: s1→s2→s3→s6 (primary, cost=30)")
    print(f"          s1→s4→s5→s6 (backup, cost=40)")
    print("="*70 + "\n")
    
    tester = Part4LinkFailureTest(args.config)
    tester.run_full_test(args.duration, args.failure_at, args.recovery_at)

if __name__ == '__main__':
    main()
