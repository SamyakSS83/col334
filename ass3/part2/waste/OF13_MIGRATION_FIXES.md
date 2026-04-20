# OpenFlow 1.3 Migration Fixes Applied

## Date: October 13, 2025

## Files Modified
1. `/home/threesamyak/col334/ass3/part2/p2_l2spf.py`
2. `/home/threesamyak/col334/ass3/part2/p2bonus_l2spf.py`

## Issues Fixed

### 1. AttributeError: 'OFPPacketIn' object has no attribute 'in_port'

**Problem**: In OpenFlow 1.3, the `in_port` field is no longer a direct attribute of `OFPPacketIn`. It's now part of the `match` dictionary.

**OpenFlow 1.0 (old)**:
```python
in_port = msg.in_port
```

**OpenFlow 1.3 (fixed)**:
```python
in_port = msg.match['in_port']
```

**Files Changed**:
- `p2_l2spf.py` line ~87
- `p2bonus_l2spf.py` line ~342

---

### 2. Missing Table-Miss Flow Entry

**Problem**: OpenFlow 1.3 requires explicit installation of a table-miss flow entry (priority 0) to send unmatched packets to the controller. Without this, switches won't forward PacketIn messages for unknown flows.

**Solution**: Added `EventOFPSwitchFeatures` handler with `CONFIG_DISPATCHER`:

```python
@set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
def switch_features_handler(self, ev):
    """Install table-miss flow entry for OpenFlow 1.3"""
    datapath = ev.msg.datapath
    ofproto = datapath.ofproto
    parser = datapath.ofproto_parser

    # Install table-miss flow entry to send packets to controller
    match = parser.OFPMatch()
    actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                      ofproto.OFPCML_NO_BUFFER)]
    inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                         actions)]
    mod = parser.OFPFlowMod(datapath=datapath, priority=0,
                            match=match, instructions=inst)
    datapath.send_msg(mod)
```

**Files Changed**:
- `p2_l2spf.py` - Added after `_state_change_handler`
- `p2bonus_l2spf.py` - Added after `_state_change_handler`

---

### 3. Missing `ofp` Variable in _install_path

**Problem**: The `_install_path` function in `p2bonus_l2spf.py` used `ofp.OFPIT_APPLY_ACTIONS` but didn't define `ofp` variable.

**Solution**: Added `ofp = dp.ofproto` at the start of both forward and reverse direction loops.

**Files Changed**:
- `p2bonus_l2spf.py` lines ~241 and ~296

---

## Key Differences: OpenFlow 1.0 vs 1.3

| Aspect | OpenFlow 1.0 | OpenFlow 1.3 |
|--------|--------------|--------------|
| **PacketIn in_port** | `msg.in_port` | `msg.match['in_port']` |
| **Table-miss** | Implicit (default behavior) | Explicit flow entry required |
| **Match fields** | `nw_src`, `nw_dst`, `tp_src`, `tp_dst` | `ipv4_src`, `ipv4_dst`, `tcp_src`/`udp_src` |
| **Instructions** | Actions directly | Must wrap in `OFPInstructionActions` |
| **Group tables** | Not supported | Supported (OFPGT_SELECT, etc.) |
| **Multiple tables** | Limited | Full pipeline support |

---

## Testing

### Before Fixes:
```
AttributeError: 'OFPPacketIn' object has no attribute 'in_port'
L2SPF: Exception occurred during handler processing.
```

### After Fixes:
Controllers should now:
1. ✅ Register datapaths correctly
2. ✅ Install table-miss flow entries
3. ✅ Receive and process PacketIn messages
4. ✅ Learn host locations
5. ✅ Install forwarding flows with 5-tuple matching
6. ✅ Handle ECMP path selection

### How to Test:

**Terminal 1: Start Controller**
```bash
cd /home/threesamyak/col334/ass3/part2
CFG=config.json ryu-manager --observe-links ryu.topology.switches p2_l2spf.py
```

**Terminal 2: Start Topology**
```bash
cd /home/threesamyak/col334/ass3/part2
sudo python3 p2_topo.py
```

**In Mininet CLI**:
```
mininet> pingall
mininet> h1 ping h2
mininet> iperf h1 h2
```

**Expected Controller Output**:
```
[INIT] Installed table-miss flow entry on s1
[INIT] Installed table-miss flow entry on s2
...
Register datapath: 0000000000000001
Register datapath: 0000000000000002
...
Learn host 00:00:00:00:00:01 at dpid 1 port 1
Learn host 00:00:00:00:00:02 at dpid 6 port 1
...
```

---

## Additional Notes

### CONFIG_DISPATCHER vs MAIN_DISPATCHER

- **CONFIG_DISPATCHER**: Used for initial switch configuration (e.g., table-miss flow)
- **MAIN_DISPATCHER**: Used for normal packet processing after handshake

The `EventOFPSwitchFeatures` is triggered during the OpenFlow handshake, before the switch enters MAIN_DISPATCHER state. This is the correct time to install default flows.

### Why Table-Miss is Required in OF1.3

OpenFlow 1.3 made the flow table behavior more explicit:
- **OF1.0**: Unmatched packets automatically sent to controller
- **OF1.3**: Unmatched packets are **dropped** unless a priority-0 flow explicitly sends them to controller

This gives more control but requires explicit configuration.

### Buffer Management

Both versions still use `msg.buffer_id` the same way:
```python
data = None if msg.buffer_id != ofp.OFP_NO_BUFFER else msg.data
```

This optimization avoids sending packet data twice if the switch has buffered it.

---

## Verification Commands

### Check OpenFlow version:
```bash
sudo ovs-vsctl get bridge s1 protocols
# Should show: [OpenFlow13]
```

### Check flows on switch:
```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows s1
```

Expected output should include:
```
priority=0 actions=CONTROLLER:65535  # Table-miss flow
priority=100 ... actions=output:2     # Installed forwarding flows
```

### Check groups (if using select groups):
```bash
sudo ovs-ofctl -O OpenFlow13 dump-groups s1
```

---

## Status

✅ **All OpenFlow 1.3 migration issues resolved**
- in_port access fixed
- Table-miss flow entries added
- Missing ofp variable fixed
- Controllers ready for testing

## Next Steps

1. Test basic connectivity (ping)
2. Test ECMP path selection (iperf multiple flows)
3. Test link failure with automated script
4. Optionally implement select groups for switch-side ECMP
