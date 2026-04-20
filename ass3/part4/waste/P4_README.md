# Part 4 SDN Link Failure Testing

This directory contains the L3 SDN controller with link failure detection and an automated test runner.

## Files

- **`p4_l3spf_lf.py`** - Ryu controller implementing L3 shortest path routing with link failure detection (Part 4 submission)
- **`p4_config.json`** - Network configuration (switches, hosts, links, costs)
- **`p3_topo-2.py`** - Mininet topology (for manual testing)
- **`p4_runner_sdn.py`** - Automated test orchestrator (starts controller, runs tests, saves logs)
- **`p4_runner.py`** - OSPF test runner (for comparison with Part 4)
- **`p4_ospf.py`** - OSPF helper functions
- **`p4_topo.py`** - OSPF topology

## Quick Start - Manual Testing

### Terminal 1: Start Controller
```bash
cd ~/col334/ass3
ryu-manager --observe-links ryu.topology.switches part4/p4_l3spf_lf.py
```

### Terminal 2: Start Topology
```bash
cd ~/col334/ass3/part4
sudo python3 p3_topo-2.py
```

### In Mininet CLI:
```bash
# Test connectivity
mininet> h1 ping -c 5 h2

# View flow rules
mininet> s1 ovs-ofctl -O OpenFlow13 dump-flows s1

# Simulate link failure
mininet> link s2 s3 down
mininet> h1 ping -c 3 h2   # Should still work via alternate path

# Restore link
mininet> link s2 s3 up

# Run iperf test
mininet> h2 iperf -s &
mininet> h1 iperf -c 10.0.67.2 -t 15 -i 1
```

## Automated Testing with Link Failure

The `p4_runner_sdn.py` script automates the entire test process:

### Basic Usage
```bash
cd ~/col334/ass3/part4
sudo python3 p4_runner_sdn.py
```

This will:
1. ✅ Start Ryu controller in background
2. ✅ Build Mininet topology
3. ✅ Wait for topology discovery (15s)
4. ✅ Save initial flow tables
5. ✅ Start iperf test (15s total)
6. ✅ Fail link s2-s3 at 2s
7. ✅ Keep link down for 5s
8. ✅ Restore link at 7s
9. ✅ Complete iperf test
10. ✅ Save final flow tables
11. ✅ Generate summary with all logs

### Custom Parameters
```bash
# 30 second test, fail at 10s, down for 8s
sudo python3 p4_runner_sdn.py --iperf-duration 30 --failure-time 10 --failure-duration 8

# Fail different link
sudo python3 p4_runner_sdn.py --link-switch1 s4 --link-switch2 s1 \
    --link-intf1 s4-eth1 --link-intf2 s1-eth3

# Skip CLI, exit immediately after test
sudo python3 p4_runner_sdn.py --no-cli

# Custom output directory
sudo python3 p4_runner_sdn.py --output-dir ./results
```

### Output Files

All logs saved to `/tmp/` (or `--output-dir`) with timestamp:

- **`sdn_controller_YYYYMMDD_HHMMSS.log`** - Full Ryu controller output
- **`sdn_flows_initial_YYYYMMDD_HHMMSS.log`** - Flow tables before test
- **`sdn_flows_YYYYMMDD_HHMMSS.log`** - Flow tables after test
- **`sdn_h1_iperf_YYYYMMDD_HHMMSS.log`** - iperf client output
- **`sdn_h2_iperf_YYYYMMDD_HHMMSS.log`** - iperf server output
- **`sdn_summary_YYYYMMDD_HHMMSS.log`** - Combined summary with all info

### Example Output

```
*** Starting Ryu controller (logs to /tmp/sdn_controller_20251013_143052.log)
*** Ryu controller started (PID 12345)
*** Building Mininet topology...
*** Waiting for topology discovery (LLDP)...
✅ Initial connectivity established

*** Starting iperf test (duration=15s)
*** Waiting 2s before link failure...

*** LINK FAILURE: s2:s2-eth2 <-> s3:s3-eth1
*** Link down for 5s...

*** LINK RECOVERY: s2:s2-eth2 <-> s3:s3-eth1
*** Waiting 8s for iperf to complete...

=== IPERF CLIENT (h1) ===
Client connecting to 10.0.67.2, TCP port 5001
[  3] local 10.0.12.2 port 54321 connected with 10.0.67.2 port 5001
[ ID] Interval       Transfer     Bandwidth
[  3]  0.0- 1.0 sec  1.12 MBytes  9.44 Mbits/sec
[  3]  1.0- 2.0 sec  1.25 MBytes  10.5 Mbits/sec
[  3]  2.0- 3.0 sec   768 KBytes  6.29 Mbits/sec  # Link failed
[  3]  3.0- 4.0 sec   896 KBytes  7.34 Mbits/sec  # Rerouting
[  3]  4.0- 5.0 sec  1.12 MBytes  9.44 Mbits/sec  # New path
...
```

## Network Topology

```
      h1 (10.0.12.2/24)
       |
      s1 ----------- s2 ----------- s3
       |             10             10
      10                            |
       |                           s6 --- h2 (10.0.67.2/24)
      s4                            |
       |                            10
      20                            |
       |                           s5
      s5 -------------------------/
               10
```

**Primary path (cost 30):** h1 → s1 → s2 → s3 → s6 → h2  
**Alternate path (cost 50):** h1 → s1 → s4 → s5 → s6 → h2

When s2-s3 link fails, traffic automatically reroutes via s4-s5.

## Key Features of p4_l3spf_lf.py

✅ **L3 Routing** - Inter-subnet forwarding with proper MAC rewriting  
✅ **Dijkstra SPF** - Shortest path computation  
✅ **TTL Decrement** - Proper IP packet handling  
✅ **ARP Handling** - Gateway ARP responses  
✅ **Link Failure Detection** - LLDP-based topology monitoring  
✅ **Automatic Rerouting** - Recomputes paths and updates flows on link failure  
✅ **Flow Table Management** - Clears old flows, installs new paths  

## Comparing with OSPF (Part 4 Requirements)

### Run OSPF Test
```bash
cd ~/col334/ass3/part4
sudo python3 p4_runner.py --input-file p4_config.json
```

### Comparison Metrics

1. **Convergence Time**
   - SDN: ~10s (LLDP timeout + flow installation)
   - OSPF: Varies based on timers (typically 5-40s)

2. **Throughput During Failure**
   - Check iperf logs for bandwidth during 2-7s window
   - Both should reroute successfully

3. **Flow Rules vs Routing Tables**
   - SDN: Check `sdn_flows_*.log` files
   - OSPF: Use `s1 ip route` in CLI

## Troubleshooting

### Controller won't start
```bash
# Check if port 6633 is in use
sudo netstat -tlnp | grep 6633

# Kill old controller
sudo pkill -9 ryu-manager
```

### Switches not connecting
```bash
# Check OVS
sudo ovs-vsctl show

# Clean up old mininet
sudo mn -c
```

### No connectivity
```bash
# Check controller logs
tail -f /tmp/sdn_controller_*.log

# Verify flows installed
sudo ovs-ofctl -O OpenFlow13 dump-flows s1
```

### Permission denied
```bash
# Runner needs sudo for Mininet
sudo python3 p4_runner_sdn.py
```

## Assignment Submission

Per `COL334_A3_2501.txt`:

**Part 4 requires:**
- ✅ Controller code: `p4_l3spf_lf.py`
- ✅ Comparison results in report (throughput, convergence, logs)

**Use runner to generate data:**
```bash
# Run SDN test
sudo python3 p4_runner_sdn.py --no-cli

# Run OSPF test  
sudo python3 p4_runner.py --input-file p4_config.json --no-cli

# Compare the logs in /tmp/
```

## Examples for Report

### Get convergence time from logs
```bash
# SDN: Check controller log for "LINK FAILED" → "Installed flow"
grep -A 20 "LINK FAILED" /tmp/sdn_controller_*.log

# OSPF: Check for LSA exchanges
# (OSPF logs show routing protocol messages)
```

### Get throughput during failure
```bash
# Extract per-second bandwidth from iperf client log
grep "sec" /tmp/sdn_h1_iperf_*.log
```

### Compare flow rules vs routing tables
```bash
# SDN flows (before/after failure)
diff /tmp/sdn_flows_initial_*.log /tmp/sdn_flows_*.log

# OSPF routing table changes
# (captured in OSPF runner logs)
```

---

**For questions, check:**
- Assignment PDF: `COL334_A3_2501.txt`
- Ryu docs: https://ryu.readthedocs.io/
- OpenFlow 1.3 spec: https://www.opennetworking.org/
