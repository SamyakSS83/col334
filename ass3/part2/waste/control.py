"""
Ryu L2 shortest-path controller with ECMP support (random or weighted)

Save this file as `ryu_sp_l2_controller.py` and run it with:
    ryu-manager ryu_sp_l2_controller.py

Place a `config.json` in the same directory (example below).

Features:
- Reads topology weight matrix from config.json
- Builds weighted graph and runs Dijkstra from source to compute shortest paths
- Enumerates all equal-cost shortest paths (ECMP) and selects randomly by default
- Optionally selects path based on lightweight link utilization weighting ("weighted")
- Learns host MAC -> (dpid, port) and installs per-flow rules along the chosen path
- Installs reverse flow so reply packets follow same path (L2 rules match eth_src/eth_dst)
- Periodically collects OFP port stats to compute per-link utilization (used by weighted ECMP)

Notes / assumptions:
- Switch names in config.json are like ["s1","s2", ...]. DPID is assumed to be the numeric
  suffix (so s1 -> dpid 1). This matches standard Mininet naming.
- The app also uses Ryu topology discovery (LLDP) to learn which port on dpid A connects to dpid B.

"""

import json
import random
import time
import threading
import heapq
import logging
from collections import defaultdict, deque

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.lib.packet import packet, ethernet
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub

# topology API
from ryu.topology import event, switches
from ryu.topology.api import get_all_link, get_all_switch

LOG = logging.getLogger('ryu.app.sp_l2')
LOG.setLevel(logging.INFO)


class ShortestPathL2(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(ShortestPathL2, self).__init__(*args, **kwargs)

        # Config
        self.config = self._load_config('config.json')
        self.nodes = self.config.get('nodes', [])
        self.node_to_idx = {n: i for i, n in enumerate(self.nodes)}
        self.idx_to_node = {i: n for n, i in self.node_to_idx.items()}
        self.weight_matrix = self.config.get('weight_matrix', [])
        self.ecmp_enabled = bool(self.config.get('ecmp', False))
        # optional mode: "random" (default) or "weighted"
        self.ecmp_mode = self.config.get('ecmp_mode', 'random')

        # Graph stored as adjacency list with weights using node indices
        self.graph = self._build_graph(self.weight_matrix)

        # Topology bookkeeping
        # adjacency_ports[(src_dpid, dst_dpid)] = src_port_no
        self.adjacency_ports = dict()
        # set of known datapaths {dpid: datapath}
        self.datapaths = dict()

        # MAC learning table: mac -> (dpid, port)
        self.mac_to_port = dict()

        # Link utilization metrics (bytes per second) keyed by (src, dst) dpid tuple
        self.link_usage_bps = defaultdict(float)
        # last seen port stats for differencing
        self._last_port_stats = dict()  # (dpid,port)->(tx_bytes, rx_bytes, timestamp)

        # Start a thread for stats polling (for weighted ECMP)
        self.stats_thread = hub.spawn(self._stats_loop)

    def _load_config(self, fname):
        try:
            with open(fname) as f:
                cfg = json.load(f)
                return cfg
        except Exception as e:
            LOG.warning('Cannot load config.json (%s). Using defaults. Error: %s', fname, e)
            return {}

    def _build_graph(self, weight_matrix):
        n = len(weight_matrix)
        g = defaultdict(list)  # idx -> list of (neighbor_idx, weight)
        for i in range(n):
            for j in range(n):
                w = weight_matrix[i][j]
                if w and w > 0:
                    g[i].append((j, w))
        return g

    # --------------------- Topology discovery handlers ---------------------
    @set_ev_cls(event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        sw = ev.switch
        dpid = sw.dp.id
        self.datapaths[dpid] = sw.dp
        LOG.info('Switch entered: dpid=%s', dpid)

    @set_ev_cls(event.EventSwitchLeave)
    def switch_leave_handler(self, ev):
        dpid = ev.switch.dp.id
        if dpid in self.datapaths:
            del self.datapaths[dpid]
        LOG.info('Switch left: dpid=%s', dpid)

    @set_ev_cls(event.EventLinkAdd)
    def link_add_handler(self, ev):
        link = ev.link
        src = link.src
        dst = link.dst
        self.adjacency_ports[(src.dpid, dst.dpid)] = src.port_no
        LOG.info('Link added: %s:%s -> %s:%s', src.dpid, src.port_no, dst.dpid, dst.port_no)

    @set_ev_cls(event.EventLinkDelete)
    def link_del_handler(self, ev):
        link = ev.link
        src = link.src
        dst = link.dst
        self.adjacency_ports.pop((src.dpid, dst.dpid), None)
        LOG.info('Link deleted: %s -> %s', src.dpid, dst.dpid)

    # Called when datapath connects (openflow handshake)
    @set_ev_cls(ofp_event.EventOFPStateChange)
    def _state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER:
            if dp.id in self.datapaths:
                del self.datapaths[dp.id]

    # ---------------------- Packet handling and learning ----------------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        src_mac = eth.src
        dst_mac = eth.dst

        # learn the source
        self.mac_to_port[src_mac] = (dpid, in_port)

        # if destination known, compute path
        if dst_mac in self.mac_to_port:
            dst_dpid, dst_port = self.mac_to_port[dst_mac]
            src_dpid, src_port = self.mac_to_port[src_mac]

            if src_dpid == dst_dpid:
                # same switch: just install flow on that switch
                out_port = dst_port
                actions = [parser.OFPActionOutput(out_port)]
                match = parser.OFPMatch(eth_src=src_mac, eth_dst=dst_mac)
                self._add_flow(dp, 100, match, actions)
                # Also forward this packet
                data = None
                if msg.buffer_id == ofp.OFP_NO_BUFFER:
                    data = msg.data
                out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                          in_port=in_port, actions=actions, data=data)
                dp.send_msg(out)
                return

            # compute path between switches
            path_candidates = self._get_shortest_paths(src_dpid, dst_dpid)
            if not path_candidates:
                LOG.info('No path found between %s and %s', src_dpid, dst_dpid)
                # fallback: flood
                self._flood(msg)
                return

            chosen_path = self._select_path(path_candidates)
            LOG.info('Chosen path (dpids): %s for %s->%s', chosen_path, src_mac, dst_mac)

            # install flows along chosen path
            self._install_path_flows(chosen_path, src_mac, dst_mac)

            # send the original packet along path (installing flow may have already done it in switch)
            # forward first hop manually
            first_dp = self.datapaths[chosen_path[0]]
            next_hop = chosen_path[1]
            out_port = self.adjacency_ports.get((chosen_path[0], next_hop))
            if out_port is None and chosen_path[0] == dst_dpid:
                out_port = dst_port
            if out_port is None:
                LOG.warning('No out_port for first hop on %s -> %s; flooding', chosen_path[0], next_hop)
                self._flood(msg)
                return

            actions = [first_dp.ofproto_parser.OFPActionOutput(out_port)]
            data = None
            if msg.buffer_id == ofp.OFP_NO_BUFFER:
                data = msg.data
            out = first_dp.ofproto_parser.OFPPacketOut(datapath=first_dp, buffer_id=msg.buffer_id,
                                                       in_port=in_port, actions=actions, data=data)
            first_dp.send_msg(out)

        else:
            # destination unknown: flood
            self._flood(msg)

    def _flood(self, msg):
        dp = msg.datapath
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
        data = None
        if msg.buffer_id == ofp.OFP_NO_BUFFER:
            data = msg.data
        out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                  in_port=msg.match['in_port'], actions=actions, data=data)
        dp.send_msg(out)

    # ---------------------- Path computation (Dijkstra + ECMP) ----------------------
    def _get_shortest_paths(self, src_dpid, dst_dpid):
        """
        Return list of candidate paths (each path is a list of dpids) between src and dst.
        Uses the weight matrix from config. Assumes nodes configured as s1..sn and dpids
        equal numeric suffixes.
        """
        # convert dpids (integers) to indices used in config (s1->idx0 etc)
        def dpid_to_idx(dpid):
            name = f's{dpid}'
            return self.node_to_idx.get(name, None)

        s_idx = dpid_to_idx(src_dpid)
        t_idx = dpid_to_idx(dst_dpid)
        if s_idx is None or t_idx is None:
            LOG.warning('DPID to node mapping missing for %s or %s', src_dpid, dst_dpid)
            return []

        # run Dijkstra to get distances from s_idx
        dist, prev = self._dijkstra(s_idx)
        if dist[t_idx] == float('inf'):
            return []

        # enumerate all shortest paths using only edges (u,v) where dist[u] + w(u,v) == dist[v]
        paths_idx = []

        def dfs(cur, path):
            if cur == t_idx:
                paths_idx.append(list(path))
                return
            for (nbr, w) in self.graph.get(cur, []):
                if dist[cur] + w == dist[nbr]:
                    path.append(nbr)
                    dfs(nbr, path)
                    path.pop()

        dfs(s_idx, [s_idx])

        # convert indices back to dpids (s1 -> 1)
        paths_dpid = []
        for p in paths_idx:
            dpids = []
            for idx in p:
                node = self.idx_to_node.get(idx)
                if node is None:
                    dpids = []
                    break
                # assume node format sN
                dpid = int(node[1:])
                dpids.append(dpid)
            if dpids:
                paths_dpid.append(dpids)
        return paths_dpid

    def _dijkstra(self, src_idx):
        n = len(self.nodes)
        dist = [float('inf')] * n
        dist[src_idx] = 0
        # prev not single-valued because multiple predecessors may exist on shortest paths,
        # but we only need distances for path reconstruction.
        pq = [(0, src_idx)]
        while pq:
            d, u = heapq.heappop(pq)
            if d != dist[u]:
                continue
            for v, w in self.graph.get(u, []):
                nd = d + w
                if nd < dist[v]:
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return dist, None

    def _select_path(self, path_candidates):
        if not self.ecmp_enabled or len(path_candidates) == 1:
            return path_candidates[0]
        if self.ecmp_mode == 'random' or self.ecmp_mode not in ('weighted',):
            return random.choice(path_candidates)
        # weighted mode: compute score = sum(link_usage) along path and choose path with smallest score
        best = None
        best_score = float('inf')
        for p in path_candidates:
            score = 0.0
            for i in range(len(p) - 1):
                a, b = p[i], p[i + 1]
                score += self.link_usage_bps.get((a, b), 0.0)
            if score < best_score:
                best_score = score
                best = p
        if best is None:
            return random.choice(path_candidates)
        return best

    # ---------------------- Install flows along path ----------------------
    def _install_path_flows(self, path, src_mac, dst_mac, idle_timeout=30):
        # ensure path is sequence of dpids
        for i in range(len(path)):
            dpid = path[i]
            datapath = self.datapaths.get(dpid)
            if datapath is None:
                LOG.warning('Datapath %s not ready', dpid)
                continue
            parser = datapath.ofproto_parser
            ofp = datapath.ofproto

            # determine out_port
            if i == len(path) - 1:
                # last switch: send to host port for destination
                dst_info = self.mac_to_port.get(dst_mac)
                if dst_info and dst_info[0] == dpid:
                    out_port = dst_info[1]
                else:
                    LOG.warning('Destination port for %s not found on %s', dst_mac, dpid)
                    continue
            else:
                nxt = path[i + 1]
                out_port = self.adjacency_ports.get((dpid, nxt))
                if out_port is None:
                    LOG.warning('No inter-switch port recorded for %s -> %s', dpid, nxt)
                    continue

            match = parser.OFPMatch(eth_src=src_mac, eth_dst=dst_mac)
            actions = [parser.OFPActionOutput(out_port)]
            inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
            mod = parser.OFPFlowMod(datapath=datapath, priority=100,
                                    match=match, instructions=inst,
                                    idle_timeout=idle_timeout)
            datapath.send_msg(mod)

        # install reverse path
        rev_path = list(reversed(path))
        for i in range(len(rev_path)):
            dpid = rev_path[i]
            datapath = self.datapaths.get(dpid)
            if datapath is None:
                continue
            parser = datapath.ofproto_parser
            ofp = datapath.ofproto
            if i == len(rev_path) - 1:
                # send to host port for original source
                src_info = self.mac_to_port.get(src_mac)
                if src_info and src_info[0] == dpid:
                    out_port = src_info[1]
                else:
                    continue
            else:
                nxt = rev_path[i + 1]
                out_port = self.adjacency_ports.get((dpid, nxt))
                if out_port is None:
                    continue
            match = parser.OFPMatch(eth_src=dst_mac, eth_dst=src_mac)
            actions = [parser.OFPActionOutput(out_port)]
            inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
            mod = parser.OFPFlowMod(datapath=datapath, priority=100,
                                    match=match, instructions=inst,
                                    idle_timeout=idle_timeout)
            datapath.send_msg(mod)

    # ---------------------- Utility: add a flow on a single switch ----------------------
    def _add_flow(self, datapath, priority, match, actions, idle_timeout=30):
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                match=match, instructions=inst, idle_timeout=idle_timeout)
        datapath.send_msg(mod)

    # ---------------------- Stats polling for weighted ECMP ----------------------
    def _stats_loop(self):
        # poll interval in seconds
        poll_interval = 2
        while True:
            try:
                if self.ecmp_mode == 'weighted' and self.datapaths:
                    for dpid, dp in list(self.datapaths.items()):
                        self._request_port_stats(dp)
                hub.sleep(poll_interval)
            except Exception as e:
                LOG.exception('Exception in stats loop: %s', e)
                hub.sleep(1)

    def _request_port_stats(self, datapath):
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        req = parser.OFPPortStatsRequest(datapath, 0, ofp.OFPP_ANY)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        body = ev.msg.body
        now = time.time()
        dpid = ev.msg.datapath.id
        for stat in body:
            port_no = stat.port_no
            tx_bytes = stat.tx_bytes
            rx_bytes = stat.rx_bytes
            key = (dpid, port_no)
            last = self._last_port_stats.get(key)
            if last is not None:
                last_bytes, last_ts = last
                delta_t = now - last_ts
                if delta_t > 0:
                    delta_bytes = (tx_bytes + rx_bytes) - last_bytes
                    bps = delta_bytes / max(delta_t, 1e-6)
                    # store bps for links that use this (dpid,port). We will map to (dpid,neighbor)
                    # Find neighbor dpid for this port by searching adjacency_ports
                    # adjacency_ports maps (src_dpid,dst_dpid) -> port_no
                    # so any (src, dst) with src==dpid and port_no==port_no corresponds to an inter-switch link
                    for (src, dst), pno in list(self.adjacency_ports.items()):
                        if src == dpid and pno == port_no:
                            self.link_usage_bps[(src, dst)] = bps
            # update last seen
            self._last_port_stats[key] = ((tx_bytes + rx_bytes), now)

    # ---------------------- Helper to list flows (for debugging) ----------------------
    def dump_state(self):
        LOG.info('Datapaths: %s', list(self.datapaths.keys()))
        LOG.info('Adjacency ports: %s', self.adjacency_ports)
        LOG.info('MAC table: %s', self.mac_to_port)
        LOG.info('Link usage (bps): %s', dict(self.link_usage_bps))


