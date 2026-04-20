#!/usr/bin/env python3

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import ether_types
from ryu.topology import event
from ryu.topology.api import get_switch, get_link
import networkx as nx
import json
import random
from collections import defaultdict

class L2ShortestPathForwarding(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(L2ShortestPathForwarding, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.topology_api_app = self
        self.switches = []
        self.links = []
        self.graph = nx.Graph()
        self.host_to_switch = {}  # Maps host MAC to connected switch
        self.switch_to_port = defaultdict(dict)  # Maps switch pairs to port numbers
        self.paths = {}  # Cache for computed paths
        
        # Load configuration
        self.load_config('config.json')
        
    def load_config(self, config_file):
        """Load configuration from JSON file"""
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
                self.ecmp_enabled = config.get('ecmp', False)
                self.nodes = config.get('nodes', [])
                self.weight_matrix = config.get('weight_matrix', [])
                
                # Build graph from configuration
                self.build_graph_from_config()
                
                print(f"[CONFIG] Loaded configuration:")
                print(f"  ECMP Enabled: {self.ecmp_enabled}")
                print(f"  Nodes: {self.nodes}")
                print(f"  Graph edges: {self.graph.edges(data=True)}")
                
        except Exception as e:
            self.logger.error(f"Failed to load config: {e}")
            self.ecmp_enabled = False
            
    def build_graph_from_config(self):
        """Build weighted graph from configuration"""
        for i in range(len(self.nodes)):
            for j in range(i + 1, len(self.nodes)):
                if self.weight_matrix[i][j] > 0:
                    self.graph.add_edge(
                        self.nodes[i], 
                        self.nodes[j], 
                        weight=self.weight_matrix[i][j]
                    )
                    
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Install default flow entry to send unmatched packets to controller"""
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Install table-miss flow entry
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)
        
        print(f"[SWITCH] Switch {datapath.id} connected")

    def add_flow(self, datapath, priority, match, actions, buffer_id=None):
        """Add a flow to a switch"""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                              actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id,
                                     priority=priority, match=match,
                                     instructions=inst)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                     match=match, instructions=inst)
        datapath.send_msg(mod)
        
        print(f"[FLOW] Added flow to switch {datapath.id}: match={match}, actions={actions}")

    @set_ev_cls(event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        """Handle switch entering the network"""
        self.switches = get_switch(self, None)
        self.links = get_link(self, None)
        
        # Update switch-to-port mapping
        for link in self.links:
            src_dpid = f"s{link.src.dpid}"
            dst_dpid = f"s{link.dst.dpid}"
            self.switch_to_port[src_dpid][dst_dpid] = link.src.port_no
            self.switch_to_port[dst_dpid][src_dpid] = link.dst.port_no
            
        print(f"[TOPOLOGY] Switches: {[f's{s.dp.id}' for s in self.switches]}")
        print(f"[TOPOLOGY] Links: {[(f's{l.src.dpid}', f's{l.dst.dpid}') for l in self.links]}")
        print(f"[TOPOLOGY] Switch-to-port mapping: {dict(self.switch_to_port)}")

    def get_shortest_paths(self, src_switch, dst_switch):
        """Get shortest path(s) between two switches using Dijkstra's algorithm"""
        try:
            if self.ecmp_enabled:
                # Get all shortest paths for ECMP
                paths = list(nx.all_shortest_paths(
                    self.graph, 
                    src_switch, 
                    dst_switch, 
                    weight='weight'
                ))
                print(f"[PATH] Found {len(paths)} equal-cost paths from {src_switch} to {dst_switch}")
                return paths
            else:
                # Get single shortest path
                path = nx.shortest_path(
                    self.graph, 
                    src_switch, 
                    dst_switch, 
                    weight='weight'
                )
                print(f"[PATH] Single shortest path from {src_switch} to {dst_switch}: {path}")
                return [path]
        except nx.NetworkXNoPath:
            print(f"[PATH] No path found from {src_switch} to {dst_switch}")
            return []

    def install_path(self, path, src_mac, dst_mac, in_port, datapath):
        """Install flow rules along the computed path"""
        print(f"[INSTALL] Installing path: {path} for flow {src_mac} -> {dst_mac}")
        
        for i in range(len(path) - 1):
            curr_switch = path[i]
            next_switch = path[i + 1]
            
            # Get the switch datapath
            curr_dpid = int(curr_switch[1:])  # Extract number from 's1', 's2', etc.
            curr_dp = None
            for switch in self.switches:
                if switch.dp.id == curr_dpid:
                    curr_dp = switch.dp
                    break
                    
            if not curr_dp:
                print(f"[ERROR] Datapath not found for switch {curr_switch}")
                continue
                
            # Get output port to next switch
            out_port = self.switch_to_port[curr_switch][next_switch]
            
            # Install bidirectional flows
            parser = curr_dp.ofproto_parser
            ofproto = curr_dp.ofproto
            
            # Forward direction
            match = parser.OFPMatch(eth_src=src_mac, eth_dst=dst_mac)
            actions = [parser.OFPActionOutput(out_port)]
            self.add_flow(curr_dp, 1, match, actions)
            
            # Reverse direction (for reply traffic)
            match = parser.OFPMatch(eth_src=dst_mac, eth_dst=src_mac)
            # For reverse, get port to previous switch or use in_port for first switch
            if i == 0:
                rev_port = in_port
            else:
                prev_switch = path[i - 1]
                rev_port = self.switch_to_port[curr_switch][prev_switch]
            actions = [parser.OFPActionOutput(rev_port)]
            self.add_flow(curr_dp, 1, match, actions)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """Handle packet-in events"""
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            # Ignore LLDP packets
            return

        dst = eth.dst
        src = eth.src
        dpid = datapath.id
        
        print(f"\n[PACKET-IN] Switch s{dpid}: {src} -> {dst}, in_port={in_port}")

        # Learn MAC to port/switch mapping
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src] = in_port
        
        # Record host-to-switch mapping
        if src not in self.host_to_switch:
            self.host_to_switch[src] = f"s{dpid}"
            print(f"[LEARN] Host {src} connected to switch s{dpid}")

        # Check if we know the destination
        if dst in self.host_to_switch:
            dst_switch = self.host_to_switch[dst]
            src_switch = f"s{dpid}"
            
            if src_switch == dst_switch:
                # Same switch, use L2 learning
                if dst in self.mac_to_port[dpid]:
                    out_port = self.mac_to_port[dpid][dst]
                else:
                    out_port = ofproto.OFPP_FLOOD
                    
                actions = [parser.OFPActionOutput(out_port)]
                
                # Install flow if we know the port
                if out_port != ofproto.OFPP_FLOOD:
                    match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
                    self.add_flow(datapath, 1, match, actions, msg.buffer_id)
                    
            else:
                # Different switches, compute shortest path
                paths = self.get_shortest_paths(src_switch, dst_switch)
                
                if paths:
                    # Select path (randomly if ECMP is enabled and multiple paths exist)
                    if len(paths) > 1:
                        selected_path = random.choice(paths)
                        print(f"[ECMP] Randomly selected path: {selected_path}")
                    else:
                        selected_path = paths[0]
                    
                    # Install the path
                    self.install_path(selected_path, src, dst, in_port, datapath)
                    
                    # Output to next hop
                    if len(selected_path) > 1:
                        next_switch = selected_path[1]
                        out_port = self.switch_to_port[src_switch][next_switch]
                        actions = [parser.OFPActionOutput(out_port)]
                    else:
                        # Should not happen if src != dst switch
                        out_port = ofproto.OFPP_FLOOD
                        actions = [parser.OFPActionOutput(out_port)]
                else:
                    # No path found, flood
                    out_port = ofproto.OFPP_FLOOD
                    actions = [parser.OFPActionOutput(out_port)]
        else:
            # Unknown destination, flood
            out_port = ofproto.OFPP_FLOOD
            actions = [parser.OFPActionOutput(out_port)]
            print(f"[FLOOD] Unknown destination {dst}, flooding")

        # Send packet out
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                   in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)