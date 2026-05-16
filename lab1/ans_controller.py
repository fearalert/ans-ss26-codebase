"""
 Copyright (c) 2026 Computer Networks Group @ UPB

 Permission is hereby granted, free of charge, to any person obtaining a copy of
 this software and associated documentation files (the "Software"), to deal in
 the Software without restriction, including without limitation the rights to
 use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
 the Software, and to permit persons to whom the Software is furnished to do so,
 subject to the following conditions:

 The above copyright notice and this permission notice shall be included in all
 copies or substantial portions of the Software.

 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
 FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
 COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
 IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
 CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
 """

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, arp, ipv4, icmp
from ryu.lib.packet import ether_types


# Router configuration
DPID_S1 = 1   # switch - subnet 10.0.1.0/24
DPID_S2 = 2   # switch - subnet 10.0.2.0/24
DPID_S3 = 3   # router

# Router port -> MAC (virtual gateway MACs from Figure 1)
port_to_own_mac = {
    1: "00:00:00:00:01:01",
    2: "00:00:00:00:01:02",
    3: "00:00:00:00:01:03",
}

# Router port -> gateway IP
port_to_own_ip = {
    1: "10.0.1.1",
    2: "10.0.2.1",
    3: "192.168.1.1",
}

# /24 prefix -> router egress port
subnet_to_port = {
    "10.0.1":    1,
    "10.0.2":    2,
    "192.168.1": 3,
}


def prefix24(ip):
    return ".".join(ip.split(".")[:3])


def egress_port(dst_ip):
    return subnet_to_port.get(prefix24(dst_ip))


class LearningSwitch(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(LearningSwitch, self).__init__(*args, **kwargs)
        
        # Switch MAC-learning tables  { dpid: { mac: port } }
        self.mac_to_port = {}

        # Router ARP cache  { ip: mac }
        self.arp_cache = {}

        # Packets waiting for ARP resolution  { dst_ip: [(datapath, in_port, data), ...] }
        self.pending = {}

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Initial flow entry for matching misses
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

    # Add a flow entry to the flow-table
    def add_flow(self, datapath, priority, match, actions):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Construct flow_mod message and send it
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                match=match, instructions=inst)
        datapath.send_msg(mod)

    # Handle the packet_in event
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        
        msg = ev.msg
        datapath = msg.datapath

        dpid     = datapath.id
        in_port  = msg.match['in_port']

        pkt     = packet.Packet(msg.data)
        eth_pkt = pkt.get_protocol(ethernet.ethernet)

        if eth_pkt is None:
            return
        if eth_pkt.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        if dpid in (DPID_S1, DPID_S2):
            self._switch_handler(datapath, in_port, msg, pkt, eth_pkt)
        elif dpid == DPID_S3:
            self._router_handler(datapath, in_port, msg, pkt, eth_pkt)

    # SWITCH logic  (s1 and s2)
    def _switch_handler(self, datapath, in_port, msg, pkt, eth_pkt):
        dpid    = datapath.id
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser

        self.mac_to_port.setdefault(dpid, {})

        src_mac = eth_pkt.src
        dst_mac = eth_pkt.dst

        # Learn source MAC -> port
        self.mac_to_port[dpid][src_mac] = in_port

        # Look up destination port
        if dst_mac in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst_mac]
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        # Install a flow rule when destination is known.
        # Match on (in_port, eth_src, eth_dst) to avoid asymmetric flooding
        # issues that arise when matching only on eth_dst.
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port,
                                    eth_src=src_mac,
                                    eth_dst=dst_mac)
            self.add_flow(datapath, 1, match, actions,)

        # Forward the current packet
        buffer_id = msg.buffer_id
        data      = None if buffer_id != ofproto.OFP_NO_BUFFER else msg.data
        out = parser.OFPPacketOut(datapath=datapath,
                                  buffer_id=buffer_id,
                                  in_port=in_port,
                                  actions=actions,
                                  data=data)
        datapath.send_msg(out)

    # ROUTER logic  (s3)
    def _router_handler(self, datapath, in_port, msg, pkt, eth_pkt):
        arp_pkt = pkt.get_protocol(arp.arp)
        ip_pkt  = pkt.get_protocol(ipv4.ipv4)

        if arp_pkt:
            self._router_arp(datapath, in_port, pkt, eth_pkt, arp_pkt)
        elif ip_pkt:
            self._router_ip(datapath, in_port, msg, pkt, eth_pkt, ip_pkt)

    #ARP
    def _router_arp(self, datapath, in_port, pkt, eth_pkt, arp_pkt):
        # Learn sender
        self.arp_cache[arp_pkt.src_ip] = arp_pkt.src_mac

        # Flush any packets that were waiting for this IP
        if arp_pkt.src_ip in self.pending:
            for (dp, i_port, raw) in self.pending.pop(arp_pkt.src_ip):
                self._forward_ip(dp, i_port, raw)

        if arp_pkt.opcode != arp.ARP_REQUEST:
            return

        # Is the request for one of our gateway IPs?
        own_mac = None
        for port, gw_ip in port_to_own_ip.items():
            if gw_ip == arp_pkt.dst_ip:
                own_mac = port_to_own_mac[port]
                break
        if own_mac is None:
            return

        # Send ARP reply
        reply = packet.Packet()
        reply.add_protocol(ethernet.ethernet(
            ethertype=ether_types.ETH_TYPE_ARP,
            dst=eth_pkt.src,
            src=own_mac))
        reply.add_protocol(arp.arp(
            opcode=arp.ARP_REPLY,
            src_mac=own_mac,
            src_ip=arp_pkt.dst_ip,
            dst_mac=arp_pkt.src_mac,
            dst_ip=arp_pkt.src_ip))
        self._send_pkt(datapath, in_port, reply)

    #IP
    def _router_ip(self, datapath, in_port, msg, pkt, eth_pkt, ip_pkt):
        parser = datapath.ofproto_parser
        src_ip = ip_pkt.src
        dst_ip = ip_pkt.dst

        # 1=ICMP  6=TCP  17=UDP
        proto = ip_pkt.proto   

        # Learn src IP -> MAC
        self.arp_cache[src_ip] = eth_pkt.src

        src_pfx = prefix24(src_ip)
        dst_pfx = prefix24(dst_ip)

        #Packet destined for a gateway IP itself
        for port, gw_ip in port_to_own_ip.items():
            if dst_ip == gw_ip:
                # Only answer ICMP echo from the same subnet
                if src_pfx == prefix24(gw_ip):
                    icmp_pkt = pkt.get_protocol(icmp.icmp)
                    if icmp_pkt and icmp_pkt.type == icmp.ICMP_ECHO_REQUEST:
                        self._icmp_reply(datapath, in_port, eth_pkt, ip_pkt,
                                         icmp_pkt, gw_ip, port_to_own_mac[port])
                return   # drop everything else aimed at gateway IPs

        #Find egress port
        out_port = egress_port(dst_ip)
        if out_port is None:
            return   # unknown subnet - drop

        #Security policy
        # Rule 1: block ICMP from ext (192.168.1.x) to any internal host
        if src_pfx == "192.168.1" and dst_pfx in ("10.0.1", "10.0.2"):
            if proto == 1:
                match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                        ip_proto=1,
                                        ipv4_src=src_ip,
                                        ipv4_dst=dst_ip)
                self.add_flow(datapath, 20, match, [])
                return

        # Rule 2: block TCP/UDP between ext and ser (10.0.2.x) both ways
        ext_ser = (src_pfx == "192.168.1" and dst_pfx == "10.0.2")
        ser_ext = (src_pfx == "10.0.2"    and dst_pfx == "192.168.1")
        if (ext_ser or ser_ext) and proto in (6, 17):
            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                    ip_proto=proto,
                                    ipv4_src=src_ip,
                                    ipv4_dst=dst_ip)
            self.add_flow(datapath, 20, match, [])
            return

        #Forward IP packets
        self._forward_ip(datapath, in_port, msg.data,
                         dst_ip=dst_ip, out_port=out_port, install=True)

    def _forward_ip(self, datapath, in_port, raw,
                    dst_ip=None, out_port=None, install=False):
        """Rewrite Ethernet header and forward an IP packet."""
        parser = datapath.ofproto_parser

        pkt    = packet.Packet(raw)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt is None:
            return

        if dst_ip   is None: dst_ip   = ip_pkt.dst
        if out_port is None: out_port = egress_port(dst_ip)
        if out_port is None: return

        src_ip      = ip_pkt.src
        new_src_mac = port_to_own_mac[out_port]

        dst_mac = self.arp_cache.get(dst_ip)
        if dst_mac is None:
            # Queue and send ARP request
            self.pending.setdefault(dst_ip, []).append(
                (datapath, in_port, raw))
            self._arp_request(datapath, out_port, dst_ip)
            return

        # Build new frame: fresh Ethernet header, reuse IP+payload
        new_pkt = packet.Packet()
        new_pkt.add_protocol(ethernet.ethernet(
            ethertype=ether_types.ETH_TYPE_IP,
            dst=dst_mac,
            src=new_src_mac))
        new_pkt.add_protocol(ip_pkt)
        for p in pkt.protocols:
            if not isinstance(p, (ethernet.ethernet, ipv4.ipv4, bytes)):
                new_pkt.add_protocol(p)

        # Install flow rule so future packets bypass the controller
        if install:
            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                    ipv4_src=src_ip,
                                    ipv4_dst=dst_ip)
            actions = [parser.OFPActionSetField(eth_src=new_src_mac),
                       parser.OFPActionSetField(eth_dst=dst_mac),
                       parser.OFPActionOutput(out_port)]
            self.add_flow(datapath, 10, match, actions)

        self._send_pkt(datapath, out_port, new_pkt)

    #ICMP echo reply
    def _icmp_reply(self, datapath, in_port, eth_pkt, ip_pkt,
                    icmp_pkt, gw_ip, gw_mac):
        reply = packet.Packet()
        reply.add_protocol(ethernet.ethernet(
            ethertype=ether_types.ETH_TYPE_IP,
            dst=eth_pkt.src,
            src=gw_mac))
        reply.add_protocol(ipv4.ipv4(
            dst=ip_pkt.src,
            src=gw_ip,
            proto=1))
        reply.add_protocol(icmp.icmp(
            type_=icmp.ICMP_ECHO_REPLY,
            code=0,
            csum=0,
            data=icmp_pkt.data))
        self._send_pkt(datapath, in_port, reply)

    #ARP request (router -> next-hop)
    def _arp_request(self, datapath, out_port, target_ip):
        src_mac = port_to_own_mac[out_port]
        src_ip  = port_to_own_ip[out_port]
        req = packet.Packet()
        req.add_protocol(ethernet.ethernet(
            ethertype=ether_types.ETH_TYPE_ARP,
            dst="ff:ff:ff:ff:ff:ff",
            src=src_mac))
        req.add_protocol(arp.arp(
            opcode=arp.ARP_REQUEST,
            src_mac=src_mac,
            src_ip=src_ip,
            dst_mac="00:00:00:00:00:00",
            dst_ip=target_ip))
        self._send_pkt(datapath, out_port, req)

        #Raw packet-out helper
    def _send_pkt(self, datapath, port, pkt):
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser
        pkt.serialize()
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER,
            actions=[parser.OFPActionOutput(port=port)],
            data=pkt.data)
        datapath.send_msg(out)