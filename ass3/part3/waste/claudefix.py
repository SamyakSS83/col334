#!/usr/bin/env python3
"""
Part 3: L3-like Shortest Path Routing - WORKING VERSION

Requirements:
- Switch interfaces have MACs but NO IPs (controller handles ARP)
- Controller responds to ARP requests for gateway IPs
- IP forwarding flows installed proactively
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
from ryu.lib.packet import packet, ethernet, ether_types, arp as arp_lib


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
        
        # All interface IPs and MACs on each switch
        self.switch_interfaces = {}  # dpid -> [(ip, mac), ...]
        self.gateway_mac = {}  # dpid -> gateway MAC for host-facing interface
        self.gateway_ip = {}   # dpid -> gateway IP for host-facing interface
        
        for sw in cfg['switches']:
            dpid = sw['dpid']
            self.switch_interfaces[dpid] = []
            
            for intf in sw['interfaces']:
                ip = intf.get('ip')
                mac = intf.get('mac', '').lower()
                if ip and mac:
                    self.switch_interfaces[dpid].append((ip, mac))
                
                # Gateway interface (host-facing)
                if intf['neighbor'].startswith('h'):
                    self.gateway_mac[dpid] = mac
                    self.gateway_ip[dpid] = ip
        
        # Host info: mac -> {dpid, ip, port}
        self.hosts = {}
        for h in cfg['hosts']:
            dpid = int(h['switch'].lstrip('r'))
            mac = h['mac'].lower()
            
            # Find which port this host is on
            host_port = None
            for sw in cfg['switches']:
                if sw['dpid'] == dpid:
                    for idx, intf in enumerate(sw['interfaces']):
                        if intf['neighbor'] == h['name']:
                            intf_name = intf['name']
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
        self.logger.info("[INIT] Gateways: MAC=%s, IP=%s", self.gateway_mac, self.gateway_ip)
        # build reverse lookup for gateway IP -> MAC
        self.gateway_by_ip = {ip: self.gateway_mac[dpid] for dpid, ip in self.gateway_ip.items()}
        
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
            
            # Install high-priority ARP flow: send ALL ARP to controller
            parser = dp.ofproto_parser
            match_arp = parser.OFPMatch(dl_type=0x0806)
            actions_arp = [parser.OFPActionOutput(dp.ofproto.OFPP_CONTROLLER)]
            fm_arp = parser.OFPFlowMod(
                datapath=dp,
                match=match_arp,
                priority=65535,
                actions=actions_arp
            )
            dp.send_msg(fm_arp)
            
            # Install table-miss flow
            match = parser.OFPMatch()
            actions = [parser.OFPActionOutput(dp.ofproto.OFPP_CONTROLLER)]
            fm = parser.OFPFlowMod(
                datapath=dp,
                match=match,
                priority=0,
                actions=actions
            )
            dp.send_msg(fm)
            
        elif ev.state == DEAD_DISPATCHER and dp.id in self.datapaths:
            del self.datapaths[dp.id]

    @set_ev_cls(event.EventSwitchEnter)
    def _switch_enter(self, ev):
        pass
    
    @set_ev_cls(event.EventLinkAdd)
    def _link_add(self, ev):
        self.adjacency.clear()
        for lk in get_link(self, None):
            self.adjacency[lk.src.dpid][lk.dst.dpid] = lk.src.port_no
        
        self.logger.info("[TOPO] Links: %s", dict(self.adjacency))
        self._try_install()

    def _try_install(self):
        if self.flows_installed or len(self.hosts) < 2:
            return
        
        macs = list(self.hosts.keys())
        h1, h2 = macs[0], macs[1]
        
        s1 = self.hosts[h1]['dpid']
        s2 = self.hosts[h2]['dpid']
        path_fwd = self._dijkstra(s1, s2)
        path_rev = self._dijkstra(s2, s1)
        
        if not path_fwd or not path_rev:
            return
        
        for dpid in set(path_fwd + path_rev):
            if dpid not in self.datapaths:
                return
        
        for path in [path_fwd, path_rev]:
            for i in range(len(path) - 1):
                if path[i+1] not in self.adjacency.get(path[i], {}):
                    self.logger.info("[INSTALL] Adjacency not ready: s%d -> s%d", path[i], path[i+1])
                    return
        
        self.logger.info("[INSTALL] h1(%s) <-> h2(%s)", h1, h2)
        self.logger.info("[INSTALL] Forward path: %s", path_fwd)
        self.logger.info("[INSTALL] Reverse path: %s", path_rev)
        
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
                out_port = dst_port
            else:
                next_dpid = path[i + 1]
                out_port = self.adjacency[dpid][next_dpid]
            
            # Determine MACs
            if i == len(path) - 1:
                next_hop_mac = dst_mac
            else:
                next_dpid = path[i + 1]
                next_hop_mac = self.gateway_mac.get(next_dpid, f"00:00:00:00:{next_dpid:02x}:01")
            
            src_mac_this = self.gateway_mac.get(dpid, f"00:00:00:00:{dpid:02x}:01")
            
            # Install flow
            match = parser.OFPMatch(
                dl_type=0x0800,
                nw_dst=dst_ip
            )
            
            actions = [
                parser.OFPActionSetDlSrc(src_mac_this),
                parser.OFPActionSetDlDst(next_hop_mac),
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
                dpid, dst_ip, src_mac_this, next_hop_mac, out_port
            )

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        dpid = dp.id
        in_port = msg.in_port
        
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        
        if not eth:
            return
        
        # Handle ARP requests and send replies for gateway IPs, flood others
        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            arp_pkt = pkt.get_protocol(arp_lib.arp)
            # debug log for ARP PacketIn
            if arp_pkt:
                self.logger.info("[PKTIN][ARP] s%d port %d: who-has %s from %s", dpid, in_port, arp_pkt.dst_ip, arp_pkt.src_mac)
            if arp_pkt and arp_pkt.opcode == arp_lib.ARP_REQUEST:
                tgt_ip = arp_pkt.dst_ip
                # reply if asking our gateway IP
                if tgt_ip in self.gateway_by_ip:
                    src_mac = self.gateway_by_ip[tgt_ip]
                    # build ARP reply
                    rep = packet.Packet()
                    rep.add_protocol(ethernet.ethernet(
                        ethertype=ether_types.ETH_TYPE_ARP,
                        src=src_mac,
                        dst=eth.src))
                    rep.add_protocol(arp_lib.arp(
                        opcode=arp_lib.ARP_REPLY,
                        src_mac=src_mac,
                        src_ip=tgt_ip,
                        dst_mac=arp_pkt.src_mac,
                        dst_ip=arp_pkt.src_ip))
                    rep.serialize()
                    actions = [parser.OFPActionOutput(in_port)]
                    out = parser.OFPPacketOut(
                        datapath=dp,
                        buffer_id=ofp.OFP_NO_BUFFER,
                        in_port=ofp.OFPP_CONTROLLER,
                        actions=actions,
                        data=rep.data)
                    dp.send_msg(out)
                    return
            # flood any other ARP (requests or replies)
            actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
            out = parser.OFPPacketOut(
                datapath=dp,
                buffer_id=ofp.OFP_NO_BUFFER,
                in_port=in_port,
                actions=actions,
                data=msg.data)
            dp.send_msg(out)
            return
        
        # Ignore LLDP
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        
        self.logger.debug("[PKTIN] s%d port %d: %s -> %s", dpid, in_port, eth.src, eth.dst)