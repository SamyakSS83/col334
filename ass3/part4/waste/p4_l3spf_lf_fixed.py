#!/usr/bin/env python3
"""
Layer 3 Shortest Path Routing Controller with Link Failure Detection
Fixed version that properly handles topology discovery and link failures

USAGE:
    ryu-manager --observe-links ryu.app.ofctl_rest part4/p4_l3spf_lf_fixed.py
    
    The --observe-links flag is REQUIRED for link failure detection!
"""

import json
import heapq
import logging
from collections import defaultdict

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.lib.packet import packet, ethernet, arp, ipv4, icmp
from ryu.ofproto import ofproto_v1_3
from ryu.ofproto import ether
from ryu.ofproto import inet
from ryu.topology import event as topo_event

LOG = logging.getLogger('ShortestPathRouterLF')
LOG.setLevel(logging.DEBUG)
logging.basicConfig(level=logging.DEBUG, format='%(name)s: %(levelname)s: %(message)s')


class ShortestPathRouterLF(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(ShortestPathRouterLF, self).__init__(*args, **kwargs)
        
        # Load configuration
        self.config = self.load_config('part4/p4_config.json')
        
        # Network topology graph for Dijkstra
        self.graph = {}  # {switch_name: {neighbor_name: cost}}
        
        # Switch information indexed by DPID
        self.switches = {}  # {dpid: switch_config}
        self.switch_name_to_dpid = {}  # {name: dpid}
        self.dpid_to_switch_name = {}  # {dpid: name}
        
        # Port information: {dpid: {neighbor_name: {port, mac, ip, subnet}}}
        self.switch_ports = {}
        
        # Host information: {host_ip: {mac, switch_name, subnet}}
        self.host_info = {}
        
        # Connected datapaths
        self.datapaths = {}  # {dpid: datapath}
        
        # ARP cache
        self.arp_table = {}  # {ip: mac}
        
        # Topology tracking
        self.topology_links = {}  # {(src_dpid, dst_dpid): (src_port, dst_port)}
        self.link_costs = {}  # {(src_name, dst_name): cost}
        
        # Parse config and build initial graph
        self.parse_config()
        self.build_initial_graph()
        
        LOG.info("=== Controller Initialized with Link Failure Detection ===")
        LOG.info(f"Initial Graph: {self.graph}")
        LOG.info(f"Switches: {list(self.switch_name_to_dpid.keys())}")
        LOG.info(f"Hosts: {list(self.host_info.keys())}")

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

    def build_initial_graph(self):
        """Build initial weighted graph from config"""
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
        LOG.info(f"Built initial graph: {self.graph}")

    def rebuild_graph_from_topology(self):
        """Rebuild graph based on current active topology"""
        LOG.info("=== REBUILDING GRAPH FROM ACTIVE TOPOLOGY ===")
        new_graph = defaultdict(dict)
        
        # Add links that are currently active
        for (src_dpid, dst_dpid), (src_port, dst_port) in self.topology_links.items():
            src_name = self.dpid_to_switch_name.get(src_dpid)
            dst_name = self.dpid_to_switch_name.get(dst_dpid)
            
            if src_name and dst_name:
                cost = self.link_costs.get((src_name, dst_name), 10)
                new_graph[src_name][dst_name] = cost
                
        self.graph = dict(new_graph)
        LOG.info(f"Rebuilt graph: {self.graph}")

    def dijkstra(self, src_name, dst_name):
        """
        Compute shortest path using Dijkstra's algorithm
        Returns: List of switch names in the path from src to dst
        """
        if src_name == dst_name:
            return [src_name]
        
        if src_name not in self.graph:
            LOG.warning(f"Source {src_name} not in graph")
            return None
            
        if dst_name not in self.graph:
            LOG.warning(f"Destination {dst_name} not in graph")
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
        LOG.warning(f"Current graph: {self.graph}")
        return None

    @set_ev_cls(topo_event.EventSwitchEnter)
    def handler_switch_enter(self, ev):
        """Handle switch connection"""
        switch = ev.switch
        dpid = switch.dp.id
        LOG.info(f"Switch entered: DPID={dpid}")

    @set_ev_cls(topo_event.EventLinkAdd)
    def handler_link_add(self, ev):
        """Handle link addition detected by topology discovery"""
        link = ev.link
        src_dpid = link.src.dpid
        dst_dpid = link.dst.dpid
        src_port = link.src.port_no
        dst_port = link.dst.port_no
        
        LOG.info(f"*** LINK DISCOVERED: DPID {src_dpid} port {src_port} <-> DPID {dst_dpid} port {dst_port} ***")
        
        # Store the link
        self.topology_links[(src_dpid, dst_dpid)] = (src_port, dst_port)
        
        # Rebuild graph
        self.rebuild_graph_from_topology()
        
        # Reinstall all flows with new topology
        self.reinstall_all_flows()

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
        
        LOG.info(f"*** LINK FAILED: {src_name} (port {src_port}) <-> {dst_name} (port {dst_port}) ***")
        
        # Remove the link from topology
        if (src_dpid, dst_dpid) in self.topology_links:
            del self.topology_links[(src_dpid, dst_dpid)]
        
        # Rebuild graph without the failed link
        self.rebuild_graph_from_topology()
        
        # Clear all flows and reinstall with new paths
        for datapath in self.datapaths.values():
            self.clear_all_flows(datapath)
        
        # Reinstall flows
        self.reinstall_all_flows()

    def clear_all_flows(self, datapath):
        """Clear all flows from a switch except table-miss"""
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
        
        # Reinstall table-miss flow
        self.install_table_miss(datapath, datapath.id)

    def reinstall_all_flows(self):
        """Reinstall flows for all host pairs"""
        LOG.info("=== REINSTALLING ALL FLOWS WITH NEW TOPOLOGY ===")
        
        # Get all host IPs
        host_ips = list(self.host_info.keys())
        
        # Install flows for all host pairs
        for src_ip in host_ips:
            for dst_ip in host_ips:
                if src_ip != dst_ip:
                    self.install_path_flows(src_ip, dst_ip)

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
            parser = datapath.ofproto_parser
            ofproto = datapath.ofproto
            
            # Determine next hop and output port
            if i < len(path) - 1:
                # Intermediate switch or source switch - forward to next switch
                next_switch = path[i + 1]
                
                port_info = self.switch_ports[current_dpid].get(next_switch)
                if not port_info:
                    LOG.warning(f"Cannot find port info for {current_switch} -> {next_switch}")
                    continue
                
                out_port = port_info['port']
                src_mac = port_info['mac']
                
                # Get the next switch's MAC on the receiving interface
                next_dpid = self.switch_name_to_dpid[next_switch]
                dst_mac = self.switch_ports[next_dpid][current_switch]['mac']
            else:
                # Destination switch - forward to host
                host_name = None
                for host in self.config.get('hosts', []):
                    if host['ip'] == dst_ip:
                        host_name = host['name']
                        break
                
                if not host_name:
                    LOG.warning(f"Cannot find host name for {dst_ip}")
                    continue
                
                port_info = self.switch_ports[current_dpid].get(host_name)
                if not port_info:
                    LOG.warning(f"Cannot find port info for {current_switch} -> {host_name}")
                    continue
                
                out_port = port_info['port']
                src_mac = port_info['mac']
                dst_mac = self.arp_table[dst_ip]
            
            # Install flow for this direction
            match = parser.OFPMatch(
                eth_type=ether.ETH_TYPE_IP,
                ipv4_src=src_ip,
                ipv4_dst=dst_ip
            )
            
            actions = [
                parser.OFPActionSetField(eth_src=src_mac),
                parser.OFPActionSetField(eth_dst=dst_mac),
                parser.OFPActionDecNwTtl(),
                parser.OFPActionOutput(out_port)
            ]
            
            inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
            
            mod = parser.OFPFlowMod(
                datapath=datapath,
                priority=100,
                match=match,
                instructions=inst,
                idle_timeout=0,
                hard_timeout=0
            )
            
            datapath.send_msg(mod)
            LOG.debug(f"Installed flow on {current_switch}: {src_ip} -> {dst_ip} via port {out_port}")

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Handle switch connection and install table-miss flow"""
        datapath = ev.msg.datapath
        dpid = datapath.id
        
        self.datapaths[dpid] = datapath
        
        switch_name = self.dpid_to_switch_name.get(dpid, f"unknown-{dpid}")
        LOG.info(f"Switch connected: {switch_name} (DPID={dpid})")
        
        # Install table-miss flow
        self.install_table_miss(datapath, dpid)

    def install_table_miss(self, datapath, dpid):
        """Install table-miss flow to send unmatched packets to controller"""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                             actions)]
        
        mod = parser.OFPFlowMod(datapath=datapath, priority=0,
                                match=match, instructions=inst)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """Handle packets sent to controller"""
        msg = ev.msg
        datapath = msg.datapath
        in_port = msg.match['in_port']
        
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        
        if eth.ethertype == ether.ETH_TYPE_LLDP:
            # Ignore LLDP packets
            return
        
        # Handle ARP
        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt:
            self.receive_arp(datapath, pkt, eth, in_port)
            return
        
        # Handle IP
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt:
            self.receive_ip(datapath, pkt, eth, in_port, msg)
            return

    def receive_arp(self, datapath, pkt, eth, in_port):
        """Handle ARP packets"""
        arp_pkt = pkt.get_protocol(arp.arp)
        
        if arp_pkt.opcode == arp.ARP_REQUEST:
            # Learn the sender's MAC
            self.arp_table[arp_pkt.src_ip] = arp_pkt.src_mac
            
            # Check if we know the target
            if arp_pkt.dst_ip in self.arp_table:
                # Reply with ARP response
                self.reply_arp(datapath, eth, arp_pkt, arp_pkt.dst_ip, in_port)
        
        elif arp_pkt.opcode == arp.ARP_REPLY:
            # Learn the sender's MAC
            self.arp_table[arp_pkt.src_ip] = arp_pkt.src_mac

    def reply_arp(self, datapath, eth, arp_pkt, arp_dst_ip, in_port):
        """Send ARP reply"""
        dpid = datapath.id
        
        # Find the switch interface that should reply
        dst_mac = self.arp_table.get(arp_dst_ip)
        if not dst_mac:
            return
        
        # Send ARP reply
        self.send_arp(
            datapath,
            arp.ARP_REPLY,
            dst_mac,
            arp_dst_ip,
            arp_pkt.src_mac,
            arp_pkt.src_ip,
            in_port
        )

    def receive_ip(self, datapath, pkt, eth, in_port, msg=None):
        """Handle IP packets"""
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        dpid = datapath.id
        
        # Check for ICMP
        icmp_pkt = pkt.get_protocol(icmp.icmp)
        if icmp_pkt:
            self.check_icmp(datapath, eth, ip_pkt, icmp_pkt, in_port)
            return
        
        # For non-ICMP, install flows if not already installed
        src_ip = ip_pkt.src
        dst_ip = ip_pkt.dst
        
        LOG.info(f"Received IP packet: {src_ip} -> {dst_ip} on switch {dpid}")
        
        # Install bidirectional flows
        self.install_path_flows(src_ip, dst_ip)
        self.install_path_flows(dst_ip, src_ip)
        
        # Forward this packet
        if msg:
            self.forward_packet(msg, datapath, src_ip, dst_ip)

    def check_icmp(self, datapath, eth, ip_pkt, icmp_pkt, in_port):
        """Handle ICMP packets"""
        dpid = datapath.id
        
        if icmp_pkt.type == icmp.ICMP_ECHO_REQUEST:
            # Check if this is for the router interface
            dst_ip = ip_pkt.dst
            src_ip = ip_pkt.src
            
            # Check if we should reply
            switch_name = self.dpid_to_switch_name.get(dpid)
            if switch_name:
                for neighbor, port_info in self.switch_ports[dpid].items():
                    if port_info['ip'] == dst_ip:
                        # Reply to ping to router interface
                        self.reply_icmp(
                            datapath,
                            port_info['mac'],
                            eth.src,
                            dst_ip,
                            src_ip,
                            64,
                            icmp_pkt.data.id,
                            icmp_pkt.data.seq,
                            icmp_pkt.data.data,
                            in_port
                        )
                        return
            
            # Otherwise, install flows and forward
            self.install_path_flows(src_ip, ip_pkt.dst)
            self.install_path_flows(ip_pkt.dst, src_ip)

    def reply_icmp(self, datapath, src_mac, dst_mac, src_ip, dst_ip, ttl, icmp_id, seq, data, out_port):
        """Send ICMP reply"""
        self.send_icmp(
            datapath,
            src_mac,
            src_ip,
            dst_mac,
            dst_ip,
            out_port,
            seq,
            data,
            id=icmp_id,
            type=icmp.ICMP_ECHO_REPLY,
            ttl=ttl
        )

    def send_icmp(self, datapath, src_mac, src_ip, dst_mac, dst_ip, out_port, seq, data, id=1, type=icmp.ICMP_ECHO_REQUEST, ttl=64):
        """Send ICMP packet"""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(
            ethertype=ether.ETH_TYPE_IP,
            dst=dst_mac,
            src=src_mac
        ))
        pkt.add_protocol(ipv4.ipv4(
            dst=dst_ip,
            src=src_ip,
            ttl=ttl,
            proto=inet.IPPROTO_ICMP
        ))
        pkt.add_protocol(icmp.icmp(
            type_=type,
            code=icmp.ICMP_ECHO_REPLY_CODE,
            csum=0,
            data=icmp.echo(id=id, seq=seq, data=data)
        ))
        pkt.serialize()
        
        actions = [parser.OFPActionOutput(out_port)]
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER,
            actions=actions,
            data=pkt.data
        )
        datapath.send_msg(out)

    def send_arp(self, datapath, opcode, src_mac, src_ip, dst_mac, dst_ip, out_port):
        """Send ARP packet"""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(
            ethertype=ether.ETH_TYPE_ARP,
            dst=dst_mac,
            src=src_mac
        ))
        pkt.add_protocol(arp.arp(
            opcode=opcode,
            src_mac=src_mac,
            src_ip=src_ip,
            dst_mac=dst_mac,
            dst_ip=dst_ip
        ))
        pkt.serialize()
        
        actions = [parser.OFPActionOutput(out_port)]
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER,
            actions=actions,
            data=pkt.data
        )
        datapath.send_msg(out)

    def forward_packet(self, msg, datapath, src_ip, dst_ip):
        """Forward a packet based on computed path"""
        # Find source and destination switches
        src_switch_name = self.host_info.get(src_ip, {}).get('switch')
        dst_switch_name = self.host_info.get(dst_ip, {}).get('switch')
        
        if not dst_switch_name:
            return
        
        # Compute path
        current_switch_name = self.dpid_to_switch_name.get(datapath.id)
        if not current_switch_name:
            return
        
        path = self.dijkstra(current_switch_name, dst_switch_name)
        if not path or len(path) < 2:
            return
        
        # Get output port for next hop
        next_switch = path[1]
        port_info = self.switch_ports[datapath.id].get(next_switch)
        
        if not port_info:
            # Check if it's the destination host
            for host in self.config.get('hosts', []):
                if host['ip'] == dst_ip and host['switch'] == current_switch_name:
                    port_info = self.switch_ports[datapath.id].get(host['name'])
                    break
        
        if not port_info:
            return
        
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        actions = [parser.OFPActionOutput(port_info['port'])]
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=msg.match['in_port'],
            actions=actions,
            data=msg.data
        )
        datapath.send_msg(out)
