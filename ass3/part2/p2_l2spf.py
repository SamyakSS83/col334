# Layer-2 Shortest Path Routing (L2SPF) Ryu Application - OpenFlow 1.3
import os
import json
import random
import heapq

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER, CONFIG_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.topology import event
from ryu.topology.api import get_switch, get_link
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, ipv4, tcp, udp

class L2SPF(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(L2SPF, self).__init__(*args, **kwargs)
        # Load topology configuration
        cfg_path = os.environ.get('CFG', os.path.join(os.path.dirname(__file__), 'config.json'))
        with open(cfg_path) as f:
            cfg = json.load(f)
        self.ecmp = cfg.get('ecmp', False)
        nodes = cfg.get('nodes', [])
        matrix = cfg.get('weight_matrix', [])
        # Build weighted graph: dpid -> {neighbor_dpid: weight}
        self.graph = {}
        for i, n in enumerate(nodes):
            dpid = int(n.lstrip('s'))
            self.graph[dpid] = {}
            for j, w in enumerate(matrix[i]):
                if w > 0:
                    nbr = int(nodes[j].lstrip('s'))
                    self.graph[dpid][nbr] = w
        # Datapaths and topology
        self.datapaths = {}            # dpid -> datapath
        self.adjacency = {}           # dpid -> {neighbor_dpid: port_no}
        self.hosts = {}               # mac -> (dpid, port)
        self.installed = {}           # cache for installed paths (optional, for optimization)

    def _flow_hash(self, seed):
        """
        Use Python's built-in hash - it's simple and effective.
        """
        return hash(seed)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.logger.info("Register datapath: %016x", dp.id)
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER:
            if dp.id in self.datapaths:
                self.logger.info("Unregister datapath: %016x", dp.id)
                del self.datapaths[dp.id]

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

    @set_ev_cls(event.EventSwitchEnter)
    def _get_topology(self, ev):
        # Initialize adjacency from switch list and link list
        switch_list = get_switch(self, None)
        for sw in switch_list:
            self.adjacency.setdefault(sw.dp.id, {})
        links = get_link(self, None)
        for link in links:
            src, dst = link.src, link.dst
            # Use setdefault to tolerate partial discovery order
            self.adjacency.setdefault(src.dpid, {})[dst.dpid] = src.port_no
            self.adjacency.setdefault(dst.dpid, {})[src.dpid] = dst.port_no

    @set_ev_cls(event.EventLinkAdd)
    def _link_add_handler(self, ev):
        """Handle link addition/recovery: update adjacency, rebuild graph, reinstall flows"""
        link = ev.link
        src, dst = link.src, link.dst
        src_dpid, dst_dpid = src.dpid, dst.dpid
        src_port, dst_port = src.port_no, dst.port_no
        
        # Check if this is truly a new link
        is_new = src_dpid not in self.adjacency or dst_dpid not in self.adjacency.get(src_dpid, {})
        
        # Update adjacency
        self.adjacency.setdefault(src_dpid, {})[dst_dpid] = src_port
        self.adjacency.setdefault(dst_dpid, {})[src_dpid] = dst_port
        
        if is_new:
            self.logger.info("=" * 80)
            self.logger.info("✅ LINK ADDED: s%s:p%s <-> s%s:p%s", src_dpid, src_port, dst_dpid, dst_port)
            self.logger.info("=" * 80)
            
            # Rebuild graph from current adjacency
            old_graph = dict(self.graph)
            self._rebuild_graph_from_adjacency()
            
            # If graph changed, clear and reinstall all flows
            if old_graph != self.graph:
                self.logger.info("Topology changed - clearing flows and recomputing paths...")
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
                self.logger.info("Graph unchanged - no flow updates needed")

    @set_ev_cls(event.EventLinkDelete)
    def _link_delete_handler(self, ev):
        """Handle link removal: update adjacency, rebuild graph, clear and reinstall all flows"""
        link = ev.link
        src_dpid, dst_dpid = link.src.dpid, link.dst.dpid
        src_port, dst_port = link.src.port_no, link.dst.port_no
        
        self.logger.warning("=" * 80)
        self.logger.warning("❌ LINK FAILURE: s%s:p%s <-> s%s:p%s", src_dpid, src_port, dst_dpid, dst_port)
        self.logger.warning("=" * 80)
        
        # Update adjacency (remove link in both directions)
        try:
            self.adjacency.get(src_dpid, {}).pop(dst_dpid, None)
        except Exception:
            pass
        try:
            self.adjacency.get(dst_dpid, {}).pop(src_dpid, None)
        except Exception:
            pass
        
        # Rebuild graph from current adjacency
        old_graph = dict(self.graph)
        self._rebuild_graph_from_adjacency()
        
        # Only clear and reinstall if graph actually changed
        if old_graph != self.graph:
            self.logger.warning("⚠️  Topology changed - clearing and reinstalling all flows")
            
            # Clear flow cache
            self.installed = {}
            
            # Clear all flows from all switches and reinstall critical flows
            for dpid, dp in self.datapaths.items():
                self._clear_all_flows(dp)
            
            # Reinstall flows for all known host pairs
            self._reinstall_all_flows()
        else:
            self.logger.info("Graph unchanged - no flow updates needed")

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        # In OpenFlow 1.3, in_port is in msg.match
        in_port = msg.match['in_port']
        # ignore LLDP
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        src, dst = eth.src, eth.dst
        dpid = dp.id
        # ignore inter-switch floods for learning
        # inter_ports = set(self.adjacency.get(dpid, {}).values())
        # if in_port in inter_ports:
        #     return
        # # learn source at ingress only
        # if src not in self.hosts:
        #     self.hosts[src] = (dpid, in_port)
        # src_dpid, src_port = self.hosts[src]
        # if dpid != src_dpid:
        #     return

        # ports that connect to other switches (do not treat them as access ports)
        inter_ports = set(self.adjacency.get(dpid, {}).values())

        # Learn source only if packet arrived on an access port (not an inter-switch port)
        if in_port not in inter_ports:
            if src not in self.hosts:
                self.logger.info("Learn host %s at dpid %s port %s", src, dpid, in_port)
                self.hosts[src] = (dpid, in_port)

        # Find known src location (may be None if we haven't learned it yet)
        src_info = self.hosts.get(src)
        if src_info is None:
            # Unknown source — we can't compute an accurate path yet.
            # Flood as a fallback so we don't drop traffic silently.
            self.logger.debug("Unknown source %s seen on dpid %s port %s — flooding", src, dpid, in_port)
            actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
            data = None if msg.buffer_id != ofp.OFP_NO_BUFFER else msg.data
            out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                    in_port=in_port, actions=actions, data=data)
            dp.send_msg(out)
            return

        src_dpid, src_port = src_info

        # If packet not arriving at the host's switch, we don't want to re-learn — but we should
        # still process to possibly forward (do not early-return on inter-switch ports).
        if dpid != src_dpid:
            # If this packet arrived at a transit switch, continue processing
            # (we *used to return* here; that causes drops). Instead, just log and continue.
            self.logger.debug("Packet from %s arrived at transit switch %s (host at %s), continuing processing",
                            src, dpid, src_dpid)
            # don't return here

        # flood if dst unknown
        if dst not in self.hosts:
            actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
            data = None if msg.buffer_id != ofp.OFP_NO_BUFFER else msg.data
            out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                       in_port=in_port, actions=actions, data=data)
            dp.send_msg(out)
            return
        # compute path
        dst_dpid, dst_port = self.hosts[dst]
        # Use 5-tuple (src_mac, dst_mac, src_ip, dst_ip, src_port, dst_port, proto) 
        # for deterministic per-flow path selection in ECMP
        flow_seed = (src, dst)  # default: just MAC addresses
        
        # Try to extract IP and transport layer info for better flow differentiation
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt:
            src_ip = ip_pkt.src
            dst_ip = ip_pkt.dst
            proto = ip_pkt.proto
            
            tcp_pkt = pkt.get_protocol(tcp.tcp)
            udp_pkt = pkt.get_protocol(udp.udp)
            
            if tcp_pkt:
                flow_seed = (src, dst, src_ip, dst_ip, tcp_pkt.src_port, tcp_pkt.dst_port, proto)
            elif udp_pkt:
                flow_seed = (src, dst, src_ip, dst_ip, udp_pkt.src_port, udp_pkt.dst_port, proto)
            else:
                flow_seed = (src, dst, src_ip, dst_ip, proto)
        
        path = self._get_path(src_dpid, dst_dpid, seed=flow_seed)
        if not path:
            # no path: flood
            actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
            data = None if msg.buffer_id != ofp.OFP_NO_BUFFER else msg.data
            out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                       in_port=in_port, actions=actions, data=data)
            dp.send_msg(out)
            return
        # install flows for entire path
        self.logger.info("Chosen path for %s->%s (seed=%s): %s (ecmp=%s)", 
                        src, dst, flow_seed, path, self.ecmp)

        self._install_path(path, src, dst, pkt)
        # forward only on ingress
        next_hop = path[1] if len(path) > 1 else None
        out_port = self.adjacency[dpid].get(next_hop, dst_port)
        actions = [parser.OFPActionOutput(out_port)]
        data = None if msg.buffer_id != ofp.OFP_NO_BUFFER else msg.data
        out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                   in_port=in_port, actions=actions, data=data)
        dp.send_msg(out)
        # end of packet_in_handler

    def _get_path(self, src, dst, seed=None):
        # Dijkstra's algorithm with equal-cost multipath support
        dist = {n: float('inf') for n in self.graph}
        prev = {n: [] for n in self.graph}
        dist[src] = 0
        heap = [(0, src)]
        while heap:
            d, u = heapq.heappop(heap)
            if d > dist[u]:
                continue
            for v, w in self.graph[u].items():
                nd = d + w
                if nd < dist[v]:
                    dist[v] = nd
                    prev[v] = [u]
                    heapq.heappush(heap, (nd, v))
                elif nd == dist[v]:
                    prev[v].append(u)
        # Backtrack all shortest paths
        paths = []
        def backtrack(cur, node):
            if node == src:
                paths.append([src] + cur)
                return
            for p in prev[node]:
                backtrack([node] + cur, p)
        backtrack([], dst)
        if not paths:
            return None
        # return random.choice(paths) if self.ecmp else paths[0]
        if self.ecmp:
        # deterministic choice per flow: use hash of src/dst macs if provided,
        # otherwise fall back to random choice
            if seed is not None:
                h = self._flow_hash(seed)
                idx = h % len(paths)
                # Debug logging
                self.logger.debug("ECMP: hash(%s) = %d, len(paths) = %d, idx = %d, paths = %s",
                                 seed, h, len(paths), idx, paths)
                return paths[idx]
            return random.choice(paths)
        else:
            return paths[0]

    # def _install_path(self, path, src_mac, dst_mac):
    #     # Install flow entries at each switch along path
    #     for idx, dpid in enumerate(path):
    #         dp = self.datapaths.get(dpid)
    #         if not dp:
    #             continue
    #         parser = dp.ofproto_parser
    #         ofp = dp.ofproto
    #         # Determine in_port
    #         if idx == 0:
    #             in_port = self.hosts[src_mac][1]
    #         else:
    #             prev = path[idx - 1]
    #             in_port = self.adjacency[dpid][prev]
    #         # Determine out_port
    #         if idx == len(path) - 1:
    #             out_port = self.hosts[dst_mac][1]
    #         else:
    #             nxt = path[idx + 1]
    #             out_port = self.adjacency[dpid][nxt]
    #         # Construct and send flow mod
    #         match = parser.OFPMatch(in_port=in_port, dl_src=src_mac, dl_dst=dst_mac)
    #         actions = [parser.OFPActionOutput(out_port)]
    #         # fm = parser.OFPFlowMod(
    #         #     datapath=dp, match=match, cookie=0,
    #         #     command=ofp.OFPFC_ADD, idle_timeout=0, hard_timeout=0,
    #         #     priority=ofp.OFP_DEFAULT_PRIORITY,
    #         #     flags=ofp.OFPFF_SEND_FLOW_REM,
    #         #     actions=actions)
    #         # use a fixed priority number and avoid passing flags that might not exist
    #         fm = parser.OFPFlowMod(
    #             datapath=dp,
    #             match=match,
    #             idle_timeout=0,
    #             hard_timeout=0,
    #             priority=100,
    #             actions=actions)

    #         dp.send_msg(fm)

    def _install_path(self, path, src_mac, dst_mac, pkt=None):
        # Extract IP and transport layer info for flow-specific matching when ECMP is enabled
        ip_pkt = pkt.get_protocol(ipv4.ipv4) if pkt else None
        tcp_pkt = pkt.get_protocol(tcp.tcp) if pkt else None
        udp_pkt = pkt.get_protocol(udp.udp) if pkt else None
        
        # install forward direction
        for idx, dpid in enumerate(path):
            dp = self.datapaths.get(dpid)
            if not dp:
                continue
            parser = dp.ofproto_parser
            ofp = dp.ofproto
            if idx == 0:
                in_port = self.hosts[src_mac][1]
            else:
                prev = path[idx - 1]
                in_port = self.adjacency[dpid][prev]
            if idx == len(path) - 1:
                out_port = self.hosts[dst_mac][1]
            else:
                nxt = path[idx + 1]
                out_port = self.adjacency[dpid][nxt]
            
            # Create match based on whether we have L3/L4 info and ECMP is enabled
            if self.ecmp and ip_pkt:
                # For ECMP, match on 5-tuple to differentiate flows
                if tcp_pkt:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        eth_type=0x0800,  # IPv4
                        ipv4_src=ip_pkt.src,
                        ipv4_dst=ip_pkt.dst,
                        ip_proto=ip_pkt.proto,
                        tcp_src=tcp_pkt.src_port,
                        tcp_dst=tcp_pkt.dst_port
                    )
                elif udp_pkt:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        eth_type=0x0800,  # IPv4
                        ipv4_src=ip_pkt.src,
                        ipv4_dst=ip_pkt.dst,
                        ip_proto=ip_pkt.proto,
                        udp_src=udp_pkt.src_port,
                        udp_dst=udp_pkt.dst_port
                    )
                else:
                    # IP but no TCP/UDP
                    match = parser.OFPMatch(
                        in_port=in_port,
                        eth_type=0x0800,
                        ipv4_src=ip_pkt.src,
                        ipv4_dst=ip_pkt.dst,
                        ip_proto=ip_pkt.proto
                    )
            else:
                # For non-ECMP or non-IP traffic, use MAC-based matching
                match = parser.OFPMatch(in_port=in_port, eth_src=src_mac, eth_dst=dst_mac)
            
            actions = [parser.OFPActionOutput(out_port)]
            inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
            fm = parser.OFPFlowMod(datapath=dp, match=match, idle_timeout=0,
                                hard_timeout=0, priority=100, instructions=inst)
            dp.send_msg(fm)

        # -------------- install reverse direction -----------------
        rev_path = list(reversed(path))
        for idx, dpid in enumerate(rev_path):
            dp = self.datapaths.get(dpid)
            if not dp:
                continue
            parser = dp.ofproto_parser
            ofp = dp.ofproto
            if idx == 0:
                in_port = self.hosts[dst_mac][1]
            else:
                prev = rev_path[idx - 1]
                in_port = self.adjacency[dpid][prev]
            if idx == len(rev_path) - 1:
                out_port = self.hosts[src_mac][1]
            else:
                nxt = rev_path[idx + 1]
                out_port = self.adjacency[dpid][nxt]
            
            # Create reverse match (swap src/dst)
            if self.ecmp and ip_pkt:
                if tcp_pkt:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        eth_type=0x0800,  # IPv4
                        ipv4_src=ip_pkt.dst,  # swapped
                        ipv4_dst=ip_pkt.src,  # swapped
                        ip_proto=ip_pkt.proto,
                        tcp_src=tcp_pkt.dst_port,  # swapped
                        tcp_dst=tcp_pkt.src_port   # swapped
                    )
                elif udp_pkt:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        eth_type=0x0800,  # IPv4
                        ipv4_src=ip_pkt.dst,  # swapped
                        ipv4_dst=ip_pkt.src,  # swapped
                        ip_proto=ip_pkt.proto,
                        udp_src=udp_pkt.dst_port,  # swapped
                        udp_dst=udp_pkt.src_port   # swapped
                    )
                else:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        eth_type=0x0800,
                        ipv4_src=ip_pkt.dst,  # swapped
                        ipv4_dst=ip_pkt.src,  # swapped
                        ip_proto=ip_pkt.proto
                    )
            else:
                match = parser.OFPMatch(in_port=in_port, eth_src=dst_mac, eth_dst=src_mac)
            
            actions = [parser.OFPActionOutput(out_port)]
            inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
            fm = parser.OFPFlowMod(datapath=dp, match=match, idle_timeout=0,
                                hard_timeout=0, priority=100, instructions=inst)
            dp.send_msg(fm)

    def _rebuild_graph_from_adjacency(self):
        """Rebuild graph based on current adjacency information"""
        self.logger.info("=== REBUILDING GRAPH FROM CURRENT ADJACENCY ===")
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
        self.logger.info("Rebuilt graph with active links: %s", 
                        {f"s{k}": {f"s{nk}": nv for nk, nv in v.items()} for k, v in self.graph.items()})

    def _clear_all_flows(self, datapath):
        """Clear all flows from a switch, then reinstall table-miss"""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        # Delete ALL flows (including routing flows)
        match = parser.OFPMatch()
        mod = parser.OFPFlowMod(
            datapath=datapath,
            command=ofproto.OFPFC_DELETE,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            match=match
        )
        datapath.send_msg(mod)
        
        self.logger.debug("Cleared all flows from switch s%s", datapath.id)
        
        # CRITICAL: Reinstall table-miss flow immediately so controller can receive packets
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=0,
                                match=match, instructions=inst)
        datapath.send_msg(mod)

    def _reinstall_all_flows(self):
        """Proactively reinstall flows for all known host pairs"""
        self.logger.info("=" * 80)
        self.logger.info("🔄 REINSTALLING FLOWS FOR ALL HOST PAIRS")
        self.logger.info("=" * 80)
        
        # Get all known hosts
        host_macs = list(self.hosts.keys())
        
        if len(host_macs) < 2:
            self.logger.info("Not enough hosts learned yet, waiting for traffic")
            return
        
        # Install flows for all host pairs (both directions)
        flow_count = 0
        for src_mac in host_macs:
            for dst_mac in host_macs:
                if src_mac != dst_mac:
                    src_dpid, src_port = self.hosts[src_mac]
                    dst_dpid, dst_port = self.hosts[dst_mac]
                    
                    # Compute path
                    path = self._get_path(src_dpid, dst_dpid, seed=(src_mac, dst_mac))
                    
                    if path:
                        self.logger.info("Reinstalling: %s -> %s via %s", src_mac, dst_mac, path)
                        self._install_path(path, src_mac, dst_mac, pkt=None)
                        flow_count += 1
                    else:
                        self.logger.warning("No path found for %s -> %s", src_mac, dst_mac)
        
        self.logger.info("=" * 80)
        self.logger.info("✅ Reinstalled %d flow paths", flow_count)
        self.logger.info("=" * 80)
