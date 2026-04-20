"""A simple Hub-like controller for Part 1.

This controller keeps a MAC->port table in the controller only and
answers PacketIn events by sending PacketOuts. It does NOT install
flow rules on switches (per the assignment description for the Hub
controller).
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_0
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import ether_types


class HubController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_0.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(HubController, self).__init__(*args, **kwargs)
        # controller-side MAC table: { dpid: {mac: port} }
        self.mac_to_port = {}

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, MAIN_DISPATCHER)
    def switch_features_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        self.logger.info('switch connected: %s', datapath.id)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return

        # ignore LLDP
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dpid = datapath.id
        self.mac_to_port.setdefault(dpid, {})

        src = eth.src
        dst = eth.dst

        # learn source MAC to port mapping (controller only)
        in_port = getattr(msg, 'in_port', None)
        # Some Ryu/msg variants put in_port in msg.match
        if in_port is None:
            try:
                in_port = msg.match['in_port']
            except Exception:
                in_port = None

        self.mac_to_port[dpid][src] = in_port
        self.logger.info('packet in %s %s %s %s', dpid, src, dst, in_port)

        # decide output port using controller-only table
        out_port = self.mac_to_port[dpid].get(dst, ofproto.OFPP_FLOOD)

        actions = [datapath.ofproto_parser.OFPActionOutput(out_port)]

        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        # send PacketOut to instruct switch to forward (but do not install flows)
        out = datapath.ofproto_parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id, in_port=in_port,
            actions=actions, data=data)
        datapath.send_msg(out)

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _port_status_handler(self, ev):
        msg = ev.msg
        reason = msg.reason
        port_no = msg.desc.port_no

        ofproto = msg.datapath.ofproto
        if reason == ofproto.OFPPR_ADD:
            self.logger.info('port added %s', port_no)
        elif reason == ofproto.OFPPR_DELETE:
            self.logger.info('port deleted %s', port_no)
        elif reason == ofproto.OFPPR_MODIFY:
            self.logger.info('port modified %s', port_no)
        else:
            self.logger.info('Illeagal port state %s %s', port_no, reason)
