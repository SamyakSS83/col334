#!/usr/bin/env python3
"""
Ryu Application: Part3 L3-like Shortest Path Router
  - Reads p3_config.json for switches, hosts, and link costs
  - Replies to ARP for router interfaces and host-facing gateways
  - Computes shortest paths via Dijkstra (networkx)
  - Installs OF1.3 flows: TTL decrement, MAC rewrite, output
"""
import os
import json
import ipaddress
import networkx as nx

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, arp, ipv4
from ryu.lib.packet import ether_types

# Config path (same directory)
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'p3_config.json')

def _extract_port(ifname):
    if 'eth' in ifname:
        try:
            return int(ifname.split('eth')[1])
        except:
            pass
    return None

class P3L3Router(app_manager.RyuApp):
    """L3 shortest-path router Ryu app"""
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(P3L3Router, self).__init__(*args, **kwargs)
        # load config
        with open(CONFIG_PATH) as f:
            self.cfg = json.load(f)
        # switch maps
        self.switches = {sw['name']: sw for sw in self.cfg.get('switches', [])}
        self.name_to_dpid = {sw['name']: sw['dpid'] for sw in self.cfg.get('switches', [])}
        self.dpid_to_name = {sw['dpid']: sw['name'] for sw in self.cfg.get('switches', [])}
        # host maps
        self.hosts = {h['ip']: h for h in self.cfg.get('hosts', [])}
        # gateway maps: dpid->(ip,mac), ip->mac
        self.gateway = {}       # dpid -> (ip, mac)
        self.gateway_by_ip = {} # ip -> mac
        # adjacency and graph
        self.adjacency = {}     # dpid -> {nbr_dpid:port}
        self.G = nx.DiGraph()
        # datapaths
        self.datapaths = {}
        # prepare
        self._prepare_topology()
        self.logger.info('Loaded config: %d switches, %d hosts, %d links',
                         len(self.switches), len(self.hosts), self.G.number_of_edges())

    def _prepare_topology(self):
        # initialize adjacency entries
        for sw in self.cfg['switches']:
            self.adjacency[sw['dpid']] = {}
        # populate gateways and inter-switch
        for sw in self.cfg['switches']:
            u_name, u = sw['name'], sw['dpid']
            for intf in sw.get('interfaces', []):
                nbr = intf['neighbor']
                port = _extract_port(intf.get('name',''))
                # gateway
                if nbr.startswith('h'):
                    ip = intf['ip']; mac = intf['mac'].lower()
                    self.gateway[u] = (ip, mac)
                    self.gateway_by_ip[ip] = mac
                # inter-switch
                if nbr.startswith('r'):
                    v = self.name_to_dpid.get(nbr)
                    if v and port is not None:
                        self.adjacency[u][v] = port
        # build graph from links
        for link in self.cfg.get('links', []):
            s = link['src']; d = link['dst']; w = link.get('cost',1)
            self.G.add_edge(s, d, weight=w)
            self.G.add_edge(d, s, weight=w)

    def _add_flow(self, dp, priority, match, actions):
        parser = dp.ofproto_parser; ofp = dp.ofproto
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=dp, priority=priority,
                                match=match, instructions=inst)
        dp.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath; dpid = dp.id
        self.datapaths[dpid] = dp
        parser = dp.ofproto_parser; ofp = dp.ofproto
        self.logger.info('Switch %s connected', dpid)
        # table miss -> controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self._add_flow(dp, 0, match, actions)
        # drop LLDP
        self._add_flow(dp, 10000,
                       parser.OFPMatch(eth_type=ether_types.ETH_TYPE_LLDP), [])
        # drop ip ttl=1
        try:
            self._add_flow(dp, 9000,
                parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_ttl=1), [])
        except Exception:
            self.logger.warning('ip_ttl match unsupported on s%s', dpid)
        # ARP -> controller
        self._add_flow(dp, 2000,
                       parser.OFPMatch(eth_type=ether_types.ETH_TYPE_ARP),
                       actions)
        # local subnet IP -> controller
        name = self.dpid_to_name.get(dpid)
        if name:
            for intf in self.switches[name]['interfaces']:
                if intf.get('subnet') and intf['neighbor'].startswith('h'):
                    net = ipaddress.ip_network(intf['subnet'])
                    m = parser.OFPMatch(
                        eth_type=ether_types.ETH_TYPE_IP,
                        ipv4_dst=(str(net.network_address), str(net.netmask)))
                    self._add_flow(dp, 2000, m, actions)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg; dp = msg.datapath; dpid = dp.id
        parser = dp.ofproto_parser; ofp = dp.ofproto
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth_pkt = pkt.get_protocol(ethernet.ethernet)
        if not eth_pkt or eth_pkt.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        # ARP
        if eth_pkt.ethertype == ether_types.ETH_TYPE_ARP:
            arp_pkt = pkt.get_protocol(arp.arp)
            if arp_pkt and arp_pkt.opcode == arp.ARP_REQUEST:
                tgt = arp_pkt.dst_ip; reply_mac = None
                # gateway
                gw = self.gateway.get(dpid)
                if gw and gw[0] == tgt:
                    reply_mac = gw[1]
                else:
                    # other router interface
                    name = self.dpid_to_name.get(dpid)
                    for intf in self.switches[name]['interfaces']:
                        if intf.get('ip') == tgt:
                            reply_mac = intf['mac'].lower(); break
                if reply_mac:
                    # send ARP reply
                    out_pkt = packet.Packet()
                    out_pkt.add_protocol(ethernet.ethernet(
                        ethertype=ether_types.ETH_TYPE_ARP,
                        dst=eth_pkt.src, src=reply_mac))
                    out_pkt.add_protocol(arp.arp(
                        opcode=arp.ARP_REPLY,
                        src_mac=reply_mac, src_ip=tgt,
                        dst_mac=arp_pkt.src_mac, dst_ip=arp_pkt.src_ip))
                    out_pkt.serialize()
                    dp.send_msg(parser.OFPPacketOut(
                        datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                        in_port=ofp.OFPP_CONTROLLER,
                        actions=[parser.OFPActionOutput(in_port)],
                        data=out_pkt.data))
                    return
            # flood others
            dp.send_msg(parser.OFPPacketOut(
                datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                in_port=in_port,
                actions=[parser.OFPActionOutput(ofp.OFPP_FLOOD)],
                data=msg.data))
            return
        # IPv4
        if eth_pkt.ethertype == ether_types.ETH_TYPE_IP:
            ipv4_pkt = pkt.get_protocol(ipv4.ipv4)
            if not ipv4_pkt: return
            src, dst = ipv4_pkt.src, ipv4_pkt.dst
            # find switches by IP
            def find_sw(ip):
                for h in self.cfg['hosts']:
                    if h['ip'] == ip: return h['switch']
                for sw in self.cfg['switches']:
                    for intf in sw['interfaces']:
                        if intf.get('ip') == ip: return sw['name']
            s_src = find_sw(src); s_dst = find_sw(dst)
            if not s_src or not s_dst: return
            # shortest path
            try:
                path = nx.shortest_path(self.G, s_src, s_dst, weight='weight')
            except nx.NetworkXNoPath:
                return
            # install flows
            for i, sw_name in enumerate(path):
                dpid_sw = self.name_to_dpid[sw_name]
                dp_sw = self.datapaths.get(dpid_sw)
                if not dp_sw: return
                p = dp_sw.ofproto_parser; o = dp_sw.ofproto
                if i == len(path)-1:
                    # last hop to host
                    h = self.hosts.get(dst)
                    out_port = _extract_port(
                        next(intf['name'] for intf in self.switches[sw_name]['interfaces']
                             if intf['neighbor'] == h['name']))
                    dst_mac = h['mac'].lower()
                else:
                    # next hop towards switch
                    nxt = path[i+1]; ndp = self.name_to_dpid[nxt]
                    out_port = self.adjacency[dpid_sw][ndp]
                    # get MAC of next switch interface facing current
                    dst_mac = self.gateway.get(ndp, (None, None))[1]
                    if not dst_mac:
                        dst_mac = self._get_intf_mac(nxt, sw_name)
                # source MAC: interface on current switch toward next hop or host
                src_mac = self.gateway.get(dpid_sw, (None, None))[1]
                if not src_mac:
                    # fallback: interface mac toward neighbor
                    if i == len(path)-1:
                        neighbor = h['name']
                    else:
                        neighbor = path[i+1]
                    src_mac = self._get_intf_mac(sw_name, neighbor)
                match = p.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP, ipv4_dst=dst)
                acts = [p.OFPActionDecNwTtl(),
                        p.OFPActionSetField(eth_src=src_mac),
                        p.OFPActionSetField(eth_dst=dst_mac),
                        p.OFPActionOutput(out_port)]
                self._add_flow(dp_sw, 3000, match, acts)
            # send first packet
            first = self.name_to_dpid[path[0]]
            if first == dpid:
                # compute out and MACs
                if len(path)>1:
                    nxt = path[1]; ndp = self.name_to_dpid[nxt]
                    out_port = self.adjacency[dpid][ndp]
                    dst_mac = self.gateway.get(ndp)[1]
                else:
                    h = self.hosts.get(dst)
                    out_port = _extract_port(
                        next(intf['name'] for intf in self.switches[path[0]]['interfaces']
                             if intf['neighbor']==h['name']))
                    dst_mac = h['mac'].lower()
                src_mac = self.gateway.get(dpid)[1]
                actions = []
                try: actions.append(parser.OFPActionDecNwTtl())
                except: pass
                actions += [parser.OFPActionSetField(eth_src=src_mac),
                            parser.OFPActionSetField(eth_dst=dst_mac),
                            parser.OFPActionOutput(out_port)]
                dp.send_msg(parser.OFPPacketOut(
                    datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                    in_port=in_port, actions=actions, data=msg.data))
            return
"""
Ryu app: Part3 L3-like Shortest Path Router
 - Reads p3_config.json for topology, hosts, and link costs
 - Handles ARP for connected subnets and router interfaces
 - Computes weighted shortest paths via Dijkstra
 - Installs OpenFlow 1.3 flows for IPv4 forwarding, including TTL decrement and MAC rewrite
"""
import os
import json
import ipaddress
import networkx as nx

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, arp, ipv4
from ryu.lib.packet import ether_types

# Default config path
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'p3_config.json')

class P3L3Router(app_manager.RyuApp):
    """Ryu application for L3 shortest-path routing"""
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(P3L3Router, self).__init__(*args, **kwargs)
        # load JSON config
        cfg_file = os.environ.get('CFG', CONFIG_PATH)
        with open(cfg_file) as f:
            self.cfg = json.load(f)
        # maps
        self.switches = {sw['name']: sw for sw in self.cfg.get('switches', [])}
        self.name_to_dpid = {sw['name']: sw['dpid'] for sw in self.cfg.get('switches', [])}
        self.dpid_to_name = {sw['dpid']: sw['name'] for sw in self.cfg.get('switches', [])}
        self.hosts = {h['name']: h for h in self.cfg.get('hosts', [])}
        self.ip_to_host = {h['ip']: h for h in self.cfg.get('hosts', [])}
        # gateway: dpid -> (ip, mac)
        self.gateway = {}
        # reverse: ip -> mac
        self.gateway_by_ip = {}
        # adjacency and pub/sub
        self.adjacency = {}  # dpid -> {neighbor_dpid: port_no}
        # graph for Dijkstra
        self.G = nx.DiGraph()
        # datapaths
        self.datapaths = {}
        # prepare
        self._build_topology()
        self.logger.info('Config loaded: %d switches, %d hosts, %d links',
                         len(self.switches), len(self.hosts), self.G.number_of_edges())

    def _build_topology(self):
        # parse interfaces: gateways and inter-switch links
        for sw_name, sw in self.switches.items():
            dpid = sw['dpid']
            self.adjacency.setdefault(dpid, {})
            for intf in sw.get('interfaces', []):
                neigh = intf.get('neighbor')
                port = self._port_from_name(intf.get('name',''))
                # gateway to host
                if neigh.startswith('h'):
                    ip = intf.get('ip'); mac = intf.get('mac','').lower()
                    self.gateway[dpid] = (ip, mac)
                    self.gateway_by_ip[ip] = mac
                    # record host port as adjacency for host-facing if needed
                # inter-switch
                elif neigh.startswith('r'):
                    # record port, will fill reciprocal later
                    pass
        # build adjacency and graph edges
        for sw_name, sw in self.switches.items():
            dpid = sw['dpid']
            for intf in sw.get('interfaces', []):
                neigh = intf.get('neighbor')
                if neigh.startswith('r'):
                    port_local = self._port_from_name(intf.get('name',''))
                    neigh_dpid = self.name_to_dpid.get(neigh)
                    # find reverse port
                    port_remote = None
                    for nintf in self.switches[neigh].get('interfaces',[]):
                        if nintf.get('neighbor') == sw_name:
                            port_remote = self._port_from_name(nintf.get('name',''))
                            break
                    if neigh_dpid and port_local is not None:
                        self.adjacency[dpid][neigh_dpid] = port_local
        # build graph weighted
        for link in self.cfg.get('links', []):
            u = self.name_to_dpid[link['src']]
            v = self.name_to_dpid[link['dst']]
            w = link.get('cost',1)
            # graph nodes use switch names, but we map to dpids on lookup
            self.G.add_edge(link['src'], link['dst'], weight=w)
            self.G.add_edge(link['dst'], link['src'], weight=w)

    def _port_from_name(self, ifname):
        if 'eth' in ifname:
            try:
                return int(ifname.split('eth')[1])
            except: pass
        return None

    def _add_flow(self, dp, priority, match, actions):
        ofp = dp.ofproto; parser = dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=dp, priority=priority,
                               match=match, instructions=inst)
        dp.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features_handler(self, ev):
        dp = ev.msg.datapath; dpid = dp.id
        self.datapaths[dpid] = dp
        parser = dp.ofproto_parser; ofp = dp.ofproto
        self.logger.info('SwitchConnected: s%s', dpid)
        # table-miss
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self._add_flow(dp, 0, match, actions)
        # drop LLDP
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_LLDP)
        self._add_flow(dp, 10000, match, [])
        # drop TTL==1
        try:
            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_ttl=1)
            self._add_flow(dp, 9000, match, [])
        except Exception:
            self.logger.warning('ip_ttl match unsupported on s%s', dpid)
        # send ARP and local IP to controller
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_ARP)
        self._add_flow(dp, 2000, match, actions)
        # for each gateway subnet, send IP dst to controller
        sw_name = self.dpid_to_name.get(dpid)
        if sw_name:
            for intf in self.switches[sw_name].get('interfaces',[]):
                if intf.get('subnet') and intf.get('neighbor','').startswith('h'):
                    net = ipaddress.ip_network(intf['subnet'])
                    match = parser.OFPMatch(
                        eth_type=ether_types.ETH_TYPE_IP,
                        ipv4_dst=(str(net.network_address), str(net.netmask)))
                    self._add_flow(dp, 2000, match, actions)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg; dp = msg.datapath; dpid = dp.id
        parser = dp.ofproto_parser; ofp = dp.ofproto
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth_pkt = pkt.get_protocol(ethernet.ethernet)
        if not eth_pkt or eth_pkt.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        # ARP
        if eth_pkt.ethertype == ether_types.ETH_TYPE_ARP:
            arp_pkt = pkt.get_protocol(arp.arp)
            if arp_pkt and arp_pkt.opcode == arp.ARP_REQUEST:
                tgt = arp_pkt.dst_ip
                # reply for gateway or router-interface
                reply_mac = None
                # gateway
                gw = self.gateway.get(dpid)
                if gw and gw[0] == tgt:
                    reply_mac = gw[1]
                else:
                    # other router interfaces
                    sw_name = self.dpid_to_name.get(dpid)
                    for intf in self.switches[sw_name]['interfaces']:
                        if intf.get('ip') == tgt:
                            reply_mac = intf['mac'].lower(); break
                if reply_mac:
                    # build and send reply
                    out_pkt = packet.Packet()
                    out_pkt.add_protocol(ethernet.ethernet(
                        ethertype=ether_types.ETH_TYPE_ARP,
                        dst=eth_pkt.src, src=reply_mac))
                    out_pkt.add_protocol(arp.arp(
                        opcode=arp.ARP_REPLY,
                        src_mac=reply_mac, src_ip=tgt,
                        dst_mac=arp_pkt.src_mac, dst_ip=arp_pkt.src_ip))
                    out_pkt.serialize()
                    actions = [parser.OFPActionOutput(in_port)]
                    dp.send_msg(parser.OFPPacketOut(
                        datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                        in_port=ofp.OFPP_CONTROLLER,
                        actions=actions, data=out_pkt.data))
                    return
            # flood other ARP
            dp.send_msg(parser.OFPPacketOut(
                datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                in_port=in_port,
                actions=[parser.OFPActionOutput(ofp.OFPP_FLOOD)],
                data=msg.data))
            return
        # IPv4 forwarding
        if eth_pkt.ethertype == ether_types.ETH_TYPE_IP:
            ip_pkt = pkt.get_protocol(ipv4.ipv4)
            if not ip_pkt: return
            src_ip, dst_ip = ip_pkt.src, ip_pkt.dst
            # locate switches by IP
            def find_sw(ip):
                for h in self.cfg['hosts']:
                    if h['ip'] == ip: return h['switch']
                for sw in self.cfg['switches']:
                    for intf in sw['interfaces']:
                        if intf.get('ip') == ip: return sw['name']
            s_src = find_sw(src_ip); s_dst = find_sw(dst_ip)
            if not s_src or not s_dst: return
            # path computation
            try:
                path = nx.shortest_path(
                    self.G, s_src, s_dst, weight='weight')
            except nx.NetworkXNoPath:
                return
            # install flows on path
            for i, sw_name in enumerate(path):
                dp_sw = self.datapaths.get(self.name_to_dpid[sw_name])
                if not dp_sw: return
                p = dp_sw.ofproto_parser; o = dp_sw.ofproto
                # output port and next MAC
                if i == len(path)-1:
                    # last hop -> host
                    host = self.ip_to_host.get(dst_ip)
                    out_port = self._port_from_name(
                        next(intf['name'] for intf in self.switches[sw_name]['interfaces']
                             if intf['neighbor']==host['name']))
                    dst_mac = host['mac'].lower()
                else:
                    nxt = path[i+1]; dpid_n = self.name_to_dpid[nxt]
                    out_port = self.adjacency[self.name_to_dpid[sw_name]][dpid_n]
                    dst_mac = self.gateway.get(dpid_n)[1]
                src_mac = self.gateway.get(self.name_to_dpid[sw_name])[1]
                # match+actions
                match = p.OFPMatch(
                    eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_dst=dst_ip)
                actions = [p.OFPActionDecNwTtl(),
                           p.OFPActionSetField(eth_src=src_mac),
                           p.OFPActionSetField(eth_dst=dst_mac),
                           p.OFPActionOutput(out_port)]
                self._add_flow(dp_sw, 3000, match, actions)
            # immediate packet-out on first hop
            first_dpid = self.name_to_dpid[path[0]]
            if first_dpid == dpid:
                # same logic to pick out_port, src/dst MAC
                if len(path) > 1:
                    nxt = path[1]; dpid_n = self.name_to_dpid[nxt]
                    out_port = self.adjacency[dpid][dpid_n]
                    dst_mac = self.gateway.get(dpid_n)[1]
                else:
                    host = self.ip_to_host.get(dst_ip)
                    out_port = self._port_from_name(
                        next(intf['name'] for intf in self.switches[path[0]]['interfaces']
                             if intf['neighbor']==host['name']))
                    dst_mac = host['mac'].lower()
                src_mac = self.gateway.get(dpid)[1]
                actions = []
                try:
                    actions.append(parser.OFPActionDecNwTtl())
                except:
                    pass
                actions += [parser.OFPActionSetField(eth_src=src_mac),
                            parser.OFPActionSetField(eth_dst=dst_mac),
                            parser.OFPActionOutput(out_port)]
                dp.send_msg(parser.OFPPacketOut(
                    datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                    in_port=in_port, actions=actions, data=msg.data))
            return

class P3L3Router(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(P3L3Router, self).__init__(*args, **kwargs)
        # load config
        cfg_path = os.environ.get('CFG', CONFIG_PATH)
        with open(cfg_path) as f:
            self.cfg = json.load(f)
        # build data structures
        # switches
        self.switches = {sw['name']: sw for sw in self.cfg.get('switches', [])}
        self.name_to_dpid = {sw['name']: sw['dpid'] for sw in self.cfg.get('switches', [])}
        self.dpid_to_name = {sw['dpid']: sw['name'] for sw in self.cfg.get('switches', [])}
        # hosts
        self.hosts = {h['name']: h for h in self.cfg.get('hosts', [])}
        self.ip_to_host = {h['ip']: h for h in self.cfg.get('hosts', [])}
        # gateway: switch dpid -> (ip, mac)
        self.gateway = {}
        for sw in self.cfg.get('switches', []):
            for intf in sw.get('interfaces', []):
                if intf.get('neighbor','').startswith('h'):
                    self.gateway[sw['dpid']] = (intf['ip'], intf['mac'].lower())
        # graph
        self.G = nx.DiGraph()
        for link in self.cfg.get('links', []):
            u = link['src']
            v = link['dst']
            w = link.get('cost', 1)
            self.G.add_edge(u, v, weight=w)
            self.G.add_edge(v, u, weight=w)
        # state
        self.datapaths = {}  # dpid -> datapath

    def _extract_port(self, ifname):
        # rX-ethN
        if 'eth' in ifname:
            try:
                return int(ifname.split('eth')[1])
            except: pass
        return None

    def _add_flow(self, dp, priority, match, actions):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=dp, priority=priority, match=match, instructions=inst)
        dp.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features(self, ev):
        dp = ev.msg.datapath
        dpid = dp.id
        self.datapaths[dpid] = dp
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        self.logger.info("Switch %s connected", dpid)
        # table-miss
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self._add_flow(dp, 0, match, actions)
        # drop LLDP
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_LLDP)
        self._add_flow(dp, 10000, match, [])
        # drop TTL=1 if supported
        try:
            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_ttl=1)
            self._add_flow(dp, 9000, match, [])
        except Exception:
            self.logger.warning("ip_ttl match unavailable on s%s", dpid)
        # ARP to controller
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_ARP)
        self._add_flow(dp, 2000, match, actions)
        # IP to controller for local subnets
        sw_name = self.dpid_to_name.get(dpid)
        if sw_name:
            for intf in self.switches[sw_name]['interfaces']:
                if intf.get('subnet') and intf.get('neighbor','').startswith('h'):
                    net = ipaddress.ip_network(intf['subnet'])
                    match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                        ipv4_dst=(str(net.network_address), str(net.netmask)))
                    self._add_flow(dp, 2000, match, actions)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        dpid = dp.id
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth_pkt = pkt.get_protocol(ethernet.ethernet)
        if not eth_pkt:
            return
        if eth_pkt.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        # ARP
        if eth_pkt.ethertype == ether_types.ETH_TYPE_ARP:
            arp_pkt = pkt.get_protocol(arp.arp)
            if arp_pkt and arp_pkt.opcode == arp.ARP_REQUEST:
                tgt = arp_pkt.dst_ip
                # host-facing or router intf
                # reply if gateway or router-interface
                reply_mac = None
                # gateway
                gw = self.gateway.get(dpid)
                if gw and gw[0] == tgt:
                    reply_mac = gw[1]
                else:
                    # router intfs
                    sw_name = self.dpid_to_name.get(dpid)
                    for intf in self.switches[sw_name]['interfaces']:
                        if intf.get('ip') == tgt:
                            reply_mac = intf['mac'].lower()
                            break
                if reply_mac:
                    # build reply
                    out_pkt = packet.Packet()
                    out_pkt.add_protocol(ethernet.ethernet(
                        ethertype=ether_types.ETH_TYPE_ARP,
                        dst=eth_pkt.src,
                        src=reply_mac))
                    out_pkt.add_protocol(arp.arp(
                        opcode=arp.ARP_REPLY,
                        src_mac=reply_mac,
                        src_ip=tgt,
                        dst_mac=arp_pkt.src_mac,
                        dst_ip=arp_pkt.src_ip))
                    out_pkt.serialize()
                    actions = [parser.OFPActionOutput(in_port)]
                    out = parser.OFPPacketOut(
                        datapath=dp,
                        buffer_id=ofp.OFP_NO_BUFFER,
                        in_port=ofp.OFPP_CONTROLLER,
                        actions=actions,
                        data=out_pkt.data)
                    dp.send_msg(out)
                    return
            # flood
            actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
            out = parser.OFPPacketOut(
                datapath=dp,
                buffer_id=ofp.OFP_NO_BUFFER,
                in_port=in_port,
                actions=actions,
                data=msg.data)
            dp.send_msg(out)
            return
        # IPv4
        if eth_pkt.ethertype == ether_types.ETH_TYPE_IP:
            ip_pkt = pkt.get_protocol(ipv4.ipv4)
            if not ip_pkt:
                                    dst_mac = self._get_intf_mac(nxt, sw_name)
            src_ip = ip_pkt.src; dst_ip = ip_pkt.dst
            # locate switches
            def find_sw(ip):
                for h in self.cfg['hosts']:
                    if h['ip'] == ip: return h['switch']
                for sw in self.cfg['switches']:
                    for intf in sw['interfaces']:
                        if intf.get('ip') == ip: return sw['name']
            src_sw = find_sw(src_ip); dst_sw = find_sw(dst_ip)
            if not src_sw or not dst_sw:
                return
            # compute path
            try:
                path = nx.shortest_path(self.G, src_sw, dst_sw, weight='weight')
            except:
                                    dst_mac = self._get_intf_mac(nxt, sw_name)
            # install flows
            for i, sw_name in enumerate(path):
                d = self.name_to_dpid[sw_name]
                dpp = self.datapaths.get(d)
                if dpp is None: return
                pp = dpp.ofproto_parser; ofpp = dpp.ofproto
                if i == len(path)-1:
                    # last hop -> host
                    h = self.ip_to_host.get(dst_ip)
                    out_port = self._extract_port(
                        next(i['name'] for i in self.switches[sw_name]['interfaces'] if i['neighbor']==h['name']))
                    dst_mac = h['mac'].lower()
                else:
                    nxt = path[i+1]; nd = self.name_to_dpid[nxt]
                    # find port toward nxt
                    out_port = None
                    for intf in self.switches[sw_name]['interfaces']:
                        if intf['neighbor'] == nxt:
                            out_port = self._extract_port(intf['name']); break
                    # next-hop MAC is interface on nxt facing current switch
                    dst_mac = self._get_intf_mac(nxt, sw_name)
                # source MAC is interface on current switch facing next hop or host
                if i == len(path)-1:
                    src_mac = self._get_intf_mac(sw_name, h['name'])
                else:
                    src_mac = self._get_intf_mac(sw_name, nxt)
                # match and actions
                match = pp.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ipv4_dst=dst_ip)
                actions = [pp.OFPActionDecNwTtl(), pp.OFPActionSetField(eth_src=src_mac), pp.OFPActionSetField(eth_dst=dst_mac), pp.OFPActionOutput(out_port)]
                self._add_flow(dpp, 3000, match, actions)
            # packet_out on first hop
            first = self.name_to_dpid[path[0]]
            if first == dpid:
                nxt = path[1] if len(path)>1 else None
                if nxt:
                    port = None
                    for intf in self.switches[path[0]]['interfaces']:
                        if intf['neighbor'] == nxt: port = self._extract_port(intf['name']); break
                    nm = self.gateway[self.name_to_dpid[nxt]][1]
                    sm = self.gateway[dpid][1]
                else:
                    # same switch
                    h = self.ip_to_host.get(dst_ip)
                    port = self._extract_port(
                        next(i['name'] for i in self.switches[path[0]]['interfaces'] if i['neighbor']==h['name']))
                    nm = h['mac'].lower(); sm = self.gateway[dpid][1]
                actions = [parser.OFPActionDecNwTtl(), parser.OFPActionSetField(eth_src=sm), parser.OFPActionSetField(eth_dst=nm), parser.OFPActionOutput(port)]
                out = parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER, in_port=in_port, actions=actions, data=msg.data)
                dp.send_msg(out)
            return

    def _get_intf_mac(self, sw_name, neighbor):
        """Return the MAC address of sw_name's interface facing neighbor"""
        for intf in self.switches[sw_name]["interfaces"]:
            if intf.get("neighbor") == neighbor:
                return intf.get("mac","").lower()
        return None
