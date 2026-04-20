# Part 2 & Part 4 Link Failure Testing

## Overview

Automated Python scripts to test link failure handling and convergence in SDN controllers. These scripts orchestrate controller startup, topology creation, iperf testing, and link failures with detailed state management and logging.

---

## Part 2: L2 Shortest Path with Link Failure

### Files
- `test_link_failure.py` - Automated test orchestrator
- `p2_l2spf.py` - L2 controller with ECMP
- `p2bonus_l2spf.py` - L2 controller with load-aware ECMP
- `config.json` - Network topology configuration

### Quick Start

```bash
cd /home/threesamyak/col334/ass3/part2

# Test with standard L2SPF controller
sudo python3 test_link_failure.py --controller p2_l2spf.py

# Test with bonus (load-aware) controller
sudo python3 test_link_failure.py --controller p2bonus_l2spf.py

# Custom timing
sudo python3 test_link_failure.py --duration 40 --failure-at 15 --recovery-at 30
```

### What It Does

1. **Starts Ryu controller** with topology discovery
2. **Creates Mininet topology** (6 switches, 2 hosts in ring)
3. **Runs iperf client→server** with per-second throughput logging
4. **Triggers link failure** at specified time (default: 10s)
   - Brings down s2-s3 link (primary path)
   - Controller should detect and switch to alternate path (s1→s4→s5→s6)
5. **Recovers link** at specified time (default: 20s)
6. **Analyzes results**:
   - Baseline throughput
   - Failure detection time
   - Convergence time to alternate path
   - Recovery time
7. **Generates logs**:
   - `/tmp/part2_link_failure_test_<timestamp>.log` - Full test log
   - Includes timestamped throughput data

### Expected Behavior

```
Baseline: ~10 Mbps (primary path s1→s2→s3→s6)
   ↓
Failure at t=10s: Link s2-s3 goes down
   ↓
Detection: Controller detects via LLDP timeout (~5-10s)
   ↓
Convergence: Switches to alternate path s1→s4→s5→s6
   ↓
Recovery: ~8-10 Mbps on alternate path
   ↓
Link up at t=20s: Primary path restored
   ↓
Optional: Traffic may return to primary path
```

### Command-Line Options

```bash
--controller FILE      Controller file (default: p2_l2spf.py)
--config FILE         Config file (default: config.json)
--duration SECONDS    Total test duration (default: 30)
--failure-at SECONDS  When to fail link (default: 10)
--recovery-at SECONDS When to recover link (default: 20)
```

---

## Part 4: L3 SDN with Link Failure (vs OSPF comparison)

### Files
- `test_link_failure.py` - Automated test orchestrator
- `p3_controller.py` - L3 controller with link failure detection
- `p4_config.json` - Multi-subnet configuration
- `p3_topo-2.py` - OVS switch topology for SDN

### Quick Start

```bash
cd /home/threesamyak/col334/ass3/part4

# Run SDN link failure test
sudo python3 test_link_failure.py

# Custom timing for longer observation
sudo python3 test_link_failure.py --duration 40 --failure-at 12 --recovery-at 28
```

### What It Does

1. **Starts Ryu controller** with OpenFlow 1.3 and LLDP discovery
2. **Creates SDN topology** with OVS switches (protocols=OpenFlow13)
3. **Runs TCP iperf** h1→h2 with per-second logging
4. **Triggers link failure** (s2-s3, primary path)
5. **Monitors controller** for:
   - EventLinkDelete detection
   - Dijkstra recomputation
   - Flow clearing and reinstallation
6. **Recovers link** and observes reconvergence
7. **Generates detailed analysis**:
   - Phase 1: Baseline throughput
   - Phase 2: Failure detection and convergence time
   - Phase 3: Recovery behavior
8. **Outputs**:
   - `/tmp/part4_sdn_link_failure_<timestamp>.log` - Full log
   - `/tmp/part4_sdn_throughput_<timestamp>.csv` - Throughput data for plotting

### Expected SDN Behavior

```
Phase 1 - Baseline:
  ~10 Mbps on primary path (s1→s2→s3→s6, cost=30)

Phase 2 - Link Failure (t=10s):
  ⚠️  Link s2-s3 goes down
  ⏱️  LLDP timeout: ~5-10 seconds
  📉 Throughput drops to ~0 during detection
  🔄 Controller detects EventLinkDelete
  🧮 Recomputes paths: s1→s4→s5→s6 (cost=40)
  🔧 Clears all flows, reinstalls on new path
  ✅ Convergence: ~6-12 seconds total
  📈 Throughput recovers: ~8-10 Mbps on alternate path

Phase 3 - Link Recovery (t=20s):
  🟢 Link s2-s3 comes back up
  🔄 Controller detects EventLinkAdd
  📊 May recompute back to primary path
  ✅ Traffic continues (possibly on either path)
```

### Convergence Metrics

The script calculates:
- **Failure detection delay**: Time from link down to throughput drop
- **SDN convergence time**: Time from link down to throughput recovery
- **Comparison with OSPF**: OSPF typically converges in ~1-2s, SDN in ~6-12s due to LLDP timeout

### Output Files

**Log file** (`/tmp/part4_sdn_link_failure_*.log`):
```
[2025-10-13 15:30:10.123] [INFO] Starting Ryu SDN controller...
[2025-10-13 15:30:13.456] [INFO] Topology started...
[2025-10-13 15:30:25.789] [WARN] ⚠️  TRIGGERING LINK FAILURE at t=10.2s
[2025-10-13 15:30:30.123] [INFO] CONTROLLER: *** LINK FAILED: s2 <-> s3 ***
[2025-10-13 15:30:35.456] [INFO] ✅ Converged to alternate path at t=15.5s
...
```

**CSV file** (`/tmp/part4_sdn_throughput_*.csv`):
```csv
Time(s),Throughput(Mbps),Event
0.50,9.80,
1.52,9.95,
...
10.12,9.90,
10.89,0.00,LINK_DOWN
11.23,0.00,
...
15.67,8.50,
16.12,9.20,
...
20.34,9.10,LINK_UP
```

### Plotting Results

```bash
# Using gnuplot
gnuplot -e "set datafile separator ','; \
  set xlabel 'Time (s)'; set ylabel 'Throughput (Mbps)'; \
  plot '/tmp/part4_sdn_throughput_*.csv' using 1:2 with lines title 'SDN Throughput'; \
  pause -1"

# Or import CSV into Excel/Google Sheets for visualization
```

---

## Troubleshooting

### Controller doesn't start
- Check if Ryu is installed: `pip3 list | grep ryu`
- Ensure port 6633 is free: `sudo netstat -tulpn | grep 6633`

### Topology creation fails
- Clean up Mininet: `sudo mn -c`
- Check OVS: `sudo service openvswitch-switch status`
- Verify Python path: `which python3`

### No throughput data
- Check if iperf is installed: `which iperf`
- Verify Mininet hosts: `sudo ip netns list`
- Look for errors in `/tmp/part*_link_failure_*.log`

### Link failure not detected
- For Part 2: LLDP timeout is ~10s, be patient
- For Part 4: Check controller logs for EventLinkDelete
- Verify link is actually down: `sudo ip link show s2-eth2`

### Permission errors
- Run scripts with sudo: `sudo python3 test_link_failure.py`
- Check file permissions: `ls -l *.py`

---

## Architecture

### Part 2 Test Flow
```
┌─────────────────┐
│  test_link_     │
│  failure.py     │
└────────┬────────┘
         │
    ┌────┴─────┬──────────────┬────────────┐
    │          │              │            │
┌───▼────┐ ┌──▼──────┐ ┌────▼──────┐ ┌──▼─────────┐
│ Ryu    │ │ Mininet │ │  iperf    │ │ Link       │
│ Ctrl   │ │ Topo    │ │  Client   │ │ Failure    │
│        │ │         │ │  Server   │ │ Manager    │
└───┬────┘ └──┬──────┘ └────┬──────┘ └──┬─────────┘
    │         │              │            │
    └─────────┴──────────────┴────────────┘
                  Orchestration
```

### Key Features

1. **Parallel Execution**: Controller, topology, iperf, and failure trigger run concurrently
2. **Thread-Safe Logging**: All components log to shared buffer with locks
3. **State Management**: Tracks test phases, timing, and events
4. **Automatic Cleanup**: Kills processes and cleans Mininet on exit
5. **Detailed Analysis**: Calculates convergence times, packet loss, throughput ratios

---

## Comparison: Part 2 vs Part 4

| Aspect | Part 2 (L2SPF) | Part 4 (L3 SDN) |
|--------|----------------|-----------------|
| **OpenFlow** | OF 1.0 (in current code) | OF 1.3 |
| **Forwarding** | MAC-based (L2) | IP-based (L3) with MAC rewriting |
| **ECMP** | Controller-side hash | Could use select groups |
| **Link Discovery** | LLDP via `--observe-links` | LLDP via `--observe-links` |
| **Failure Detection** | LLDP timeout (~10s) | LLDP timeout + EventLinkDelete |
| **Path Recomputation** | Dijkstra with ECMP | Dijkstra on demand |
| **Expected Convergence** | ~10-15s | ~6-12s (optimized with flow caching) |
| **Test Focus** | ECMP distribution + failover | Inter-subnet routing + failover |

---

## Next Steps

1. **Run tests** with both Part 2 controllers (standard + bonus)
2. **Collect logs** and CSV files
3. **Plot throughput graphs** to visualize convergence
4. **Compare SDN convergence** with OSPF baseline (~1-2s)
5. **Document** findings in report:
   - Include graphs
   - Explain convergence time differences (LLDP vs OSPF Hello)
   - Discuss trade-offs (centralized SDN vs distributed OSPF)

---

## Advanced Usage

### Running Multiple Tests

```bash
# Batch testing with different controllers
for ctrl in p2_l2spf.py p2bonus_l2spf.py; do
  echo "Testing $ctrl..."
  sudo python3 test_link_failure.py --controller $ctrl --duration 25
  sleep 5
done
```

### Custom Topology Modifications

Edit the topology script section in `test_link_failure.py`:
```python
# Modify link costs
self.addLink(s1, s2, bw=20)  # Change bandwidth
self.addLink(s2, s3, bw=5, delay='10ms')  # Add delay
```

### Debugging Mode

```python
# In test_link_failure.py, change log level filtering
if 'DEBUG' in line_strip or 'INFO' in line_strip:
    self.log(f"CONTROLLER: {line_strip}", "DEBUG")
```

---

## Authors
COL334 Assignment 3 - SDN Link Failure Testing
October 2025
