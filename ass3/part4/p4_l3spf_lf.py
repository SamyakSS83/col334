#!/usr/bin/env python3
"""
Layer 3 Shortest Path Routing Controller for Ryu with Link Failure Detection
Based on SimpleRouter with Dijkstra's algorithm for path computation
Handles dynamic link failures and recovery for Part 4
"""

import json
import heapq
import logging
import time
import os
from collections import defaultdict

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.lib.packet import packet, ethernet, arp, ipv4, icmp
from ryu.ofproto import ofproto_v1_3
from ryu.ofproto import ether
from ryu.ofproto import inet
from ryu.topology import event as topo_event
from netaddr.ip import IPNetwork

LOG = logging.getLogger('ShortestPathRouter')
LOG.setLevel(logging.DEBUG)
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s: %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)


class ShortestPathRouter(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(ShortestPathRouter, self).__init__(*args, **kwargs)
        
        # Load configuration
        self.config = self.load_config('part4/p3_config.json')
        
        # Network topology graph for Dijkstra
        self.graph = {}  # {switch_name: {neighbor_name: cost}}
        self.original_graph = {}  # Backup of initial graph from config
        
        # Switch information indexed by DPID
        self.switches = {}  # {dpid: switch_config}
        self.switch_name_to_dpid = {}  # {name: dpid}
        self.dpid_to_switch_name = {}  # Reverse mapping for events
        
        # Port information: {dpid: {neighbor_name: {port, mac, ip, subnet}}}
        self.switch_ports = {}
        
        # Host information: {host_ip: {mac, switch_name, subnet}}
        self.host_info = {}
        
        # Connected datapaths
        self.datapaths = {}  # {dpid: datapath}
        
        # ARP cache
        self.arp_table = {}  # {ip: mac}
        
        # Topology tracking for link failure detection
        self.topology_links = {}  # {(src_dpid, dst_dpid): (src_port, dst_port)}
        self.link_costs = {}  # {(src_name, dst_name): cost}
        
        # Parse config and build graph
        self.parse_config()
        self.build_graph()
        
        LOG.info("=== Controller Initialized with Link Failure Detection ===")
        LOG.info(f"Network Graph: {self.graph}")
        LOG.info(f"Switches: {list(self.switch_name_to_dpid.keys())}")
        LOG.info(f"Hosts: {list(self.host_info.keys())}")
        LOG.info("=== Port Mappings ===")
        for dpid, ports in self.switch_ports.items():
            switch_name = self.dpid_to_switch_name.get(dpid, f"dpid{dpid}")
            LOG.info(f"{switch_name} (dpid={dpid}):")
            for neighbor, info in ports.items():
                LOG.info(f"  {neighbor}: port={info['port']}, mac={info['mac']}")

    def load_config(self, config_file):
        """Load configuration from JSON file"""
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
                LOG.info(f"Loaded config from {config_file}")
                return config
        except Exception as e:
            LOG.error(f"Failed to load config: {e}")
            return {"hosts": [], "switches": [], "links": []}

    def parse_config(self):
        """Parse configuration to extract switch and host information"""
        # Parse switches
        for switch in self.config.get('switches', []):
            dpid = switch['dpid']
            name = switch['name']
            
            self.switches[dpid] = switch
            self.switch_name_to_dpid[name] = dpid
            self.dpid_to_switch_name[dpid] = name
            self.switch_ports[dpid] = {}
            
            for iface in switch['interfaces']:
                neighbor = iface['neighbor']
                port = int(iface['name'].split('-eth')[1])
                
                self.switch_ports[dpid][neighbor] = {
                    'port': port,
                    'mac': iface['mac'],
                    'ip': iface['ip'],
                    'subnet': iface['subnet']
                }
        
        # Parse hosts
        for host in self.config.get('hosts', []):
            self.host_info[host['ip']] = {
                'mac': host['mac'],
                'switch': host['switch'],
                'subnet': host['connected_subnet']
            }
            self.arp_table[host['ip']] = host['mac']

    def build_graph(self):
        """Build weighted graph from config for Dijkstra's algorithm"""
        self.graph = defaultdict(dict)
        
        for link in self.config.get('links', []):
            src = link['src']
            dst = link['dst']
            cost = link['cost']
            
            # Store link costs for later lookup
            self.link_costs[(src, dst)] = cost
            self.link_costs[(dst, src)] = cost
            
            # Bidirectional links
            self.graph[src][dst] = cost
            self.graph[dst][src] = cost
        
        self.graph = dict(self.graph)
        self.original_graph = dict(self.graph)  # Keep backup
        LOG.info(f"Built graph with {len(self.graph)} switches")

    def dijkstra(self, src_name, dst_name):
        """
        Compute shortest path using Dijkstra's algorithm
        Returns: List of switch names in the path from src to dst
        """
        if src_name == dst_name:
            return [src_name]
        
        if src_name not in self.graph or dst_name not in self.graph:
            LOG.warning(f"Cannot find path: {src_name} or {dst_name} not in graph")
            return None
        
        # Priority queue: (cost, node, path)
        pq = [(0, src_name, [src_name])]
        visited = set()
        
        while pq:
            cost, node, path = heapq.heappop(pq)
            
            if node in visited:
                continue
            
            visited.add(node)
            
            if node == dst_name:
                LOG.info(f"Shortest path from {src_name} to {dst_name}: {' -> '.join(path)} (cost={cost})")
                return path
            
            for neighbor, edge_cost in self.graph.get(node, {}).items():
                if neighbor not in visited:
                    new_cost = cost + edge_cost
                    new_path = path + [neighbor]
                    heapq.heappush(pq, (new_cost, neighbor, new_path))
        
        LOG.warning(f"No path found from {src_name} to {dst_name}")
        return None

    def install_path_flows(self, src_ip, dst_ip):
        """
        Compute shortest path and install flows on all switches in the path
        """
        # Find source and destination switches
        src_switch = self.host_info.get(src_ip, {}).get('switch')
        dst_switch = self.host_info.get(dst_ip, {}).get('switch')
        
        if not src_switch or not dst_switch:
            LOG.warning(f"Cannot find switch for {src_ip} or {dst_ip}")
            return
        
        # Compute shortest path
        path = self.dijkstra(src_switch, dst_switch)
        
        if not path:
            LOG.warning(f"No path found from {src_switch} to {dst_switch}")
            return
        
        LOG.info(f"=" * 80)
        LOG.info(f"📍 INSTALLING FLOWS: {src_ip} -> {dst_ip}")
        LOG.info(f"🛤️  PATH: {' → '.join(path)}")
        LOG.info(f"=" * 80)
        
        # Install flows on each switch in the path
        for i in range(len(path)):
            current_switch = path[i]
            current_dpid = self.switch_name_to_dpid[current_switch]
            
            if current_dpid not in self.datapaths:
                LOG.warning(f"Switch {current_switch} (dpid={current_dpid}) not connected")
                continue
            
            datapath = self.datapaths[current_dpid]
            
            # Determine next hop and output port
            if i < len(path) - 1:
                # Intermediate switch or source switch - forward to next switch
                next_switch = path[i + 1]
                LOG.debug(f"Switch {current_switch}: forwarding to next switch {next_switch}")
                
                port_info = self.switch_ports[current_dpid].get(next_switch)
                if not port_info:
                    LOG.warning(f"Cannot find port info for {current_switch} -> {next_switch}")
                    LOG.debug(f"Available neighbors for {current_switch}: {list(self.switch_ports[current_dpid].keys())}")
                    continue
                
                out_port = port_info['port']
                src_mac = port_info['mac']  # This switch's MAC on the outgoing interface
                
                # Get the next switch's MAC on the receiving interface
                next_dpid = self.switch_name_to_dpid[next_switch]
                dst_mac = self.switch_ports[next_dpid][current_switch]['mac']
                
                LOG.debug(f"  Next hop: port={out_port}, src_mac={src_mac}, dst_mac={dst_mac}")
            else:
                # Destination switch - forward to host
                LOG.debug(f"Switch {current_switch}: final hop, forwarding to host {dst_ip}")
                
                # Find the host's name to look up the port
                host_name = None
                for host in self.config.get('hosts', []):
                    if host['ip'] == dst_ip:
                        host_name = host['name']
                        break
                
                if not host_name:
                    LOG.warning(f"Cannot find host name for {dst_ip}")
                    continue
                
                LOG.debug(f"  Host name: {host_name}")
                LOG.debug(f"  Available neighbors for {current_switch}: {list(self.switch_ports[current_dpid].keys())}")
                
                port_info = self.switch_ports[current_dpid].get(host_name)
                if not port_info:
                    LOG.warning(f"Cannot find port info for {current_switch} -> {host_name}")
                    continue
                
                out_port = port_info['port']
                src_mac = port_info['mac']  # Gateway MAC
                dst_mac = self.arp_table.get(dst_ip)  # Host's MAC
                
                if not dst_mac:
                    LOG.warning(f"Cannot find MAC for {dst_ip} in ARP table")
                    LOG.debug(f"  ARP table: {self.arp_table}")
                    continue
                
                LOG.debug(f"  Final hop: port={out_port}, src_mac={src_mac}, dst_mac={dst_mac}")
            
            # Install flow rule
            self.add_flow_route(
                datapath=datapath,
                ethertype=ether.ETH_TYPE_IP,
                dst_ip=dst_ip,
                dst_mask='255.255.255.255',
                src_mac=src_mac,
                dst_mac=dst_mac,
                out_port=out_port
            )
            
            LOG.info(f"  ✅ {current_switch}: dst={dst_ip} → port {out_port} (src_mac={src_mac[-8:]}, dst_mac={dst_mac[-8:]})")

    def forward_packet(self, msg, datapath, src_ip, dst_ip):
        """
        Forward the packet that triggered flow installation by reconstructing it
        with proper MAC addresses and TTL decrement
        """
        dpid = datapath.id
        
        # Find which switch we're on
        current_switch = None
        for name, switch_dpid in self.switch_name_to_dpid.items():
            if switch_dpid == dpid:
                current_switch = name
                break
        
        if not current_switch:
            LOG.error(f"  forward_packet: Cannot find switch name for dpid={dpid}")
            return
        
        LOG.debug(f"  forward_packet: current_switch={current_switch}")
        
        # Find destination switch
        src_switch = self.host_info.get(src_ip, {}).get('switch')
        dst_switch = self.host_info.get(dst_ip, {}).get('switch')
        if not dst_switch:
            LOG.error(f"  forward_packet: Cannot find dst_switch for {dst_ip}")
            return
        
        LOG.debug(f"  forward_packet: src_switch={src_switch}, dst_switch={dst_switch}")
        
        # Get the full path from source to destination
        full_path = self.dijkstra(src_switch, dst_switch)
        if not full_path:
            LOG.error(f"  forward_packet: No path found")
            return
        
        LOG.debug(f"  forward_packet: full_path={full_path}")
        
        # Find our position in the path
        try:
            current_index = full_path.index(current_switch)
            LOG.debug(f"  forward_packet: current_index={current_index}")
        except ValueError:
            LOG.error(f"  forward_packet: {current_switch} not in path {full_path}")
            return
        
        # Determine next hop
        if current_index < len(full_path) - 1:
            # Forward to next switch
            next_switch = full_path[current_index + 1]
            LOG.debug(f"  forward_packet: forwarding to next switch {next_switch}")
            
            port_info = self.switch_ports[dpid].get(next_switch)
            if not port_info:
                LOG.error(f"  forward_packet: Cannot find port to {next_switch}")
                LOG.debug(f"    Available: {list(self.switch_ports[dpid].keys())}")
                return
            
            out_port = port_info['port']
            src_mac = port_info['mac']
            
            # Get next switch's receiving MAC
            next_dpid = self.switch_name_to_dpid[next_switch]
            dst_mac = self.switch_ports[next_dpid][current_switch]['mac']
            
            LOG.debug(f"  forward_packet: out_port={out_port}, src_mac={src_mac}, dst_mac={dst_mac}")
        else:
            # We're at destination switch - forward to host
            LOG.debug(f"  forward_packet: at destination switch, forwarding to host {dst_ip}")
            
            host_name = None
            for host in self.config.get('hosts', []):
                if host['ip'] == dst_ip:
                    host_name = host['name']
                    break
            
            if not host_name:
                LOG.error(f"  forward_packet: Cannot find host name for {dst_ip}")
                return
            
            LOG.debug(f"  forward_packet: host_name={host_name}")
            LOG.debug(f"    Available neighbors: {list(self.switch_ports[dpid].keys())}")
            
            port_info = self.switch_ports[dpid].get(host_name)
            if not port_info:
                LOG.error(f"  forward_packet: Cannot find port to {host_name}")
                return
            
            out_port = port_info['port']
            src_mac = port_info['mac']
            dst_mac = self.arp_table.get(dst_ip)
            
            if not dst_mac:
                LOG.error(f"  forward_packet: Cannot find MAC for {dst_ip}")
                return
            
            LOG.debug(f"  forward_packet: out_port={out_port}, src_mac={src_mac}, dst_mac={dst_mac}")
        
        # Reconstruct and forward packet with modified headers
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        
        # Create new packet with modified headers
        new_pkt = packet.Packet()
        new_pkt.add_protocol(ethernet.ethernet(
            dst=dst_mac,
            src=src_mac,
            ethertype=ether.ETH_TYPE_IP
        ))
        new_pkt.add_protocol(ipv4.ipv4(
            version=ip_pkt.version,
            header_length=ip_pkt.header_length,
            tos=ip_pkt.tos,
            total_length=0,
            identification=ip_pkt.identification,
            flags=ip_pkt.flags,
            offset=ip_pkt.offset,
            ttl=ip_pkt.ttl - 1,  # Decrement TTL
            proto=ip_pkt.proto,
            csum=0,
            src=ip_pkt.src,
            dst=ip_pkt.dst
        ))
        
        # Copy ICMP or other payload
        if ip_pkt.proto == inet.IPPROTO_ICMP:
            icmp_pkt = pkt.get_protocol(icmp.icmp)
            new_pkt.add_protocol(icmp_pkt)
        
        new_pkt.serialize()
        
        # Send packet out
        parser = datapath.ofproto_parser
        actions = [parser.OFPActionOutput(out_port)]
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=datapath.ofproto.OFP_NO_BUFFER,
            in_port=msg.match['in_port'],
            actions=actions,
            data=new_pkt.data
        )
        datapath.send_msg(out)
        LOG.info(f"  Forwarded packet from {src_ip} to {dst_ip} via {current_switch} port {out_port} (TTL={ip_pkt.ttl - 1})")

    # @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    # def switch_features_handler(self, ev):
    #     """Handle switch connection"""
    #     msg = ev.msg
    #     datapath = msg.datapath
    #     dpid = datapath.id
        
    #     self.datapaths[dpid] = datapath
    #     LOG.info(f"Switch connected: DPID={dpid}")
        
    #     # Install table-miss flow entry
    #     self.install_table_miss(datapath, dpid)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Handle switch connection"""
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        
        self.datapaths[dpid] = datapath
        LOG.info(f"Switch connected: DPID={dpid}")
        
        # Install table-miss flow entry
        self.install_table_miss(datapath, dpid)
        
        # Install LLDP forwarding flow (critical for topology discovery)
        # self.install_lldp_flow(datapath)

    def install_table_miss(self, datapath, dpid):
        """Install table-miss flow entry (send to controller)"""
        match = datapath.ofproto_parser.OFPMatch()
        actions = [datapath.ofproto_parser.OFPActionOutput(
            datapath.ofproto.OFPP_CONTROLLER,
            datapath.ofproto.OFPCML_NO_BUFFER)]
        inst = [datapath.ofproto_parser.OFPInstructionActions(
            datapath.ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = datapath.ofproto_parser.OFPFlowMod(
            datapath=datapath,
            priority=0,
            buffer_id=0xffffffff,
            match=match,
            instructions=inst)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        """Handle port status changes (link up/down) for fast failure detection"""
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        
        reason = msg.reason
        port_no = msg.desc.port_no
        
        dpid = datapath.id
        switch_name = self.dpid_to_switch_name.get(dpid, f"DPID{dpid}")
        
        if reason == ofproto.OFPPR_ADD:
            LOG.info(f"PORT UP: {switch_name} port {port_no}")
        elif reason == ofproto.OFPPR_DELETE:
            LOG.warning(f"PORT DOWN: {switch_name} port {port_no}")
        elif reason == ofproto.OFPPR_MODIFY:
            # Check if link went down or up
            if msg.desc.state & ofproto.OFPPS_LINK_DOWN:
                LOG.warning(f"⚠️  PORT LINK DOWN: {switch_name} port {port_no} - Link failure detected!")
            else:
                LOG.info(f"✅ PORT LINK UP: {switch_name} port {port_no} - Link restored!")

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """Handle packets sent to controller"""
        msg = ev.msg
        datapath = msg.datapath
        in_port = msg.match['in_port']
        
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        
        if eth.ethertype == ether.ETH_TYPE_ARP:
            self.receive_arp(datapath, pkt, eth, in_port)
        elif eth.ethertype == ether.ETH_TYPE_IP:
            self.receive_ip(datapath, pkt, eth, in_port, msg)
        # else:
            # LOG.debug(f"Unknown packet type: {eth.ethertype}")

    def receive_arp(self, datapath, pkt, eth, in_port):
        """Handle ARP packets"""
        arp_pkt = pkt.get_protocol(arp.arp)
        
        if arp_pkt.opcode == arp.ARP_REQUEST:
            LOG.info(f"ARP Request: Who has {arp_pkt.dst_ip}? Tell {arp_pkt.src_ip} (port {in_port})")
            
            # Update ARP table
            self.arp_table[arp_pkt.src_ip] = arp_pkt.src_mac
            
            # Reply if we have this IP on our interface
            self.reply_arp(datapath, eth, arp_pkt, arp_pkt.dst_ip, in_port)
            
        elif arp_pkt.opcode == arp.ARP_REPLY:
            LOG.info(f"ARP Reply: {arp_pkt.src_ip} is at {arp_pkt.src_mac}")
            self.arp_table[arp_pkt.src_ip] = arp_pkt.src_mac

    def reply_arp(self, datapath, eth, arp_pkt, arp_dst_ip, in_port):
        """Send ARP reply if we own the requested IP"""
        dpid = datapath.id
        
        # Check if the requested IP belongs to one of our interfaces
        src_mac = None
        out_port = None
        
        for neighbor, port_info in self.switch_ports.get(dpid, {}).items():
            if arp_dst_ip == port_info['ip']:
                src_mac = port_info['mac']
                out_port = in_port  # Reply back on the same port
                break
        
        if src_mac:
            self.send_arp(
                datapath=datapath,
                opcode=arp.ARP_REPLY,
                src_mac=src_mac,
                src_ip=arp_pkt.dst_ip,
                dst_mac=eth.src,
                dst_ip=arp_pkt.src_ip,
                out_port=out_port
            )
            LOG.info(f"Sent ARP Reply: {arp_pkt.dst_ip} is at {src_mac}")

    def receive_ip(self, datapath, pkt, eth, in_port, msg=None):
        """Handle IP packets"""
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        
        # Ignore multicast/broadcast packets (224.0.0.0 and above)
        try:
            dst_octets = [int(x) for x in ip_pkt.dst.split('.')]
            if dst_octets[0] >= 224:  # Multicast/broadcast range
                return
        except:
            return
        
        dpid = datapath.id
        switch_name = None
        for name, sid in self.switch_name_to_dpid.items():
            if sid == dpid:
                switch_name = name
                break
        
        LOG.info(f"[{switch_name}/dpid={dpid}/port={in_port}] IP packet: {ip_pkt.src} -> {ip_pkt.dst} (TTL={ip_pkt.ttl})")
        LOG.debug(f"  Ethernet: src={eth.src}, dst={eth.dst}")
        
        # Check TTL
        if ip_pkt.ttl <= 1:
            LOG.warning(f"TTL expired, dropping packet from {ip_pkt.src}")
            # Should send ICMP Time Exceeded, but we'll just drop for simplicity
            return
        
        # Handle ICMP
        if ip_pkt.proto == inet.IPPROTO_ICMP:
            icmp_pkt = pkt.get_protocol(icmp.icmp)
            self.check_icmp(datapath, eth, ip_pkt, icmp_pkt, in_port)
        
        # Only install flows for packets between actual hosts
        if ip_pkt.src in self.host_info and ip_pkt.dst in self.host_info:
            LOG.debug(f"  Both src and dst are hosts, installing flows")
            # Install bidirectional flows
            self.install_path_flows(ip_pkt.src, ip_pkt.dst)
            self.install_path_flows(ip_pkt.dst, ip_pkt.src)
            
            # Forward this packet along the path
            if msg:
                LOG.debug(f"  Forwarding triggering packet")
                self.forward_packet(msg, datapath, ip_pkt.src, ip_pkt.dst)
        else:
            LOG.debug(f"  Ignoring: src in hosts={ip_pkt.src in self.host_info}, dst in hosts={ip_pkt.dst in self.host_info}")

    def check_icmp(self, datapath, eth, ip_pkt, icmp_pkt, in_port):
        """Handle ICMP packets"""
        if icmp_pkt.type == icmp.ICMP_ECHO_REQUEST:
            LOG.info(f"ICMP Echo Request: {ip_pkt.src} -> {ip_pkt.dst}")
            
            # Check if this ping is for us (one of our switch IPs)
            dpid = datapath.id
            for neighbor, port_info in self.switch_ports.get(dpid, {}).items():
                if ip_pkt.dst == port_info['ip']:
                    # Reply to ping
                    self.reply_icmp(
                        datapath=datapath,
                        src_mac=eth.dst,
                        dst_mac=eth.src,
                        src_ip=ip_pkt.dst,
                        dst_ip=ip_pkt.src,
                        ttl=64,
                        icmp_id=icmp_pkt.data.id,
                        seq=icmp_pkt.data.seq,
                        data=icmp_pkt.data.data,
                        out_port=in_port
                    )
                    LOG.info(f"Sent ICMP Echo Reply")
                    return
        
        elif icmp_pkt.type == icmp.ICMP_ECHO_REPLY:
            LOG.info(f"ICMP Echo Reply: {ip_pkt.src} -> {ip_pkt.dst}")

    def reply_icmp(self, datapath, src_mac, dst_mac, src_ip, dst_ip, ttl, icmp_id, seq, data, out_port):
        """Send ICMP Echo Reply"""
        self.send_icmp(
            datapath=datapath,
            src_mac=src_mac,
            src_ip=src_ip,
            dst_mac=dst_mac,
            dst_ip=dst_ip,
            out_port=out_port,
            seq=seq,
            data=data,
            id=icmp_id,
            type=icmp.ICMP_ECHO_REPLY,
            ttl=ttl
        )

    def send_icmp(self, datapath, src_mac, src_ip, dst_mac, dst_ip, out_port, seq, data, id=1, type=icmp.ICMP_ECHO_REQUEST, ttl=64):
        """Construct and send ICMP packet"""
        e = ethernet.ethernet(dst_mac, src_mac, ether.ETH_TYPE_IP)
        iph = ipv4.ipv4(version=4, header_length=5, tos=0, total_length=0,
                        identification=0, flags=2, offset=0, ttl=ttl,
                        proto=inet.IPPROTO_ICMP, csum=0, src=src_ip, dst=dst_ip)
        echo = icmp.echo(id=id, seq=seq, data=data)
        icmph = icmp.icmp(type_=type, code=0, csum=0, data=echo)
        
        p = packet.Packet()
        p.add_protocol(e)
        p.add_protocol(iph)
        p.add_protocol(icmph)
        p.serialize()
        
        actions = [datapath.ofproto_parser.OFPActionOutput(out_port, 0)]
        out = datapath.ofproto_parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=0xffffffff,
            in_port=datapath.ofproto.OFPP_CONTROLLER,
            actions=actions,
            data=p.data)
        datapath.send_msg(out)

    def send_arp(self, datapath, opcode, src_mac, src_ip, dst_mac, dst_ip, out_port):
        """Construct and send ARP packet"""
        if opcode == arp.ARP_REQUEST:
            target_mac = "00:00:00:00:00:00"
        else:
            target_mac = dst_mac
        
        e = ethernet.ethernet(dst_mac, src_mac, ether.ETH_TYPE_ARP)
        a = arp.arp(hwtype=1, proto=ether.ETH_TYPE_IP, hlen=6, plen=4,
                    opcode=opcode, src_mac=src_mac, src_ip=src_ip,
                    dst_mac=target_mac, dst_ip=dst_ip)
        
        p = packet.Packet()
        p.add_protocol(e)
        p.add_protocol(a)
        p.serialize()
        
        actions = [datapath.ofproto_parser.OFPActionOutput(out_port, 0)]
        out = datapath.ofproto_parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=0xffffffff,
            in_port=datapath.ofproto.OFPP_CONTROLLER,
            actions=actions,
            data=p.data)
        datapath.send_msg(out)

    def add_flow_route(self, datapath, ethertype, dst_ip, dst_mask, src_mac, dst_mac, out_port):
        """
        Install flow rule for IP routing with MAC rewriting and TTL decrement
        """
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        
        match = parser.OFPMatch(
            eth_type=ethertype,
            ipv4_dst=(dst_ip, dst_mask)
        )
        
        actions = [
            parser.OFPActionDecNwTtl(),  # Decrement TTL
            parser.OFPActionSetField(eth_src=src_mac),
            parser.OFPActionSetField(eth_dst=dst_mac),
            parser.OFPActionOutput(out_port, 0)
        ]
        
        inst = [parser.OFPInstructionActions(
            ofproto.OFPIT_APPLY_ACTIONS, actions)]
        
        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=100,
            match=match,
            instructions=inst,
            idle_timeout=0,  # No timeout
            hard_timeout=0   # No timeout
        )
        
        datapath.send_msg(mod)
        
        # Log flow table change with timestamp for convergence analysis
        switch_name = self.dpid_to_switch_name.get(datapath.id, f"dpid{datapath.id}")
        LOG.info(f"[FLOW_CHANGE] t={time.time():.3f} switch={switch_name} dst={dst_ip}/{dst_mask} port={out_port} src_mac={src_mac} dst_mac={dst_mac}")

    # ===== LINK FAILURE DETECTION HANDLERS =====
    
    @set_ev_cls(topo_event.EventLinkAdd)
    def handler_link_add(self, ev):
        """Handle link addition/recovery detected by topology discovery"""
        link = ev.link
        src_dpid = link.src.dpid
        dst_dpid = link.dst.dpid
        src_port = link.src.port_no
        dst_port = link.dst.port_no
        
        src_name = self.dpid_to_switch_name.get(src_dpid)
        dst_name = self.dpid_to_switch_name.get(dst_dpid)
        
        if not src_name or not dst_name:
            return
        
        # Check if this is a new link
        is_new_link = (src_dpid, dst_dpid) not in self.topology_links
        
        if is_new_link:
            LOG.info("=" * 80)
            LOG.info(f"✅ LINK DISCOVERED/RESTORED: {src_name}:{src_port} ↔ {dst_name}:{dst_port}")
            LOG.info("=" * 80)
        
        # Store the link
        self.topology_links[(src_dpid, dst_dpid)] = (src_port, dst_port)
        
        # Rebuild graph from active topology
        old_graph = dict(self.graph)
        self.rebuild_graph_from_topology()
        
        # Only reinstall flows if this is a new link that changes the graph
        if is_new_link and old_graph != self.graph:
            LOG.info("New link added - clearing flows and recomputing paths...")
            
            # Clear flows from all switches
            for datapath in self.datapaths.values():
                self.clear_all_flows(datapath)
            
            # Add small delay to ensure flow deletion completes
            import time
            time.sleep(0.1)
            
            # Now reinstall flows
            self.reinstall_all_flows()
        else:
            LOG.debug("Graph unchanged - no flow updates needed")

    # @set_ev_cls(topo_event.EventLinkDelete)
    # def handler_link_delete(self, ev):
    #     """Handle link deletion (failure)"""
    #     link = ev.link
    #     src_dpid = link.src.dpid
    #     dst_dpid = link.dst.dpid
    #     src_port = link.src.port_no
    #     dst_port = link.dst.port_no
        
    #     src_name = self.dpid_to_switch_name.get(src_dpid, f"DPID{src_dpid}")
    #     dst_name = self.dpid_to_switch_name.get(dst_dpid, f"DPID{dst_dpid}")
        
    #     LOG.info(f"*** LINK FAILED: {src_name} (port {src_port}) <-> {dst_name} (port {dst_port}) ***")
        
    #     # Remove the link from topology
    #     if (src_dpid, dst_dpid) in self.topology_links:
    #         del self.topology_links[(src_dpid, dst_dpid)]
        
    #     # Rebuild graph without the failed link
    #     old_graph = dict(self.graph)
    #     self.rebuild_graph_from_topology()
        
    #     # Only clear and reinstall flows if graph actually changed
    #     if old_graph != self.graph:
    #         LOG.info("Topology changed - clearing flows and computing new paths...")
    #         for datapath in self.datapaths.values():
    #             self.clear_all_flows(datapath)
            
    #         # Reinstall flows with new paths
    #         self.reinstall_all_flows()
    #     else:
    #         LOG.info("Graph unchanged - no flow updates needed")

    # def rebuild_graph_from_topology(self):
    #     """Rebuild graph based on currently active topology links"""
    #     LOG.info("=== REBUILDING GRAPH FROM ACTIVE TOPOLOGY ===")
    #     new_graph = defaultdict(dict)
        
    #     # Add links that are currently active
    #     for (src_dpid, dst_dpid), (src_port, dst_port) in self.topology_links.items():
    #         src_name = self.dpid_to_switch_name.get(src_dpid)
    #         dst_name = self.dpid_to_switch_name.get(dst_dpid)
            
    #         if src_name and dst_name:
    #             cost = self.link_costs.get((src_name, dst_name), 10)
    #             new_graph[src_name][dst_name] = cost
        
    #     self.graph = dict(new_graph)
    #     LOG.info(f"Rebuilt graph: {self.graph}")

    def install_lldp_flow(self, datapath):
        """
        Install flow to forward LLDP packets to controller
        This must survive flow table clears to maintain topology discovery
        """
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        
        # Match LLDP packets (ethertype 0x88cc)
        match = parser.OFPMatch(eth_type=0x88cc)
        
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                        ofproto.OFPCML_NO_BUFFER)]
        
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        
        # High priority to ensure LLDP is always forwarded
        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=65535,  # Highest priority
            match=match,
            instructions=inst,
            idle_timeout=0,
            hard_timeout=0
        )
        
        datapath.send_msg(mod)
        LOG.debug(f"Installed LLDP forwarding flow on switch {datapath.id}")

    @set_ev_cls(topo_event.EventLinkDelete)
    def handler_link_delete(self, ev):
        """Handle link deletion (failure)"""
        link = ev.link
        src_dpid = link.src.dpid
        dst_dpid = link.dst.dpid
        src_port = link.src.port_no
        dst_port = link.dst.port_no
        
        src_name = self.dpid_to_switch_name.get(src_dpid, f"DPID{src_dpid}")
        dst_name = self.dpid_to_switch_name.get(dst_dpid, f"DPID{dst_dpid}")
        
        LOG.warning("=" * 80)
        LOG.warning(f"❌ LINK FAILURE DETECTED: {src_name}:{src_port} ↔ {dst_name}:{dst_port}")
        LOG.warning("=" * 80)
        
        # Only log once per physical link
        if (src_dpid, dst_dpid) in self.topology_links:
            LOG.info(f"*** LINK FAILED: {src_name} (port {src_port}) <-> {dst_name} (port {dst_port}) ***")
            
            # Remove both directions from topology
            if (src_dpid, dst_dpid) in self.topology_links:
                del self.topology_links[(src_dpid, dst_dpid)]
            if (dst_dpid, src_dpid) in self.topology_links:
                del self.topology_links[(dst_dpid, src_dpid)]
            
            # Rebuild graph without the failed link
            old_graph = dict(self.graph)
            self.rebuild_graph_from_topology()
            
            # Only clear and reinstall flows if graph actually changed
            if old_graph != self.graph:
                LOG.warning("⚠️  Topology changed - clearing all flows and computing new paths...")
                
                # Optional: Add artificial delay to simulate slower convergence (for testing)
                # Set environment variable CONVERGENCE_DELAY_MS to add delay (e.g., CONVERGENCE_DELAY_MS=500)
                delay_ms = int(os.environ.get('CONVERGENCE_DELAY_MS', '0'))
                if delay_ms > 0:
                    LOG.warning(f"⏱️  Simulating convergence delay: {delay_ms}ms")
                    time.sleep(delay_ms / 1000.0)
                
                for datapath in self.datapaths.values():
                    self.clear_all_flows(datapath)
                
                # Reinstall flows with new paths
                self.reinstall_all_flows()
            else:
                LOG.info("Graph unchanged - no flow updates needed")
        else:
            LOG.warning(f"    Already removed from topology, ignoring duplicate event")
    
    def rebuild_graph_from_topology(self):
        """Rebuild graph based on currently active topology links"""
        LOG.info("=== REBUILDING GRAPH FROM ACTIVE TOPOLOGY ===")
        new_graph = defaultdict(dict)
        
        # Track which bidirectional links we've already processed
        processed_pairs = set()
        
        # Add links that are currently active - ensure bidirectionality
        for (src_dpid, dst_dpid), (src_port, dst_port) in self.topology_links.items():
            src_name = self.dpid_to_switch_name.get(src_dpid)
            dst_name = self.dpid_to_switch_name.get(dst_dpid)
            
            if src_name and dst_name:
                # Create a canonical pair representation (sorted) to avoid duplicates
                pair = tuple(sorted([src_name, dst_name]))
                
                if pair not in processed_pairs:
                    processed_pairs.add(pair)
                    
                    # Get the cost from original config
                    cost = self.link_costs.get((src_name, dst_name), 10)
                    
                    # CRITICAL: Add BOTH directions for bidirectional link
                    new_graph[src_name][dst_name] = cost
                    new_graph[dst_name][src_name] = cost
                    
                    LOG.debug(f"Added bidirectional link: {src_name} <-> {dst_name} (cost={cost})")
        
        self.graph = dict(new_graph)
        LOG.info(f"Rebuilt graph: {self.graph}")

    # def clear_all_flows(self, datapath):
    #     """Clear all flows from a switch"""
    #     ofproto = datapath.ofproto
    #     parser = datapath.ofproto_parser
        
    #     match = parser.OFPMatch()
    #     mod = parser.OFPFlowMod(
    #         datapath=datapath,
    #         command=ofproto.OFPFC_DELETE,
    #         out_port=ofproto.OFPP_ANY,
    #         out_group=ofproto.OFPG_ANY,
    #         match=match
    #     )
    #     datapath.send_msg(mod)
        #     LOG.debug(f"Cleared all flows from switch {datapath.id}")
    def clear_all_flows(self, datapath):
        """Clear all flows from a switch, then reinstall critical flows"""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        # Delete all flows
        match = parser.OFPMatch()
        mod = parser.OFPFlowMod(
            datapath=datapath,
            command=ofproto.OFPFC_DELETE,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            match=match
        )
        datapath.send_msg(mod)
        
        # Send a barrier request to ensure deletion completes
        barrier_req = parser.OFPBarrierRequest(datapath)
        datapath.send_msg(barrier_req)
        
        LOG.debug(f"Cleared all flows from switch {datapath.id}")
        
        # CRITICAL: Reinstall table-miss and LLDP flows immediately
        self.install_table_miss(datapath, datapath.id)
        # self.install_lldp_flow(datapath)

    def reinstall_all_flows(self):
        """Reinstall flows for all host pairs after topology change"""
        LOG.info("=" * 80)
        LOG.info("🔄 REINSTALLING ALL FLOWS WITH NEW TOPOLOGY")
        LOG.info("=" * 80)
        
        # Get all host IPs
        host_ips = list(self.host_info.keys())
        
        # Install flows for all host pairs
        flow_count = 0
        for src_ip in host_ips:
            for dst_ip in host_ips:
                if src_ip != dst_ip:
                    self.install_path_flows(src_ip, dst_ip)
                    flow_count += 1
        
        LOG.info("=" * 80)
        LOG.info(f"✅ Reinstalled {flow_count} flow paths")
        LOG.info("=" * 80)