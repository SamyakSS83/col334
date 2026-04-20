# Bonus: Layer-2 Shortest Path with Weighted ECMP (link utilization-aware) - OpenFlow 1.3
import os
import time
import json
import random
import heapq
from collections import defaultdict
from threading import Thread

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER, CONFIG_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.topology import event
from ryu.topology.api import get_switch, get_link
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, ipv4, tcp, udp

class L2SPFWeightedECMP(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(L2SPFWeightedECMP, self).__init__(*args, **kwargs)
        cfg_path = os.environ.get('CFG', os.path.join(os.path.dirname(__file__), 'config.json'))
        with open(cfg_path) as f:
            cfg = json.load(f)
        # force ECMP behavior but use weighted selection
        self.nodes = cfg.get('nodes', [])
        self.matrix = cfg.get('weight_matrix', [])
        # build weighted graph using integer dpids
        self.graph = {}
        for i, n in enumerate(self.nodes):
            u = int(n.lstrip('s'))
            self.graph.setdefault(u, {})
            for j, w in enumerate(self.matrix[i]):
                if w > 0:
                    v = int(self.nodes[j].lstrip('s'))
                    self.graph[u][v] = w
        print("[CONFIG] Loaded weighted-ECMP controller")
        print(f"[CONFIG] Nodes: {self.nodes}")
        # Summarize weighted edges (avoid duplicates by u<v if symmetric)
        edges = []
        for u, nbrs in self.graph.items():
            for v, w in nbrs.items():
                edges.append((u, v, w))
        print(f"[GRAPH] Directed edges (u->v, w): {edges}")
        # datapaths, adjacency, hosts
        self.datapaths = {}
        self.adjacency = defaultdict(dict)  # dpid -> neighbor -> port
        self.hosts = {}  # mac -> (dpid, port)
        # link byte counters per (dpid->neighbor)
        self.link_bytes = defaultdict(lambda: defaultdict(int))
        # cache installed paths per (src, dst) to avoid reinstall spam
        self.installed = {}
        # Group ID management for select groups
        self.next_group_id = 1
        self.groups = {}  # (dpid, dst_mac) -> group_id
        self.use_groups = os.environ.get('USE_GROUPS', 'true').lower() == 'true'
        print(f"[CONFIG] USE_GROUPS={self.use_groups} (set USE_GROUPS=false to disable)")
        # stats polling control
        self.stats_interval = float(os.environ.get('STATS_INTERVAL', '2.0'))
        self._last_stats = 0.0
        # Start background stats polling thread
        self._stats_thread = Thread(target=self._stats_poll_loop, daemon=True)
        self._stats_thread.start()
        print(f"[STATS] Started background polling thread (interval={self.stats_interval}s)")

    def _stats_poll_loop(self):
        """Background thread to continuously poll port stats"""
        while True:
            time.sleep(self.stats_interval)
            self._request_stats()

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
            print(f"[DP] Registered datapath: s{dp.id}")
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)
            print(f"[DP] Unregistered datapath: s{dp.id}")

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
        print(f"[INIT] Installed table-miss flow entry on s{datapath.id}")

    @set_ev_cls(event.EventSwitchEnter)
    def _topo_discover(self, ev):
        for sw in get_switch(self, None):
            self.adjacency.setdefault(sw.dp.id, {})
        for l in get_link(self, None):
            self.adjacency[l.src.dpid][l.dst.dpid] = l.src.port_no
            self.adjacency[l.dst.dpid][l.src.dpid] = l.dst.port_no
        print("[TOPO] Switches:", [f"s{sw.dp.id}" for sw in get_switch(self, None)])
        print("[TOPO] Links:", [(f"s{lk.src.dpid}", f"s{lk.dst.dpid}") for lk in get_link(self, None)])
        print("[TOPO] Adjacency (dpid -> {nbr:port}):",
              {dpid: dict(ports) for dpid, ports in self.adjacency.items()})

    @set_ev_cls(event.EventLinkAdd)
    def _link_add(self, ev):
        """Handle link addition/recovery: update adjacency, rebuild graph, reinstall flows"""
        link = ev.link
        src_dpid, dst_dpid = link.src.dpid, link.dst.dpid
        src_port, dst_port = link.src.port_no, link.dst.port_no
        
        # Check if this is truly a new link
        is_new = src_dpid not in self.adjacency or dst_dpid not in self.adjacency.get(src_dpid, {})
        
        # Update adjacency
        self.adjacency[src_dpid][dst_dpid] = src_port
        self.adjacency[dst_dpid][src_dpid] = dst_port
        
        if is_new:
            print("=" * 80)
            print(f"✅ LINK ADDED: s{src_dpid}:p{src_port} <-> s{dst_dpid}:p{dst_port}")
            print("=" * 80)
            
            # Rebuild graph from current adjacency
            old_graph = dict(self.graph)
            self._rebuild_graph_from_adjacency()
            
            # If graph changed, clear and reinstall all flows
            if old_graph != self.graph:
                print("Topology changed - clearing flows and recomputing paths...")
                self.installed = {}
                
                # Clear flows from all switches
                for dpid, dp in self.datapaths.items():
                    self._clear_all_flows(dp)
                
                # Add small delay to ensure flow deletion completes
                import time
                time.sleep(0.1)
                
                # Now reinstall flows
                self._reinstall_all_flows()
            else:
                print("Graph unchanged - no flow updates needed")

    @set_ev_cls(event.EventLinkDelete)
    def _link_delete(self, ev):
        """Handle link removal: update adjacency, rebuild graph, clear and reinstall all flows"""
        l = ev.link
        s1, s2 = l.src.dpid, l.dst.dpid
        src_port, dst_port = l.src.port_no, l.dst.port_no
        
        print("=" * 80)
        print(f"❌ LINK FAILURE: s{s1}:p{src_port} <-> s{s2}:p{dst_port}")
        print("=" * 80)
        
        # Update adjacency (remove link in both directions)
        self.adjacency.get(s1, {}).pop(s2, None)
        self.adjacency.get(s2, {}).pop(s1, None)
        
        # Rebuild graph from current adjacency
        old_graph = dict(self.graph)
        self._rebuild_graph_from_adjacency()
        
        # Only clear and reinstall if graph actually changed
        if old_graph != self.graph:
            print("⚠️  Topology changed - clearing and reinstalling all flows")
            
            # Clear flow cache and groups
            self.installed.clear()
            self.groups.clear()
            
            # Clear all flows from all switches and reinstall critical flows
            for dpid, dp in self.datapaths.items():
                self._clear_all_flows(dp)
            
            # Reinstall flows for all known host pairs
            self._reinstall_all_flows()
        else:
            print("Graph unchanged - no flow updates needed")

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply(self, ev):
        # collect bytes transmitted per port and update link load
        dp = ev.msg.datapath
        dpid = dp.id
        for stat in ev.msg.body:
            port_no = stat.port_no
            # map port to neighbor if it's an inter-switch link
            for nbr, pno in self.adjacency[dpid].items():
                if pno == port_no:
                    # use tx_bytes as a simple link utilization proxy
                    self.link_bytes[dpid][nbr] = int(getattr(stat, 'tx_bytes', 0))
                    # print(f"[STATS] s{dpid} port {port_no} (to s{nbr}) tx_bytes={self.link_bytes[dpid][nbr]}")

    def _request_stats(self):
        # poll all datapaths for port stats
        # print("[STATS] Polling port stats from all switches")
        for dp in self.datapaths.values():
            ofp = dp.ofproto
            parser = dp.ofproto_parser
            # In OpenFlow 1.3, use OFPP_ANY instead of OFPP_NONE
            req = parser.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY)
            dp.send_msg(req)

    def _dijkstra_all_shortest(self, src, dst):
        # compute all shortest paths with Dijkstra predecessor lists
        dist = {n: float('inf') for n in self.graph}
        prev = {n: [] for n in self.graph}
        dist[src] = 0
        pq = [(0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for v, w in self.graph[u].items():
                nd = d + w
                if nd < dist[v]:
                    dist[v] = nd
                    prev[v] = [u]
                    heapq.heappush(pq, (nd, v))
                elif nd == dist[v]:
                    prev[v].append(u)
        paths = []
        def backtrack(cur, node):
            if node == src:
                paths.append([src] + cur)
                return
            for p in prev[node]:
                backtrack([node] + cur, p)
        backtrack([], dst)
        return paths

    def _choose_path_by_load(self, paths):
        # Path cost = sum of link loads (tx_bytes) along the path; choose minimum
        def path_load(path):
            total = 0
            for i in range(len(path)-1):
                u, v = path[i], path[i+1]
                total += self.link_bytes[u][v]
            return total
        if not paths:
            return None
        loads = [(path_load(p), p) for p in paths]
        print("[ECMP] Candidate shortest paths and their loads:")
        for l, p in loads:
            print(f"  load={l} path={p}")
        loads.sort(key=lambda x: x[0])
        # break ties randomly among least-loaded
        min_load = loads[0][0]
        candidates = [p for l, p in loads if l == min_load]
        chosen = random.choice(candidates)
        print(f"[ECMP] Chosen path (min load={min_load}): {chosen}")
        
        # Update predicted load for the chosen path (assume ~1500 bytes per packet)
        # This helps avoid selecting the same path repeatedly before stats update
        estimated_flow_bytes = 15000  # conservative estimate for initial packets
        for i in range(len(chosen)-1):
            u, v = chosen[i], chosen[i+1]
            self.link_bytes[u][v] += estimated_flow_bytes
        
        return chosen

    def _create_or_update_group(self, dpid, dst_mac, next_hops):
        """
        Create or update a select group for ECMP at a branching switch.
        next_hops: list of (next_dpid, out_port) tuples
        Returns: group_id
        """
        dp = self.datapaths.get(dpid)
        if not dp:
            return None
        
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        
        key = (dpid, dst_mac)
        if key in self.groups:
            group_id = self.groups[key]
            # Update existing group (delete + re-add)
            req = parser.OFPGroupMod(dp, ofp.OFPGC_DELETE, ofp.OFPGT_SELECT, group_id)
            dp.send_msg(req)
            print(f"[GROUP] Deleted group {group_id} on s{dpid} for re-creation")
        else:
            group_id = self.next_group_id
            self.next_group_id += 1
            self.groups[key] = group_id
        
        # Create buckets - one per next hop
        buckets = []
        for next_dpid, out_port in next_hops:
            actions = [parser.OFPActionOutput(out_port)]
            buckets.append(parser.OFPBucket(actions=actions))
        
        req = parser.OFPGroupMod(dp, ofp.OFPGC_ADD, ofp.OFPGT_SELECT, group_id, buckets=buckets)
        dp.send_msg(req)
        print(f"[GROUP] Created select group {group_id} on s{dpid} with {len(buckets)} buckets: {next_hops}")
        return group_id

    def _install_path(self, path, src_mac, dst_mac, pkt=None):
        # Extract IP and transport info for flow-specific matching
        ip_pkt = pkt.get_protocol(ipv4.ipv4) if pkt else None
        tcp_pkt = pkt.get_protocol(tcp.tcp) if pkt else None
        udp_pkt = pkt.get_protocol(udp.udp) if pkt else None
        
        # forward direction
        for i, dpid in enumerate(path):
            dp = self.datapaths.get(dpid)
            if not dp:
                continue
            ofp = dp.ofproto
            parser = dp.ofproto_parser
            in_port = self.hosts[src_mac][1] if i == 0 else self.adjacency[dpid][path[i-1]]
            out_port = self.hosts[dst_mac][1] if i == len(path)-1 else self.adjacency[dpid][path[i+1]]
            
            # Use 5-tuple matching for per-flow load balancing
            if ip_pkt:
                if tcp_pkt:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        eth_type=0x0800,
                        ipv4_src=ip_pkt.src,
                        ipv4_dst=ip_pkt.dst,
                        ip_proto=ip_pkt.proto,
                        tcp_src=tcp_pkt.src_port,
                        tcp_dst=tcp_pkt.dst_port
                    )
                elif udp_pkt:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        eth_type=0x0800,
                        ipv4_src=ip_pkt.src,
                        ipv4_dst=ip_pkt.dst,
                        ip_proto=ip_pkt.proto,
                        udp_src=udp_pkt.src_port,
                        udp_dst=udp_pkt.dst_port
                    )
                else:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        eth_type=0x0800,
                        ipv4_src=ip_pkt.src,
                        ipv4_dst=ip_pkt.dst,
                        ip_proto=ip_pkt.proto
                    )
            else:
                # Fallback to MAC-based matching for non-IP traffic
                match = parser.OFPMatch(in_port=in_port, eth_src=src_mac, eth_dst=dst_mac)
            
            actions = [parser.OFPActionOutput(out_port)]
            inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
            fm = parser.OFPFlowMod(datapath=dp, match=match, idle_timeout=0, hard_timeout=0,
                                   priority=100, instructions=inst)
            dp.send_msg(fm)
            if ip_pkt and (tcp_pkt or udp_pkt):
                proto_name = "TCP" if tcp_pkt else "UDP"
                sport = tcp_pkt.src_port if tcp_pkt else udp_pkt.src_port
                dport = tcp_pkt.dst_port if tcp_pkt else udp_pkt.dst_port
                print(f"[FLOW] fwd s{dpid}: in_port={in_port} {proto_name} {ip_pkt.src}:{sport}->{ip_pkt.dst}:{dport} out_port={out_port}")
            else:
                print(f"[FLOW] fwd s{dpid}: in_port={in_port} {src_mac}->{dst_mac} out_port={out_port}")
        
        # reverse direction
        rev = list(reversed(path))
        for i, dpid in enumerate(rev):
            dp = self.datapaths.get(dpid)
            if not dp:
                continue
            ofp = dp.ofproto
            parser = dp.ofproto_parser
            in_port = self.hosts[dst_mac][1] if i == 0 else self.adjacency[dpid][rev[i-1]]
            out_port = self.hosts[src_mac][1] if i == len(rev)-1 else self.adjacency[dpid][rev[i+1]]
            
            # Reverse match (swap src/dst)
            if ip_pkt:
                if tcp_pkt:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        eth_type=0x0800,
                        ipv4_src=ip_pkt.dst,  # swapped
                        ipv4_dst=ip_pkt.src,  # swapped
                        ip_proto=ip_pkt.proto,
                        tcp_src=tcp_pkt.dst_port,  # swapped
                        tcp_dst=tcp_pkt.src_port   # swapped
                    )
                elif udp_pkt:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        eth_type=0x0800,
                        ipv4_src=ip_pkt.dst,
                        ipv4_dst=ip_pkt.src,
                        ip_proto=ip_pkt.proto,
                        udp_src=udp_pkt.dst_port,
                        udp_dst=udp_pkt.src_port
                    )
                else:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        eth_type=0x0800,
                        ipv4_src=ip_pkt.dst,
                        ipv4_dst=ip_pkt.src,
                        ip_proto=ip_pkt.proto
                    )
            else:
                match = parser.OFPMatch(in_port=in_port, eth_src=dst_mac, eth_dst=src_mac)
            
            actions = [parser.OFPActionOutput(out_port)]
            inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
            fm = parser.OFPFlowMod(datapath=dp, match=match, idle_timeout=0, hard_timeout=0,
                                   priority=100, instructions=inst)
            dp.send_msg(fm)
            if ip_pkt and (tcp_pkt or udp_pkt):
                proto_name = "TCP" if tcp_pkt else "UDP"
                sport = tcp_pkt.dst_port if tcp_pkt else udp_pkt.dst_port
                dport = tcp_pkt.src_port if tcp_pkt else udp_pkt.src_port
                print(f"[FLOW] rev s{dpid}: in_port={in_port} {proto_name} {ip_pkt.dst}:{sport}->{ip_pkt.src}:{dport} out_port={out_port}")
            else:
                print(f"[FLOW] rev s{dpid}: in_port={in_port} {dst_mac}->{src_mac} out_port={out_port}")

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        # periodically request stats so link_bytes stay updated
        now = time.monotonic()
        if now - self._last_stats >= self.stats_interval:
            self._request_stats()
            self._last_stats = now
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        # In OpenFlow 1.3, in_port is in msg.match
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        src, dst = eth.src, eth.dst
        dpid = dp.id
        # Quiet noisy IPv6 multicast (e.g., ff02::fb -> 33:33:00:00:00:fb): drop locally
        if dst.lower().startswith('33:33'):
            match = parser.OFPMatch(eth_dst=dst)
            inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, [])]
            fm = parser.OFPFlowMod(datapath=dp, match=match, idle_timeout=10,
                                   hard_timeout=0, priority=5, instructions=inst)
            dp.send_msg(fm)
            # don't log every multicast frame to avoid spam
            return
        print(f"\n[PKTIN] s{dpid} in_port={in_port} {src} -> {dst}")
        # learn hosts on access ports only
        inter_ports = set(self.adjacency.get(dpid, {}).values())
        if in_port not in inter_ports and src not in self.hosts:
            self.hosts[src] = (dpid, in_port)
            print(f"[LEARN] host {src} at s{dpid} port {in_port}")
        
        if dst not in self.hosts:
            print(f"[FLOOD] dst {dst} unknown; flooding")
            actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
            data = None if msg.buffer_id != ofp.OFP_NO_BUFFER else msg.data
            out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                       in_port=in_port, actions=actions, data=data)
            dp.send_msg(out)
            return
        
        # Both hosts known, compute path
        src_dpid, _ = self.hosts[src]
        dst_dpid, dst_port = self.hosts[dst]
        # compute all shortest paths then choose least-loaded
        paths = self._dijkstra_all_shortest(src_dpid, dst_dpid)
        print(f"[ECMP] Found {len(paths)} equal-cost shortest path(s) from s{src_dpid} to s{dst_dpid}")
        if not paths:
            print("[FLOOD] no path available; flooding")
            actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
            data = None if msg.buffer_id != ofp.OFP_NO_BUFFER else msg.data
            out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                       in_port=in_port, actions=actions, data=data)
            dp.send_msg(out)
            return
        path = self._choose_path_by_load(paths)
        print(f"[PATH] Selected path: {path}")
        
        # Create flow key based on 5-tuple if possible
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt:
            tcp_pkt = pkt.get_protocol(tcp.tcp)
            udp_pkt = pkt.get_protocol(udp.udp)
            if tcp_pkt:
                key = (src, dst, ip_pkt.src, ip_pkt.dst, tcp_pkt.src_port, tcp_pkt.dst_port, ip_pkt.proto)
            elif udp_pkt:
                key = (src, dst, ip_pkt.src, ip_pkt.dst, udp_pkt.src_port, udp_pkt.dst_port, ip_pkt.proto)
            else:
                key = (src, dst, ip_pkt.src, ip_pkt.dst, ip_pkt.proto)
        else:
            key = (src, dst)
        
        # install only if it's a new or changed path for this flow
        if self.installed.get(key) != tuple(path):
            self._install_path(path, src, dst, pkt)
            self.installed[key] = tuple(path)
        # forward only from ingress switch (where the source host sits)
        if dpid == src_dpid:
            next_hop = path[1] if len(path) > 1 else None
            out_port = self.adjacency[dpid].get(next_hop, dst_port)
            actions = [parser.OFPActionOutput(out_port)]
            data = None if msg.buffer_id != ofp.OFP_NO_BUFFER else msg.data
            out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                       in_port=in_port, actions=actions, data=data)
            dp.send_msg(out)
            print(f"[OUT] s{dpid} sending packet out of port {out_port}")
        else:
            # non-ingress PacketIn: flows are installed; drop this packet to avoid mis-forwarding
            pass

    def _rebuild_graph_from_adjacency(self):
        """Rebuild graph based on current adjacency information"""
        print("=== REBUILDING GRAPH FROM CURRENT ADJACENCY ===")
        new_graph = {}
        
        # Initialize all switches (including ones from adjacency that might not be in original graph)
        all_dpids = set(self.graph.keys()) | set(self.adjacency.keys())
        for dpid in all_dpids:
            new_graph[dpid] = {}
        
        # Build graph from current adjacency (use original costs if available, default=10)
        for dpid in new_graph.keys():
            if dpid in self.adjacency:
                for neighbor_dpid, port in self.adjacency[dpid].items():
                    # Use original cost if it existed, otherwise default to 10
                    if dpid in self.graph and neighbor_dpid in self.graph.get(dpid, {}):
                        cost = self.graph[dpid][neighbor_dpid]
                    else:
                        cost = 10  # Default cost for new links
                    new_graph[dpid][neighbor_dpid] = cost
        
        self.graph = new_graph
        print(f"Rebuilt graph with active links: {new_graph}")

    def _clear_all_flows(self, datapath):
        """Clear all flows from a switch, then reinstall table-miss"""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        # Delete ALL flows
        match = parser.OFPMatch()
        mod = parser.OFPFlowMod(
            datapath=datapath,
            command=ofproto.OFPFC_DELETE,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            match=match
        )
        datapath.send_msg(mod)
        
        print(f"[RECOVERY] Cleared all flows from switch s{datapath.id}")
        
        # CRITICAL: Reinstall table-miss flow immediately
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=0,
                                match=match, instructions=inst)
        datapath.send_msg(mod)

    def _reinstall_all_flows(self):
        """Proactively reinstall flows for all known host pairs"""
        print("=" * 80)
        print("🔄 REINSTALLING FLOWS FOR ALL HOST PAIRS")
        print("=" * 80)
        
        # Get all known hosts
        host_macs = list(self.hosts.keys())
        
        if len(host_macs) < 2:
            print("Not enough hosts learned yet, waiting for traffic")
            return
        
        # Install flows for all host pairs (both directions)
        flow_count = 0
        for src_mac in host_macs:
            for dst_mac in host_macs:
                if src_mac != dst_mac:
                    src_dpid, src_port = self.hosts[src_mac]
                    dst_dpid, dst_port = self.hosts[dst_mac]
                    
                    # Compute path
                    paths = self._dijkstra_all_shortest(src_dpid, dst_dpid)
                    
                    if paths:
                        path = self._choose_path_by_load(paths)
                        print(f"[REINSTALL] {src_mac} -> {dst_mac} via {path}")
                        self._install_path(path, src_mac, dst_mac, pkt=None)
                        flow_count += 1
                    else:
                        print(f"[REINSTALL] No path found for {src_mac} -> {dst_mac}")
        
        print("=" * 80)
        print(f"✅ Reinstalled {flow_count} flow paths")
        print("=" * 80)
