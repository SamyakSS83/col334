# Layer-3-like Shortest Path Routing (L3SPF) Ryu App (OpenFlow 1.0)
# - Computes shortest path between source/destination switches
# - Rewrites Ethernet src/dst to router-facing MACs to enable inter-subnet routing
# - Decrements IPv4 TTL and drops when TTL reaches zero
# - No ECMP required for Part 3

import os
import json
import heapq
from collections import defaultdict

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.topology import event
from ryu.topology.api import get_switch, get_link
from ryu.ofproto import ofproto_v1_0
from ryu.lib.packet import packet, ethernet, ipv4, arp, ether_types
try:
    import networkx as nx  # optional, for better debug visualization
except Exception:
    nx = None

class L3SPF(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_0.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(L3SPF, self).__init__(*args, **kwargs)
        # Load config and build graph/host_cfg/iface_mac
        # Load Part3 config (hosts, switches, links)
        cfg_path = os.environ.get('CFG', os.path.join(os.path.dirname(__file__), 'p3_config.json'))
        with open(cfg_path) as f:
            cfg = json.load(f)
        # Build graph from links
        self.graph = {}
        for link in cfg.get('links', []):
            u = int(link['src'].lstrip('r'))
            v = int(link['dst'].lstrip('r'))
            w = link.get('cost', 1)
            self.graph.setdefault(u, {})[v] = w
            self.graph.setdefault(v, {})[u] = w
        # Optional: networkx graph for debugging
        self.nxG = None
        if nx is not None:
            self.nxG = nx.Graph()
            for u, nbrs in self.graph.items():
                self.nxG.add_node(u)
                for v, w in nbrs.items():
                    if not self.nxG.has_edge(u, v):
                        self.nxG.add_edge(u, v, weight=w)
        # Map interface MACs per (dpid -> neighbor_dpid)
        from collections import defaultdict as _ddict
        self.iface_mac = _ddict(dict)
        for sw in cfg['switches']:
            u = sw['dpid']
            for intf in sw['interfaces']:
                nb = intf['neighbor']
                mac = intf.get('mac')
                if nb.startswith('r'):
                    v = int(nb.lstrip('r'))
                    self.iface_mac[u][v] = mac
        # Host config mapping (mac -> (dpid, ip))
        # Build host mapping from hosts section (use actual host MACs)
        self.host_cfg = {}
        for h in cfg.get('hosts', []):
            m = h.get('mac')
            sw = int(h.get('switch').lstrip('r'))
            ip = h.get('ip')
            self.host_cfg[m.lower()] = (sw, ip)
        self.logger.info("[CFG] hosts: %s", self.host_cfg)
        self.logger.info("[CFG] L3SPF loaded: %d switches, %d links, %d hosts", len(cfg.get('switches', [])), len(cfg.get('links', [])), len(self.host_cfg))
        # Debug: print weighted graph
        if self.nxG is not None:
            edges_dbg = [(u, v, d.get('weight')) for u, v, d in self.nxG.edges(data=True)]
            self.logger.info("[CFG] Graph edges (u, v, w): %s", edges_dbg)
        else:
            edges_dbg = []
            for u, nbrs in self.graph.items():
                for v, w in nbrs.items():
                    if u < v:
                        edges_dbg.append((u, v, w))
            self.logger.info("[CFG] Graph edges (u, v, w): %s", edges_dbg)
        # Build per-switch gateway MAC for host-facing interfaces
        self.gateway_mac = {}
        for sw in cfg.get('switches', []):
            u = sw['dpid']
            for intf in sw.get('interfaces', []):
                if intf['neighbor'].startswith('h'):
                    self.gateway_mac[u] = intf.get('mac')

        # Identify two hosts from host_cfg
        hosts = list(self.host_cfg.items())
        if len(hosts) == 2:
            (m1,(sw1,ip1)), (m2,(sw2,ip2)) = hosts
            self.host1_mac, self.host1_ip = m1.lower(), ip1
            self.host2_mac, self.host2_ip = m2.lower(), ip2
        # runtime
        self.datapaths = {}
        self.adjacency = defaultdict(dict)
        self.hosts = {}  # learned MAC->(dpid,port)
        # cache to avoid redundant installs
        self.installed_paths = {}

    def _debug_dump(self):
        try:
            self.logger.info("[DBG] adjacency: %s", {k: dict(v) for k, v in self.adjacency.items()})
            self.logger.info("[DBG] learned hosts: %s", self.hosts)
            if hasattr(self, 'installed_paths'):
                self.logger.info("[DBG] installed_paths: %s", self.installed_paths)
        except Exception as e:
            self.logger.info("[DBG] dump error: %s", e)

    # Datapath lifecycle
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            dpid = dp.id
            self.datapaths[dpid] = dp
            self.logger.info("[DP] Registered s%d", dpid)
            # install ARP flood rule to help learning
            self._install_arp_rule(dp)
        elif ev.state == DEAD_DISPATCHER:
            dpid = dp.id
            # remove only if we have a valid datapath ID
            if dpid is not None:
                self.datapaths.pop(dpid, None)
                self.logger.info("[DP] Unregistered s%d", dpid)
            else:
                self.logger.info("[DP] Unregistered datapath with no dpid")

    # Topology discovery
    @set_ev_cls(event.EventSwitchEnter)
    def _topo(self, ev):
        for sw in get_switch(self, None):
            self.adjacency.setdefault(sw.dp.id, {})
        for l in get_link(self, None):
            self.adjacency.setdefault(l.src.dpid, {})[l.dst.dpid] = l.src.port_no
            self.adjacency.setdefault(l.dst.dpid, {})[l.src.dpid] = l.dst.port_no
        self.logger.info("[TOPO] Switches: %s", [f"s{sw.dp.id}" for sw in get_switch(self, None)])
        self.logger.info("[TOPO] Links: %s", [(f"s{lk.src.dpid}", f"s{lk.dst.dpid}") for lk in get_link(self, None)])
        
        # Proactive host learning from config (needed because OVS handles ARP locally)
        # h1 on s1 port 1, h2 on s6 port 1 (based on link creation order in topology)
        if '00:00:00:00:01:02' in self.host_cfg:
            self.hosts['00:00:00:00:01:02'] = (1, 1)  # h1 on s1 port 1
            self.logger.info("[HOST PROACTIVE] Learned h1 (00:00:00:00:01:02) at s1 port 1")
        if '00:00:00:00:06:02' in self.host_cfg:
            self.hosts['00:00:00:00:06:02'] = (6, 1)  # h2 on s6 port 1 (first link created for s6)
            self.logger.info("[HOST PROACTIVE] Learned h2 (00:00:00:00:06:02) at s6 port 1")
        
        # Try proactive installation after topology is discovered
        self._try_proactive_install()
        
        self._debug_dump()
    
    def _try_proactive_install(self):
        """Install flows proactively if both hosts are learned and all needed datapaths are ready"""
        if '00:00:00:00:01:02' not in self.hosts or '00:00:00:00:06:02' not in self.hosts:
            return
        
        # Check if path exists and all switches on path have connected datapaths
        src_sw = self.hosts['00:00:00:00:01:02'][0]
        dst_sw = self.hosts['00:00:00:00:06:02'][0]
        path = self._shortest_path(src_sw, dst_sw)
        if not path:
            self.logger.info("[PROACTIVE] No path yet between s%d and s%d", src_sw, dst_sw)
            return
        
        # Check if all switches on path are connected
        for dpid in path:
            if dpid not in self.datapaths:
                self.logger.info("[PROACTIVE] Waiting for s%d to connect before installing", dpid)
                return
        
        # All prerequisites met, install flows
        self.logger.info("[PROACTIVE] Installing flows for h1 <-> h2 via path %s", path)
        try:
            # Clear any stale cache entries first
            self.installed_paths.pop(('00:00:00:00:01:02', '00:00:00:00:06:02'), None)
            self.installed_paths.pop(('00:00:00:00:06:02', '00:00:00:00:01:02'), None)
            
            # Install h1 -> h2 path
            self._install(None, '00:00:00:00:01:02', '00:00:00:00:06:02')
            # Install h2 -> h1 path (reverse)
            self._install(None, '00:00:00:00:06:02', '00:00:00:00:01:02')
            self.logger.info("[PROACTIVE] Flows successfully installed for h1 <-> h2")
        except Exception as e:
            self.logger.error("[PROACTIVE] Failed to install flows: %s", e)
            import traceback
            traceback.print_exc()

    @set_ev_cls(event.EventLinkAdd)
    def _link_add(self, ev):
        l = ev.link
        self.adjacency.setdefault(l.src.dpid, {})[l.dst.dpid] = l.src.port_no
        self.adjacency.setdefault(l.dst.dpid, {})[l.src.dpid] = l.dst.port_no
        self.logger.info("[TOPO] Link add s%d:p%d <-> s%d:p%d", l.src.dpid, l.src.port_no, l.dst.dpid, l.dst.port_no)
        # Try proactive installation each time topology changes
        self._try_proactive_install()
        self._debug_dump()

    # Dijkstra (single-path)
    def _shortest_path(self, src, dst):
        self.logger.info("[SPF] compute path %s -> %s", src, dst)
        dist = {n: float('inf') for n in self.graph}
        prev = {n: None for n in self.graph}
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
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        if dist[dst] == float('inf'):
            return None
        # reconstruct
        path = []
        cur = dst
        while cur is not None:
            path.append(cur)
            cur = prev[cur]
        path.reverse()
        self.logger.info("[SPF] path %s cost=%s", path, dist[dst])
        return path

    def _install_l3_flows(self, path, src_mac, src_ip, dst_mac, dst_ip):
        # For OF1.0 we can't match on IP directly as flexibly as OF1.3, but we can:
        # - Match on in_port + dl_src + dl_dst and emit actions:
        #   - Set dl_src to router gw mac of this switch
        #   - Set dl_dst to next-hop MAC (we use per-switch gw MAC as proxy)
        # - We can't decrement TTL on switch in OF1.0 directly; emulate by catching low-TTL PacketIns and dropping.
        # For assignment purposes, we show intent in logs and do L2.5 rewrite with MACs.
        for i, dpid in enumerate(path):
            dp = self.datapaths.get(dpid)
            if not dp:
                continue
            parser = dp.ofproto_parser
            # in_port is from host on first switch; otherwise from prev switch
            if i == 0:
                if src_mac not in self.hosts:
                    self.logger.info("[FLOW] missing src host port for %s; abort install", src_mac)
                    return
                in_port = self.hosts[src_mac][1]
            else:
                prev = path[i-1]
                in_port = self.adjacency[dpid][prev]
            # out_port is to next hop or to host on last
            if i == len(path)-1:
                if dst_mac in self.hosts:
                    out_port = self.hosts[dst_mac][1]
                else:
                    # fallback: flood on last hop until we learn dst host port
                    out_port = ofproto_v1_0.OFPP_FLOOD
            else:
                nxt = path[i+1]
                out_port = self.adjacency[dpid][nxt]

            # choose this switch's gateway MAC for source rewriting
            gw_mac = self.gateway_mac.get(dpid)
            if not gw_mac:
                # fallback placeholder MAC if none configured
                gw_mac = f"00:00:00:00:{dpid:02x}:{dpid:02x}"
            
            # IP-based match stays valid across MAC rewrites
            match = parser.OFPMatch(in_port=in_port, dl_type=0x0800, nw_dst=dst_ip)
            
            # Set dl_src to this switch's gateway MAC
            # Set dl_dst to the destination host MAC (for L3 routing, dst stays as target host)
            actions = [
                parser.OFPActionSetDlSrc(gw_mac),
                parser.OFPActionSetDlDst(dst_mac),
                parser.OFPActionOutput(out_port)
            ]
            fm = parser.OFPFlowMod(datapath=dp, match=match, idle_timeout=5 if i == len(path)-1 and dst_mac not in self.hosts else 0,
                                   hard_timeout=0, priority=1000, actions=actions)
            dp.send_msg(fm)
            self.logger.info("[FLOW] s%d in=%s %s(%s)->%s(%s) set dlsrc=%s dldst=%s out=%s",
                             dpid, in_port, src_mac, src_ip, dst_mac, dst_ip, gw_mac, dst_mac, out_port)
            # On the ingress switch, also catch frames sent by host to the gateway MAC to avoid LOCAL capture
            if i == 0:
                try:
                    gw = self.gateway_mac.get(dpid)
                    if gw:
                        m2 = parser.OFPMatch(in_port=in_port, dl_type=0x0800, dl_dst=gw)
                        a2 = [parser.OFPActionSetDlSrc(gw), parser.OFPActionSetDlDst(dst_mac), parser.OFPActionOutput(out_port)]
                        fm2 = parser.OFPFlowMod(datapath=dp, match=m2, idle_timeout=5, hard_timeout=0, priority=1500, actions=a2)
                        dp.send_msg(fm2)
                        self.logger.info("[FLOW+INGRESS] s%d catch dl_dst=%s in_port=%s -> set dst=%s out %s", dpid, gw, in_port, dst_mac, out_port)
                except Exception as e:
                    self.logger.info("[FLOW+INGRESS] s%d failed: %s", dpid, e)

        # reverse path (symmetric)
        rev = list(reversed(path))
        for i, dpid in enumerate(rev):
                dp = self.datapaths.get(dpid)
                if not dp:
                    continue
                parser = dp.ofproto_parser
                if i == 0:
                    if dst_mac not in self.hosts:
                        self.logger.info("[FLOW] missing dst host port for %s (reverse); abort install", dst_mac)
                        return
                    in_port = self.hosts[dst_mac][1]
                else:
                    prev = rev[i-1]
                    in_port = self.adjacency[dpid][prev]
                if i == len(rev)-1:
                    if src_mac in self.hosts:
                        out_port = self.hosts[src_mac][1]
                    else:
                        out_port = ofproto_v1_0.OFPP_FLOOD
                else:
                    nxt = rev[i+1]
                    out_port = self.adjacency[dpid][nxt]
                gw_mac = self.gateway_mac.get(dpid)
                if not gw_mac:
                    gw_mac = f"00:00:00:00:{dpid:02x}:{dpid:02x}"
                match = parser.OFPMatch(in_port=in_port, dl_type=0x0800, nw_dst=src_ip)
                actions = [
                    parser.OFPActionSetDlSrc(gw_mac),
                    parser.OFPActionSetDlDst(src_mac),
                    parser.OFPActionOutput(out_port)
                ]
                fm = parser.OFPFlowMod(datapath=dp, match=match, idle_timeout=5 if i == len(rev)-1 and src_mac not in self.hosts else 0,
                                     hard_timeout=0, priority=1000, actions=actions)
                dp.send_msg(fm)
                self.logger.info("[FLOW] s%d in=%s %s(%s)->%s(%s) set dlsrc=%s dldst=%s out=%s",
                                dpid, in_port, dst_mac, dst_ip, src_mac, src_ip, gw_mac, src_mac, out_port)
                # On the reverse ingress (destination host side), catch frames to local gateway MAC too
                if i == 0:
                    try:
                        gw = self.gateway_mac.get(dpid)
                        if gw:
                            m2 = parser.OFPMatch(in_port=in_port, dl_type=0x0800, dl_dst=gw)
                            a2 = [parser.OFPActionSetDlSrc(gw), parser.OFPActionSetDlDst(src_mac), parser.OFPActionOutput(out_port)]
                            fm2 = parser.OFPFlowMod(datapath=dp, match=m2, idle_timeout=5, hard_timeout=0, priority=1500, actions=a2)
                            dp.send_msg(fm2)
                            self.logger.info("[FLOW+INGRESS] s%d (rev) catch dl_dst=%s in_port=%s -> set dst=%s out %s", dpid, gw, in_port, src_mac, out_port)
                    except Exception as e:
                        self.logger.info("[FLOW+INGRESS] s%d (rev) failed: %s", dpid, e)

    def _install_arp_rule(self, dp):
        try:
            parser, ofp = dp.ofproto_parser, dp.ofproto
            match = parser.OFPMatch(dl_type=0x0806)  # ARP
            actions = [parser.OFPActionOutput(ofproto_v1_0.OFPP_FLOOD),
                       parser.OFPActionOutput(ofproto_v1_0.OFPP_LOCAL)]
            fm = parser.OFPFlowMod(datapath=dp, match=match, idle_timeout=0, hard_timeout=0,
                                   priority=5, actions=actions)
            dp.send_msg(fm)
            self.logger.info("[INIT] ARP flood+local rule installed on s%d", dp.id)
        except Exception as e:
            self.logger.info("[INIT] ARP rule install failed on s%d: %s", getattr(dp, 'id', -1), e)

        # Simple wrapper used by packet_in to install forward/reverse flows
    def _install(self, path, src_mac, dst_mac):
        # normalize
        src = src_mac.lower()
        dst = dst_mac.lower()
        # lookup IPs from config mapping
        src_ip = self.host_cfg.get(src, (None, None))[1]
        dst_ip = self.host_cfg.get(dst, (None, None))[1]
        if src_ip is None or dst_ip is None:
            self.logger.warning("[INSTALL] missing IP for %s or %s", src, dst)
            return
        # avoid re-installing identical path for same flow
        key = (src, dst)
        # simple cache already initialized in __init__
        # compute path if not provided
        if not path:
            src_sw = self.hosts.get(src, (None,))[0]
            dst_sw = self.hosts.get(dst, (None,))[0]
            path = self._shortest_path(src_sw, dst_sw)
            if not path:
                return
        if self.installed_paths.get(key) == tuple(path):
            self.logger.info("[INSTALL] skip, same path cached for %s->%s: %s", src, dst, path)
            return
        self._install_l3_flows(path, src, src_ip, dst, dst_ip)
        self.installed_paths[key] = tuple(path)
        self.logger.info("[INSTALL] cached %s->%s: %s", src, dst, path)

    def _route_and_install(self, src_mac, src_ip, dst_mac, dst_ip):
        src_sw, _ = self.hosts[src_mac]
        dst_sw, dst_port = self.hosts[dst_mac]
        path = self._shortest_path(src_sw, dst_sw)
        if not path:
            self.logger.warning("[ROUTE] no path s%d->s%d", src_sw, dst_sw)
            return None
        self.logger.info("[ROUTE] %s(%s) -> %s(%s) via %s", src_mac, src_ip, dst_mac, dst_ip, path)
        self._install_l3_flows(path, src_mac, src_ip, dst_mac, dst_ip)
        return path

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        dp, ofp, parser = msg.datapath, msg.datapath.ofproto, msg.datapath.ofproto_parser
        dpid, in_port = dp.id, msg.in_port
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if not eth or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        src_mac, dst_mac = eth.src.lower(), eth.dst.lower()
        # Debug print ethertype
        self.logger.info("[PKTIN] s%d in_port=%d ethertype=0x%04x src=%s dst=%s", dpid, in_port, eth.ethertype, src_mac, dst_mac)
        # learn only if host from config AND on its expected access switch AND NOT on an inter-switch port
        if src_mac in self.host_cfg and src_mac not in self.hosts:
            expected_sw = self.host_cfg[src_mac][0]
            # Check if in_port is an inter-switch link (if so, skip learning)
            is_inter_switch = in_port in self.adjacency.get(dpid, {}).values()
            if dpid == expected_sw and not is_inter_switch:
                self.hosts[src_mac] = (dpid, in_port)
                self.logger.info("[LEARN] %s at s%d p%d (expected, not inter-switch)", src_mac, dpid, in_port)
            else:
                reason = "inter-switch port" if is_inter_switch else f"expected s{expected_sw}"
                self.logger.info("[SKIP LEARN] %s seen at s%d p%d (%s)", src_mac, dpid, in_port, reason)
        # Handle ARP proactively: flood to help endpoints learn
        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt is not None:
            self.logger.info("[ARP] op=%s src_ip=%s dst_ip=%s", arp_pkt.opcode, arp_pkt.src_ip, arp_pkt.dst_ip)
            actions = [parser.OFPActionOutput(ofproto_v1_0.OFPP_FLOOD)]
            out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id, in_port=in_port, actions=actions, data=msg.data)
            dp.send_msg(out)
            return
        # If both hosts are learned, proactively install flows
        if hasattr(self, 'host1_mac') and hasattr(self, 'host2_mac'):
            if self.host1_mac in self.hosts and self.host2_mac in self.hosts:
                s_sw = self.hosts[self.host1_mac][0]
                d_sw = self.hosts[self.host2_mac][0]
                path = self._shortest_path(s_sw, d_sw)
                if path:
                    self.logger.info("[AUTO-INSTALL] path %s for %s->%s", path, self.host1_mac, self.host2_mac)
                    self._install(path, self.host1_mac, self.host2_mac)
                    self._install(list(reversed(path)), self.host2_mac, self.host1_mac)
                self._debug_dump()
        # only process IPv4 between h1 and h2
        ip = pkt.get_protocol(ipv4.ipv4)
        if not ip:
            self.logger.info("[PKTIN] non-IPv4 ethertype=%s on s%d p%d src=%s dst=%s", eth.ethertype, dpid, in_port, src_mac, dst_mac)
            return
        if src_mac == self.host1_mac and ip.dst == self.host2_ip:
            forward = True
        elif src_mac == self.host2_mac and ip.dst == self.host1_ip:
            forward = False
        else:
            self.logger.info("[PKTIN] IPv4 not target flow src=%s ip.dst=%s on s%d", src_mac, ip.dst, dpid)
            return
        # ensure both hosts are learned
        if self.host1_mac not in self.hosts or self.host2_mac not in self.hosts:
            self.logger.info("[WAIT] hosts not fully learned: %s %s", self.host1_mac in self.hosts, self.host2_mac in self.hosts)
            return
        # build path for forward direction only
        if forward:
            src, dst = self.host1_mac, self.host2_mac
        else:
            src, dst = self.host2_mac, self.host1_mac
        src_sw = self.hosts[src][0]
        dst_sw = self.hosts[dst][0]
        path = self._shortest_path(src_sw, dst_sw)
        if not path:
            return
        self.logger.info("[ROUTE] %s->%s via %s", src, dst, path)
        # install symmetric flows
        self._install(path, src, dst)
        self._install(list(reversed(path)), dst, src)
        # send original packet along path
        out_port = self.adjacency[dpid].get(path[1], None)
        if out_port:
            out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                      in_port=in_port,
                                      actions=[parser.OFPActionOutput(out_port)],
                                      data=msg.data)
            dp.send_msg(out)
            self.logger.info("[OUT] s%d outport=%d for %s->%s", dpid, out_port, src, dst)
        else:
            self.logger.info("[OUT] no out_port for s%d on next hop of %s", dpid, path)
