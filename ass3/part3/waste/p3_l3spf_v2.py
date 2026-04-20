#!/usr/bin/env python3
"""
Ryu app: L3-like Shortest Path Forwarding (reads config.json — no hardcoding)

Requirements:
 - config.json in same folder with the format you provided.
 - networkx installed (used for Dijkstra on weighted graph).
 - Runs with OpenFlow 1.3 (ryu + ovs supporting OF13).
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


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "p3_config.json")


def _extract_port_from_ifname(ifname):
    # Expect names like "r1-eth2" -> return 2 (int)
    if "eth" in ifname:
        try:
            return int(ifname.split("eth")[1])
        except Exception:
            pass
    return None


class P3L3SPF(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(P3L3SPF, self).__init__(*args, **kwargs)

        # Load config
        with open(CONFIG_PATH, "r") as f:
            self.cfg = json.load(f)

        # Maps and state
        # switch name -> switch dict (from config)
        self.switches = {sw["name"]: sw for sw in self.cfg.get("switches", [])}
        # dpid (int) -> switch name
        self.dpid_to_name = {sw["dpid"]: sw["name"] for sw in self.cfg.get("switches", [])}
        # switch name -> dpid
        self.name_to_dpid = {sw["name"]: sw["dpid"] for sw in self.cfg.get("switches", [])}

        # Hosts: name -> host dict (from config)
        self.hosts = {h["name"]: h for h in self.cfg.get("hosts", [])}
        # host ip -> host dict
        self.ip_to_host = {h["ip"]: h for h in self.cfg.get("hosts", [])}
        # host mac -> host dict
        self.mac_to_host = {h["mac"].lower(): h for h in self.cfg.get("hosts", [])}

        # gateway_mac[dpid] = mac of host-facing interface (first interface whose neighbor startswith 'h')
        self.gateway_mac = {}
        # host_port_on_switch[dpid] = port number where the host is connected (from interface name)
        self.host_port_on_switch = {}

        # adjacency: dpid -> { neighbor_dpid: port_no (on dpid) }
        self.adjacency = {}

        # networkx graph for shortest path with weights from config 'links'
        self.G = nx.DiGraph()

        # OpenFlow datapaths
        self.datapaths = {}  # dpid -> datapath

        self._prepare_from_config()
        self.logger.info("Config parsed: %d switches, %d hosts, %d links",
                         len(self.switches), len(self.hosts), self.G.number_of_edges())

    def _prepare_from_config(self):
        # build adjacency (from interface names) and fill gateway_mac & host ports
        # First: process interfaces to extract port numbers and neighbor names
        # store: iface_map[sw_name][neighbor_name] = (port_no, mac, ip, subnet)
        iface_map = {}
        for sw_name, sw in self.switches.items():
            iface_map[sw_name] = {}
            for intf in sw.get("interfaces", []):
                neigh = intf["neighbor"]
                ifname = intf.get("name", "")
                port = _extract_port_from_ifname(ifname)
                iface_map[sw_name][neigh] = {
                    "port": port,
                    "mac": intf.get("mac"),
                    "ip": intf.get("ip"),
                    "subnet": intf.get("subnet"),
                    "ifname": ifname
                }
                # If neighbor is a host, record gateway mac and host port
                if neigh.startswith("h"):
                    dpid = sw["dpid"]
                    self.gateway_mac[dpid] = intf.get("mac").lower()
                    # host port on that switch
                    self.host_port_on_switch[dpid] = port

        # Next: build adjacency between routers with ports
        # For every switch s and neighbor r, if neighbor is a router, find the reciprocal port
        for sw_name, sw in self.switches.items():
            dpid = sw["dpid"]
            self.adjacency.setdefault(dpid, {})
            for intf in sw.get("interfaces", []):
                neigh = intf["neighbor"]
                if not neigh.startswith("r"):
                    continue
                # neighbor is router name
                port_local = _extract_port_from_ifname(intf.get("name", ""))
                # find neighbor port where neighbor has an interface pointing to sw_name
                neigh_sw = self.switches.get(neigh)
                neighbor_port = None
                if neigh_sw is not None:
                    for nintf in neigh_sw.get("interfaces", []):
                        if nintf.get("neighbor") == sw_name:
                            neighbor_port = _extract_port_from_ifname(nintf.get("name", ""))
                            break
                # store adjacency using dpids
                if neighbor_port is None:
                    self.logger.warning("Could not find reciprocal port for link %s <-> %s", sw_name, neigh)
                self.adjacency[dpid][self.name_to_dpid[neigh]] = port_local

        # Build graph from config 'links' (weighted undirected)
        for link in self.cfg.get("links", []):
            s = link["src"]
            d = link["dst"]
            w = link.get("cost", 1)
            # ensure both directions with same weight
            self.G.add_edge(s, d, weight=w)
            self.G.add_edge(d, s, weight=w)

    # -----------------------
    # OpenFlow: switch connected
    # -----------------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        dpid = dp.id
        self.datapaths[dpid] = dp
        parser = dp.ofproto_parser
        ofp = dp.ofproto

        self.logger.info("Switch connected: dpid=%s name=%s", dpid, self.dpid_to_name.get(dpid))

        # table-miss -> send to controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self._add_flow(dp, priority=0, match=match, actions=actions)

        # Drop LLDP
        match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_LLDP)
        self._add_flow(dp, priority=10000, match=match, actions=[])

        # Drop IP packets with TTL == 1 (so they don't get forwarded then wrap to 0)
        # Drop IP packets with TTL == 1 (so they don't get forwarded then wrap to 0)
        # Not all Ryu/ofproto versions expose an OXM field named 'ip_ttl'.
        # Try to install the match if available, otherwise skip with a warning.
        try:
            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_ttl=1)
            self._add_flow(dp, priority=9000, match=match, actions=[])
        except Exception as e:
            # Avoid crashing controller if the OXM field is missing (KeyError previously)
            self.logger.warning("Could not install TTL==1 drop rule on s%s: %s", dpid, e)

        # For each local subnet on this switch, send to controller so ARP / host local traffic is handled.
        sw_name = self.dpid_to_name.get(dpid)
        if sw_name:
            sw = self.switches.get(sw_name, {})
            for intf in sw.get("interfaces", []):
                if "subnet" in intf and intf.get("neighbor", "").startswith("h"):
                    # match packets destined to this connected subnet -> send to controller (priority > 0)
                    subnet = intf["subnet"]
                    net = ipaddress.ip_network(subnet)
                    # OFPMatch supports ipv4_dst as (addr, mask)
                    match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                            ipv4_dst=(str(net.network_address), str(net.netmask)))
                    actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER)]
                    self._add_flow(dp, priority=2000, match=match, actions=actions)

    def _add_flow(self, datapath, priority, match, actions, buffer_id=None):
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id,
                                    priority=priority, match=match, instructions=inst)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority, match=match, instructions=inst)
        datapath.send_msg(mod)

    # -----------------------
    # Packet-in handler
    # -----------------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth_pkt = pkt.get_protocol(ethernet.ethernet)
        if eth_pkt is None:
            return

        # ignore LLDP
        if eth_pkt.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        # ARP handling (controller acts as router for connected subnets)
        if eth_pkt.ethertype == ether_types.ETH_TYPE_ARP:
            a = pkt.get_protocol(arp.arp)
            if a.opcode == arp.ARP_REQUEST:
                target_ip = a.dst_ip
                # If target_ip is one of router-interface IPs or a host IP known in config, reply using appropriate MAC
                # Find which switch/interface owns that IP (could be host-facing interface or inter-switch link interface)
                replied = False
                # Check hosts first
                for h in self.hosts.values():
                    if h["ip"] == target_ip:
                        # reply with gateway MAC of the switch to which the host is attached
                        sw_name = h["switch"]
                        dpid_sw = self.name_to_dpid[sw_name]
                        gwmac = self.gateway_mac.get(dpid_sw)
                        if gwmac:
                            # send ARP reply: src_mac = gwmac (router's interface MAC facing host)
                            pkt_out = packet.Packet()
                            pkt_out.add_protocol(ethernet.ethernet(
                                ethertype=ether_types.ETH_TYPE_ARP,
                                dst=eth_pkt.src,
                                src=gwmac
                            ))
                            pkt_out.add_protocol(arp.arp(
                                opcode=arp.ARP_REPLY,
                                src_mac=gwmac,
                                src_ip=target_ip,
                                dst_mac=a.src_mac,
                                dst_ip=a.src_ip
                            ))
                            pkt_out.serialize()
                            actions = [parser.OFPActionOutput(in_port)]
                            out = parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                                      in_port=ofp.OFPP_CONTROLLER,
                                                      actions=actions, data=pkt_out.data)
                            dp.send_msg(out)
                            self.logger.info("[ARP] Replied for host IP %s with gwmac %s on s%d", target_ip, gwmac, dpid)
                            replied = True
                            break
                if replied:
                    return

                # Otherwise check router interface IPs on config (inter-switch links)
                for sw in self.switches.values():
                    for intf in sw.get("interfaces", []):
                        if intf.get("ip") == target_ip:
                            # reply with this interface MAC
                            src_mac = intf.get("mac")
                            pkt_out = packet.Packet()
                            pkt_out.add_protocol(ethernet.ethernet(
                                ethertype=ether_types.ETH_TYPE_ARP,
                                dst=eth_pkt.src,
                                src=src_mac
                            ))
                            pkt_out.add_protocol(arp.arp(
                                opcode=arp.ARP_REPLY,
                                src_mac=src_mac,
                                src_ip=target_ip,
                                dst_mac=a.src_mac,
                                dst_ip=a.src_ip
                            ))
                            pkt_out.serialize()
                            actions = [parser.OFPActionOutput(in_port)]
                            out = parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                                      in_port=ofp.OFPP_CONTROLLER,
                                                      actions=actions, data=pkt_out.data)
                            dp.send_msg(out)
                            self.logger.info("[ARP] Replied for router-if IP %s with %s on s%d", target_ip, src_mac, dpid)
                            return

                # else: let ARP drop / or you can implement proxy ARP to forward
                self.logger.debug("[ARP] Unknown target %s on s%d", target_ip, dpid)
            return

        # IPv4 handling
        if eth_pkt.ethertype == ether_types.ETH_TYPE_IP:
            ip_pkt = pkt.get_protocol(ipv4.ipv4)
            if ip_pkt is None:
                return
            src_ip = ip_pkt.src
            dst_ip = ip_pkt.dst

            # Find which switches host (or router interface) src and dst belong to
            src_sw = self._find_switch_for_ip(src_ip)
            dst_sw = self._find_switch_for_ip(dst_ip)

            if not src_sw or not dst_sw:
                self.logger.debug("Unknown route: %s -> %s (src_sw=%s dst_sw=%s)", src_ip, dst_ip, src_sw, dst_sw)
                return

            # compute shortest path (switch names)
            try:
                path = nx.shortest_path(self.G, source=src_sw, target=dst_sw, weight="weight")
            except nx.NetworkXNoPath:
                self.logger.warning("No path %s -> %s", src_sw, dst_sw)
                return

            self.logger.info("[PATH] %s -> %s via %s", src_ip, dst_ip, path)

            # install per-hop flows along the path
            # We'll install flows matching ipv4_dst=dst_ip
            for idx, sw_name in enumerate(path):
                cur_dpid = self.name_to_dpid[sw_name]
                dp_cur = self.datapaths.get(cur_dpid)
                if dp_cur is None:
                    self.logger.warning("Datapath s%d not connected yet", cur_dpid)
                    return

                parser_cur = dp_cur.ofproto_parser
                ofp_cur = dp_cur.ofproto

                # determine out_port and next-hop MAC
                if idx == len(path) - 1:
                    # last hop -> send to destination host port and set dst MAC to host MAC
                    # find host info for dst_ip
                    host = None
                    for h in self.hosts.values():
                        if h["ip"] == dst_ip:
                            host = h
                            break
                    if host is None:
                        self.logger.error("Could not find host for %s", dst_ip)
                        return
                    out_port = _extract_port_from_ifname(
                        next((i["name"] for i in self.switches[sw_name]["interfaces"] if i.get("neighbor") == host["name"]), "")
                    )
                    # fallback to host_port_on_switch via dpid if present
                    if not out_port:
                        out_port = self.host_port_on_switch.get(cur_dpid)
                    dst_mac_next = host["mac"].lower()
                else:
                    next_sw = path[idx + 1]
                    next_dpid = self.name_to_dpid[next_sw]
                    out_port = self.adjacency.get(cur_dpid, {}).get(next_dpid)
                    if out_port is None:
                        self.logger.error("No adjacency port for %s -> %s (dpids %s->%s)",
                                          sw_name, next_sw, cur_dpid, next_dpid)
                        return
                    # next hop MAC should be the gateway mac of the next switch (router-facing MAC)
                    dst_mac_next = self.gateway_mac.get(next_dpid)
                    if not dst_mac_next:
                        # find any interface mac on next_sw that faces cur_sw
                        for intf in self.switches[next_sw]["interfaces"]:
                            if intf.get("neighbor") == sw_name:
                                dst_mac_next = intf.get("mac")
                                break
                        if not dst_mac_next:
                            self.logger.error("No next-hop MAC known for %s", next_sw)
                            return

                # current src MAC is this switch's gateway MAC if exists else find interface mac towards next hop
                src_mac_this = self.gateway_mac.get(cur_dpid)
                if not src_mac_this:
                    # try interface towards neighbor or host
                    for intf in self.switches[sw_name]["interfaces"]:
                        if intf.get("neighbor") == (path[idx + 1] if idx + 1 < len(path) else host["name"]):
                            src_mac_this = intf.get("mac")
                            break
                if not src_mac_this:
                    self.logger.warning("No gateway mac for s%d (%s); using first interface mac", cur_dpid, sw_name)
                    # fallback to first interface mac
                    first_if = self.switches[sw_name]["interfaces"][0]
                    src_mac_this = first_if.get("mac")

                # Build match+actions: decrement TTL, set dl_src/dl_dst, output to specific port
                # Use OF1.3 set_field actions for eth_src/eth_dst and DecNwTtl action
                actions = [
                    parser_cur.OFPActionDecNwTtl(),
                    parser_cur.OFPActionSetField(eth_src=src_mac_this),
                    parser_cur.OFPActionSetField(eth_dst=dst_mac_next),
                    parser_cur.OFPActionOutput(out_port)
                ]
                match = parser_cur.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ipv4_dst=dst_ip)
                # install with high priority
                self._add_flow(dp_cur, priority=3000, match=match, actions=actions)

            # Finally, forward this particular packet: generate packet_out from current datapath (the one that received packet)
            # figure out out_port from first switch in path (i.e., the switch that got this packet)
                        # Finally, forward this particular packet: generate packet_out from current datapath
            first_sw = path[0]
            first_dpid = self.name_to_dpid[first_sw]

            if first_dpid != dpid:
                # This packet-in is not on the first-hop switch: let switch handle it or ignore
                # (We installed flows on all hops; future packets should be handled by switches.)
                return

            # Compute first-hop out_port, src_mac and dst_mac for the packet_out
            if len(path) == 1:
                # src and dst are on same switch -> send to host port
                target_host = self.ip_to_host.get(dst_ip)
                if target_host:
                    out_port = _extract_port_from_ifname(
                        next((i["name"] for i in self.switches[first_sw]["interfaces"]
                              if i.get("neighbor") == target_host["name"]), "")
                    ) or self.host_port_on_switch.get(first_dpid, ofp.OFPP_FLOOD)
                else:
                    out_port = ofp.OFPP_FLOOD
                # MACs: source = gateway mac for this switch or interface-mac; dst = host MAC if known
                src_mac_pkt = self.gateway_mac.get(first_dpid)
                if not src_mac_pkt:
                    # fallback to first interface mac
                    first_if = self.switches[first_sw]["interfaces"][0]
                    src_mac_pkt = first_if.get("mac")
                dst_host = self.ip_to_host.get(dst_ip)
                dst_mac_pkt = dst_host["mac"].lower() if dst_host else None
            else:
                # normal multi-hop: next hop is path[1]
                next_sw = path[1]
                next_dpid = self.name_to_dpid[next_sw]
                out_port = self.adjacency.get(first_dpid, {}).get(next_dpid, ofp.OFPP_FLOOD)

                # src MAC is this switch's gateway/interface towards next hop
                src_mac_pkt = self.gateway_mac.get(first_dpid)
                if not src_mac_pkt:
                    for intf in self.switches[first_sw]["interfaces"]:
                        if intf.get("neighbor") == next_sw:
                            src_mac_pkt = intf.get("mac")
                            break

                # dst MAC for first hop is the next-switch's gateway/interface MAC (next-hop MAC)
                dst_mac_pkt = self.gateway_mac.get(next_dpid)
                if not dst_mac_pkt:
                    for intf in self.switches[next_sw]["interfaces"]:
                        if intf.get("neighbor") == first_sw:
                            dst_mac_pkt = intf.get("mac")
                            break

            # sanity checks
            if not src_mac_pkt or not dst_mac_pkt:
                self.logger.error("Missing MAC info for first-hop packet_out: s%d src=%s dst=%s",
                                  first_dpid, src_mac_pkt, dst_mac_pkt)

            actions = []
            # Decrement TTL and rewrite eth headers, then output
            try:
                actions.append(parser.OFPActionDecNwTtl())
            except Exception:
                # If DecNwTtl not supported, skip it
                pass
            if src_mac_pkt:
                actions.append(parser.OFPActionSetField(eth_src=src_mac_pkt))
            if dst_mac_pkt:
                actions.append(parser.OFPActionSetField(eth_dst=dst_mac_pkt))
            actions.append(parser.OFPActionOutput(out_port))

            out = parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                      in_port=in_port, actions=actions, data=msg.data)
            dp.send_msg(out)

    # -----------------------
    # helpers
    # -----------------------
    def _find_switch_for_ip(self, ip):
        # check hosts
        for h in self.hosts.values():
            if h.get("ip") == ip:
                return h.get("switch")
        # check router interfaces
        for sw_name, sw in self.switches.items():
            for intf in sw.get("interfaces", []):
                if intf.get("ip") == ip:
                    return sw_name
        return None
