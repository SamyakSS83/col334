#!/usr/bin/env python3
"""
Layer 3 Shortest Path Routing Controller for Ryu
Based on SimpleRouter with Dijkstra's algorithm for path computation
"""

import json
import heapq
import logging
from collections import defaultdict

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.lib.packet import packet, ethernet, arp, ipv4, icmp
from ryu.ofproto import ofproto_v1_3
from ryu.ofproto import ether
from ryu.ofproto import inet
from netaddr.ip import IPNetwork

LOG = logging.getLogger('ShortestPathRouter')
LOG.setLevel(logging.DEBUG)
logging.basicConfig(level=logging.DEBUG, format='%(name)s: %(levelname)s: %(message)s')


class ShortestPathRouter(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(ShortestPathRouter, self).__init__(*args, **kwargs)
        
        # Load configuration
        self.config = self.load_config('part3/p3_config.json')
        
        # Network topology graph for Dijkstra
        self.graph = {}  # {switch_name: {neighbor_name: cost}}
        
        # Switch information indexed by DPID
        self.switches = {}  # {dpid: switch_config}
        self.switch_name_to_dpid = {}  # {name: dpid}
        
        # Port information: {dpid: {neighbor_name: {port, mac, ip, subnet}}}
        self.switch_ports = {}
        
        # Host information: {host_ip: {mac, switch_name, subnet}}
        self.host_info = {}
        
        # Connected datapaths
        self.datapaths = {}  # {dpid: datapath}
        
        # ARP cache
        self.arp_table = {}  # {ip: mac}
        
        # Parse config and build graph
        self.parse_config()
        self.build_graph()
        
        LOG.info("=== Controller Initialized ===")
        LOG.info(f"Network Graph: {self.graph}")
        LOG.info(f"Switches: {list(self.switch_name_to_dpid.keys())}")
        LOG.info(f"Hosts: {list(self.host_info.keys())}")
        LOG.info("=== Port Mappings ===")
        for dpid, ports in self.switch_ports.items():
            switch_name = [n for n, d in self.switch_name_to_dpid.items() if d == dpid][0]
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
            self.switch_ports[dpid] = {}
            
            # Handle both 'interfaces' and 'intesfaces' (typo in config)
            interfaces = switch.get('interfaces', switch.get('intesfaces', []))
            
            for iface in interfaces:
                # Handle both 'neighbor' and 'neighbos' (typo in config)
                neighbor = iface.get('neighbor', iface.get('neighbos'))
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
            # Handle both 'src' and 'ssc' (typo in config)
            src = link.get('src', link.get('ssc'))
            dst = link['dst']
            cost = link['cost']
            
            # Bidirectional links
            self.graph[src][dst] = cost
            self.graph[dst][src] = cost
        
        self.graph = dict(self.graph)

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
        
        LOG.info(f"Installing flows for {src_ip} -> {dst_ip}")
        LOG.info(f"Path: {' -> '.join(path)}")
        
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
            
            LOG.info(f"Installed flow on {current_switch}: dst={dst_ip} out_port={out_port} src_mac={src_mac} dst_mac={dst_mac}")

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
        #     LOG.debug(f"Unknown packet type: {eth.ethertype}")

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
        LOG.info(f"Installed flow on switch {datapath.id}: {dst_ip} -> port {out_port}")