# Layer-2 Shortest Path Routing (L2SPF) Ryu Application
import os
import json
import random
import heapq

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.topology import event
from ryu.topology.api import get_switch, get_link
from ryu.ofproto import ofproto_v1_0
from ryu.lib.packet import packet, ethernet, ether_types, ipv4, tcp, udp

class L2SPF(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_0.OFP_VERSION]

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
        # Update adjacency for new links
        link = ev.link
        src, dst = link.src, link.dst
        self.adjacency.setdefault(src.dpid, {})[dst.dpid] = src.port_no
        self.adjacency.setdefault(dst.dpid, {})[src.dpid] = dst.port_no

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        in_port = msg.in_port
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
                        dl_type=0x0800,  # IPv4
                        nw_src=ip_pkt.src,
                        nw_dst=ip_pkt.dst,
                        nw_proto=ip_pkt.proto,
                        tp_src=tcp_pkt.src_port,
                        tp_dst=tcp_pkt.dst_port
                    )
                elif udp_pkt:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        dl_type=0x0800,  # IPv4
                        nw_src=ip_pkt.src,
                        nw_dst=ip_pkt.dst,
                        nw_proto=ip_pkt.proto,
                        tp_src=udp_pkt.src_port,
                        tp_dst=udp_pkt.dst_port
                    )
                else:
                    # IP but no TCP/UDP
                    match = parser.OFPMatch(
                        in_port=in_port,
                        dl_type=0x0800,
                        nw_src=ip_pkt.src,
                        nw_dst=ip_pkt.dst,
                        nw_proto=ip_pkt.proto
                    )
            else:
                # For non-ECMP or non-IP traffic, use MAC-based matching
                match = parser.OFPMatch(in_port=in_port, dl_src=src_mac, dl_dst=dst_mac)
            
            actions = [parser.OFPActionOutput(out_port)]
            fm = parser.OFPFlowMod(datapath=dp, match=match, idle_timeout=0,
                                hard_timeout=0, priority=100, actions=actions)
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
                        dl_type=0x0800,  # IPv4
                        nw_src=ip_pkt.dst,  # swapped
                        nw_dst=ip_pkt.src,  # swapped
                        nw_proto=ip_pkt.proto,
                        tp_src=tcp_pkt.dst_port,  # swapped
                        tp_dst=tcp_pkt.src_port   # swapped
                    )
                elif udp_pkt:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        dl_type=0x0800,  # IPv4
                        nw_src=ip_pkt.dst,  # swapped
                        nw_dst=ip_pkt.src,  # swapped
                        nw_proto=ip_pkt.proto,
                        tp_src=udp_pkt.dst_port,  # swapped
                        tp_dst=udp_pkt.src_port   # swapped
                    )
                else:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        dl_type=0x0800,
                        nw_src=ip_pkt.dst,  # swapped
                        nw_dst=ip_pkt.src,  # swapped
                        nw_proto=ip_pkt.proto
                    )
            else:
                match = parser.OFPMatch(in_port=in_port, dl_src=dst_mac, dl_dst=src_mac)
            
            actions = [parser.OFPActionOutput(out_port)]
            fm = parser.OFPFlowMod(datapath=dp, match=match, idle_timeout=0,
                                hard_timeout=0, priority=100, actions=actions)
            dp.send_msg(fm)
