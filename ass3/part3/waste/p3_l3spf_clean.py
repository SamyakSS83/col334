#!/usr/bin/env python3
"""
Part 3: L3-like Shortest Path Routing - CLEAN IMPLEMENTATION

This implements proper L3 routing by:
1. Matching on IP destination
2. Rewriting Ethernet MACs at each hop
3. Installing flows proactively after topology discovery

The key insight: We match ONLY on nw_dst (destination IP), not on dl_dst.
This allows the flow to match regardless of what MAC the packet currently has.
"""

import os
import json
import heapq
from collections import defaultdict

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
from ryu.topology import event
from ryu.topology.api import get_link
from ryu.ofproto import ofproto_v1_0
from ryu.lib.packet import packet, ethernet, ether_types
from ryu.lib.packet import arp,ipv4

class L3SPF(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_0.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(L3SPF, self).__init__(*args, **kwargs)
        
        # Load config
        cfg_path = os.environ.get('CFG', os.path.join(os.path.dirname(__file__), 'p3_config.json'))
        with open(cfg_path) as f:
            cfg = json.load(f)
        
        # Build graph
        self.graph = {}
        for link in cfg.get('links', []):
            u = int(link['src'].lstrip('r'))
            v = int(link['dst'].lstrip('r'))
            w = link.get('cost', 1)
            self.graph.setdefault(u, {})[v] = w
            self.graph.setdefault(v, {})[u] = w
        
        # Gateway MACs per switch (host-facing interface)
        self.gateway_mac = {}
        for sw in cfg['switches']:
            for intf in sw['interfaces']:
                if intf['neighbor'].startswith('h'):
                    self.gateway_mac[sw['dpid']] = intf['mac'].lower()
        
        # Host info: mac -> {dpid, ip, port}
        # Determine port from interface config
        self.hosts = {}
        for h in cfg['hosts']:
            dpid = int(h['switch'].lstrip('r'))
            mac = h['mac'].lower()
            
            # Find which port/interface this host is on
            host_port = None
            for sw in cfg['switches']:
                if sw['dpid'] == dpid:
                    for idx, intf in enumerate(sw['interfaces']):
                        if intf['neighbor'] == h['name']:
                            # Port number is typically interface index + 1
                            # But eth numbering might not match, so we extract from interface name
                            intf_name = intf['name']  # e.g., "r1-eth1" or "r6-eth3"
                            if 'eth' in intf_name:
                                host_port = int(intf_name.split('eth')[1])
                            else:
                                host_port = idx + 1
                            break
            
            self.hosts[mac] = {
                'dpid': dpid,
                'ip': h['ip'],
                'port': host_port if host_port else 1
            }
        
        self.logger.info("[INIT] Loaded: %d switches, %d hosts", len(cfg['switches']), len(self.hosts))
        self.logger.info("[INIT] Hosts: %s", self.hosts)
        
        # Runtime
        self.datapaths = {}
        self.adjacency = defaultdict(dict)
        self.flows_installed = False

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _dp_state(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
            self.logger.info("[DP] s%d connected", dp.id)
            # Install ARP flood rule
            # parser = dp.ofproto_parser
            # match = parser.OFPMatch(dl_type=0x0806)
            # actions = [parser.OFPActionOutput(dp.ofproto.OFPP_FLOOD)]
            # fm = parser.OFPFlowMod(datapath=dp, match=match, priority=100, actions=actions)
            # dp.send_msg(fm)
            # Install ARP flood rule only to host ports
            parser = dp.ofproto_parser
            ofp = dp.ofproto

            # Build list of host-facing ports for this switch
            host_ports = []
            for mac, h in self.hosts.items():
                if h['dpid'] == dp.id:
                    host_ports.append(h['port'])

            # Flood only to host ports
            actions = [parser.OFPActionOutput(p) for p in host_ports]
            match = parser.OFPMatch(dl_type=0x0806)
            fm = parser.OFPFlowMod(datapath=dp, match=match, priority=100, actions=actions)
            dp.send_msg(fm)

        elif ev.state == DEAD_DISPATCHER and dp.id in self.datapaths:
            del self.datapaths[dp.id]

    @set_ev_cls(event.EventSwitchEnter)
    def _switch_enter(self, ev):
        # Just log, don't try to install yet
        pass
    
    @set_ev_cls(event.EventLinkAdd)
    def _link_add(self, ev):
        # Rebuild adjacency every time a link is added
        self.adjacency.clear()
        for lk in get_link(self, None):
            self.adjacency[lk.src.dpid][lk.dst.dpid] = lk.src.port_no
        
        self.logger.info("[TOPO] Links: %s", dict(self.adjacency))
        self._try_install()

    def _try_install(self):
        if self.flows_installed or len(self.hosts) < 2:
            return
        
        # Get the two hosts
        macs = list(self.hosts.keys())
        h1, h2 = macs[0], macs[1]
        
        # Compute paths
        s1 = self.hosts[h1]['dpid']
        s2 = self.hosts[h2]['dpid']
        path_fwd = self._dijkstra(s1, s2)
        path_rev = self._dijkstra(s2, s1)
        
        if not path_fwd or not path_rev:
            return
        
        # Check all DPs ready
        for dpid in set(path_fwd + path_rev):
            if dpid not in self.datapaths:
                return
        
        # Check all adjacencies in path exist
        for path in [path_fwd, path_rev]:
            for i in range(len(path) - 1):
                if path[i+1] not in self.adjacency.get(path[i], {}):
                    self.logger.info("[INSTALL] Adjacency not ready: s%d -> s%d", path[i], path[i+1])
                    return
        
        self.logger.info("[INSTALL] h1(%s) <-> h2(%s)", h1, h2)
        self.logger.info("[INSTALL] Forward path: %s", path_fwd)
        self.logger.info("[INSTALL] Reverse path: %s", path_rev)
        
        # Install flows
        self._install_flows(h1, h2, path_fwd)
        self._install_flows(h2, h1, path_rev)
        
        self.flows_installed = True

    def _dijkstra(self, src, dst):
        dist = {n: float('inf') for n in self.graph}
        prev = {n: None for n in self.graph}
        dist[src] = 0
        pq = [(0, src)]
        
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for v, w in self.graph[u].items():
                if d + w < dist[v]:
                    dist[v] = d + w
                    prev[v] = u
                    heapq.heappush(pq, (d + w, v))
        
        if dist[dst] == float('inf'):
            return None
        
        path = []
        cur = dst
        while cur:
            path.append(cur)
            cur = prev[cur]
        return list(reversed(path))

    def _install_flows(self, src_mac, dst_mac, path):
        """Install flows along path for src -> dst"""
        src_ip = self.hosts[src_mac]['ip']
        dst_ip = self.hosts[dst_mac]['ip']
        dst_port = self.hosts[dst_mac]['port']
        src_port = self.hosts[src_mac]['port']
        
        for i, dpid in enumerate(path):
            dp = self.datapaths[dpid]
            parser = dp.ofproto_parser
            
            # Determine output port
            if i == len(path) - 1:
                # Last hop: output to host
                out_port = dst_port
            else:
                # Forward to next switch
                next_dpid = path[i + 1]
                out_port = self.adjacency[dpid][next_dpid]
            
            # Get this switch's gateway MAC
            gw = self.gateway_mac.get(dpid, f"00:00:00:00:{dpid:02x}:01")
            
            # On ingress switch, also match on dl_dst=gateway to catch packets from host
            # This prevents OVS LOCAL port from capturing the packet
            if i == 0:
                match_ingress = parser.OFPMatch(
                    in_port=src_port,
                    dl_type=0x0800,
                    dl_dst=gw,
                    nw_dst=dst_ip
                )
                actions_ingress = [
                    parser.OFPActionSetDlSrc(gw),
                    parser.OFPActionSetDlDst(dst_mac),
                    parser.OFPActionOutput(out_port)
                ]
                fm_ingress = parser.OFPFlowMod(
                    datapath=dp,
                    match=match_ingress,
                    priority=2000,  # Higher priority to catch before LOCAL
                    idle_timeout=0,
                    hard_timeout=0,
                    actions=actions_ingress
                )
                dp.send_msg(fm_ingress)
                self.logger.info(
                    "[FLOW-INGRESS] s%d: in=%d dl_dst=%s nw_dst=%s -> set_dl_dst=%s, out=%d",
                    dpid, src_port, gw, dst_ip, dst_mac, out_port
                )
            
            # General rule matching on nw_dst for transit traffic
            match = parser.OFPMatch(
                dl_type=0x0800,
                nw_dst=dst_ip
            )
            
            actions = [
                parser.OFPActionSetDlSrc(gw),
                parser.OFPActionSetDlDst(dst_mac),
                parser.OFPActionOutput(out_port)
            ]
            
            fm = parser.OFPFlowMod(
                datapath=dp,
                match=match,
                priority=1000,
                idle_timeout=0,
                hard_timeout=0,
                actions=actions
            )
            dp.send_msg(fm)
            
            self.logger.info(
                "[FLOW] s%d: nw_dst=%s -> set_dl_src=%s, set_dl_dst=%s, out=%d",
                dpid, dst_ip, gw, dst_mac, out_port
            )

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            a = pkt.get_protocol(arp.arp)
            if a.opcode == arp.ARP_REQUEST:
                # Find destination host
                target_ip = a.dst_ip
                dst_host = None
                for mac, h in self.hosts.items():
                    if h['ip'] == target_ip:
                        dst_host = h
                        break
                if dst_host:
                    self.logger.info("[ARP-REPLY] %s -> %s", a.src_ip, a.dst_ip)
                    self._send_arp_reply(
                        datapath=msg.datapath,
                        src_mac=eth.dst,   # our gateway MAC
                        src_ip=target_ip,
                        dst_mac=eth.src,
                        dst_ip=a.src_ip,
                        out_port=msg.in_port
                    )
                return 
        
        if eth and eth.ethertype not in [ether_types.ETH_TYPE_LLDP]:
            
            self.logger.debug("[PKTIN] s%d: %s -> %s (0x%04x)", 
                            msg.datapath.id, eth.src, eth.dst, eth.ethertype)
        msg = ev.msg
        # dp = msg.datapath
        # parser = dp.ofproto_parser
        # ofp = dp.ofproto

        # pkt = packet.Packet(msg.data)
        # eth = pkt.get_protocol(ethernet.ethernet)
        
        # if eth.ethertype == ether_types.ETH_TYPE_ARP:
        #     a = pkt.get_protocol(arp.arp)
        #     if a.opcode == arp.ARP_REQUEST:
        #         # Check if we know this IP
        #         for mac, info in self.hosts.items():
        #             if a.dst_ip == info['ip']:
        #                 self.logger.info("[ARP] Replying to %s for %s -> %s", a.src_ip, a.dst_ip, mac)
                        
        #                 e = ethernet.ethernet(dst=eth.src, src=mac, ethertype=ether_types.ETH_TYPE_ARP)
        #                 a_reply = arp.arp(opcode=arp.ARP_REPLY,
        #                                 src_mac=mac, src_ip=a.dst_ip,
        #                                 dst_mac=eth.src, dst_ip=a.src_ip)
                        
        #                 p = packet.Packet()
        #                 p.add_protocol(e)
        #                 p.add_protocol(a_reply)
        #                 p.serialize()
                        
        #                 actions = [parser.OFPActionOutput(msg.match['in_port'])]
        #                 out = parser.OFPPacketOut(datapath=dp,
        #                                         buffer_id=ofp.OFP_NO_BUFFER,
        #                                         in_port=ofp.OFPP_CONTROLLER,
        #                                         actions=actions,
        #                                         data=p.data)
        #                 dp.send_msg(out)
        #                 return
        #     return  # Do not flood further

    def _send_arp_reply(self, datapath, src_mac, src_ip, dst_mac, dst_ip, out_port):
        parser = datapath.ofproto_parser
        ofp = datapath.ofproto
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(
            ethertype=ether_types.ETH_TYPE_ARP,
            dst=dst_mac,
            src=src_mac))
        pkt.add_protocol(arp.arp(
            opcode=arp.ARP_REPLY,
            src_mac=src_mac,
            src_ip=src_ip,
            dst_mac=dst_mac,
            dst_ip=dst_ip))
        pkt.serialize()

        actions = [parser.OFPActionOutput(out_port)]
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofp.OFP_NO_BUFFER,
            in_port=ofp.OFPP_CONTROLLER,
            actions=actions,
            data=pkt.data)
        datapath.send_msg(out)
