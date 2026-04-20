# Bonus: Layer-2 Shortest Path with Weighted ECMP (link utilization-aware)
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
from ryu.ofproto import ofproto_v1_0
from ryu.lib.packet import packet, ethernet, ether_types, ipv4, tcp, udp

class L2SPFWeightedECMP(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_0.OFP_VERSION]

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
        l = ev.link
        self.adjacency[l.src.dpid][l.dst.dpid] = l.src.port_no
        self.adjacency[l.dst.dpid][l.src.dpid] = l.dst.port_no
        print(f"[TOPO] Link added: s{l.src.dpid}:p{l.src.port_no} <-> s{l.dst.dpid}:p{l.dst.port_no}")

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
            req = parser.OFPPortStatsRequest(dp, 0, ofp.OFPP_NONE)
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
        return chosen

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
            parser = dp.ofproto_parser
            in_port = self.hosts[src_mac][1] if i == 0 else self.adjacency[dpid][path[i-1]]
            out_port = self.hosts[dst_mac][1] if i == len(path)-1 else self.adjacency[dpid][path[i+1]]
            
            # Use 5-tuple matching for per-flow load balancing
            if ip_pkt:
                if tcp_pkt:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        dl_type=0x0800,
                        nw_src=ip_pkt.src,
                        nw_dst=ip_pkt.dst,
                        nw_proto=ip_pkt.proto,
                        tp_src=tcp_pkt.src_port,
                        tp_dst=tcp_pkt.dst_port
                    )
                elif udp_pkt:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        dl_type=0x0800,
                        nw_src=ip_pkt.src,
                        nw_dst=ip_pkt.dst,
                        nw_proto=ip_pkt.proto,
                        tp_src=udp_pkt.src_port,
                        tp_dst=udp_pkt.dst_port
                    )
                else:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        dl_type=0x0800,
                        nw_src=ip_pkt.src,
                        nw_dst=ip_pkt.dst,
                        nw_proto=ip_pkt.proto
                    )
            else:
                # Fallback to MAC-based matching for non-IP traffic
                match = parser.OFPMatch(in_port=in_port, dl_src=src_mac, dl_dst=dst_mac)
            
            actions = [parser.OFPActionOutput(out_port)]
            fm = parser.OFPFlowMod(datapath=dp, match=match, idle_timeout=0, hard_timeout=0,
                                   priority=100, actions=actions)
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
            parser = dp.ofproto_parser
            in_port = self.hosts[dst_mac][1] if i == 0 else self.adjacency[dpid][rev[i-1]]
            out_port = self.hosts[src_mac][1] if i == len(rev)-1 else self.adjacency[dpid][rev[i+1]]
            
            # Reverse match (swap src/dst)
            if ip_pkt:
                if tcp_pkt:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        dl_type=0x0800,
                        nw_src=ip_pkt.dst,  # swapped
                        nw_dst=ip_pkt.src,  # swapped
                        nw_proto=ip_pkt.proto,
                        tp_src=tcp_pkt.dst_port,  # swapped
                        tp_dst=tcp_pkt.src_port   # swapped
                    )
                elif udp_pkt:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        dl_type=0x0800,
                        nw_src=ip_pkt.dst,
                        nw_dst=ip_pkt.src,
                        nw_proto=ip_pkt.proto,
                        tp_src=udp_pkt.dst_port,
                        tp_dst=udp_pkt.src_port
                    )
                else:
                    match = parser.OFPMatch(
                        in_port=in_port,
                        dl_type=0x0800,
                        nw_src=ip_pkt.dst,
                        nw_dst=ip_pkt.src,
                        nw_proto=ip_pkt.proto
                    )
            else:
                match = parser.OFPMatch(in_port=in_port, dl_src=dst_mac, dl_dst=src_mac)
            
            actions = [parser.OFPActionOutput(out_port)]
            fm = parser.OFPFlowMod(datapath=dp, match=match, idle_timeout=0, hard_timeout=0,
                                   priority=100, actions=actions)
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
        in_port = msg.in_port
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        src, dst = eth.src, eth.dst
        dpid = dp.id
        # Quiet noisy IPv6 multicast (e.g., ff02::fb -> 33:33:00:00:00:fb): drop locally
        if dst.lower().startswith('33:33'):
            match = parser.OFPMatch(dl_dst=dst)
            fm = parser.OFPFlowMod(datapath=dp, match=match, idle_timeout=10,
                                   hard_timeout=0, priority=5, actions=[])
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
