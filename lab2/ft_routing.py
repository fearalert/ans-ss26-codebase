"""
 Copyright (c) 2025 Computer Networks Group @ UPB

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

#!/usr/bin/env python3

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import ether_types
from ryu.lib.packet import ipv4
from ryu.lib.packet import arp

from ryu.topology import event
from ryu.topology.api import get_switch, get_link

import topo


# Flow priorities. Prefix-based entries (host /32, pod /24, pod-block /16)
# always outrank suffix-based entries, as suggested in the lab handout, so
# that intra-pod / locally-known traffic never gets sent further up the
# tree than necessary.
PRIORITY_HOST = 40
PRIORITY_POD_PREFIX = 30
PRIORITY_PODBLOCK_PREFIX = 20
PRIORITY_SUFFIX = 10


class FTRouter(app_manager.RyuApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(FTRouter, self).__init__(*args, **kwargs)
        
        # Initialize the topology with #ports=4
        self.topo_net = topo.Fattree(4)
        self.k = 4

        # The switches are named s1..sN (dpid 1..N) in fat-tree.py, in the
        # same order as self.topo_net.switches. This lets us recover the
        # fat-tree address (and hence the role/position) of every switch
        # from its dpid, without relying on any static port mapping.
        self.dpid_to_node = {}
        self.node_id_to_dpid = {}
        for i, switch in enumerate(self.topo_net.switches):
            dpid = i + 1
            self.dpid_to_node[dpid] = switch
            self.node_id_to_dpid[switch.id] = dpid

        # dpid -> datapath object, used to push flows/packets to any switch
        self.datapaths = {}

        # dpid -> set of all port numbers on that switch
        self.switch_ports = {}

        # dpid -> {neighbor_dpid: local_port_no}, switch-to-switch links only,
        # discovered at runtime via get_link()
        self.adjacency = {}

        # host IP -> (dpid, port) of the edge switch port it is attached to,
        # and host IP -> MAC, both learned at runtime from ARP traffic
        self.host_location = {}
        self.host_mac = {}


    # Topology discovery
    @set_ev_cls(event.EventSwitchEnter)
    def get_topology_data(self, ev):

        # Switches and links in the network
        switch_list = get_switch(self, None)
        for sw in switch_list:
            dpid = sw.dp.id
            self.datapaths[dpid] = sw.dp
            self.switch_ports[dpid] = set(p.port_no for p in sw.ports)

        links = get_link(self, None)
        adjacency = {}
        for link in links:
            adjacency.setdefault(link.src.dpid, {})[link.dst.dpid] = link.src.port_no
            adjacency.setdefault(link.dst.dpid, {})[link.src.dpid] = link.dst.port_no
        self.adjacency = adjacency

        # Now that the (switch-level) topology is known, install the
        # two-level prefix/suffix flow entries on every switch.
        self._install_structural_flows()


    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        self.datapaths[datapath.id] = datapath

        # Install entry-miss flow entry
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)


    # Add a flow entry to the flow-table
    def add_flow(self, datapath, priority, match, actions):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Construct flow_mod message and send it
        inst = [parser.OFPInstructionActions(
            ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                match=match, instructions=inst)
        datapath.send_msg(mod)


    # Neighbours of a switch in the static fat-tree graph, of a given type
    # ("host", "edge", "agg" or "core"), with their dpid and the local port
    # towards them (if already discovered via get_link()).
    def _neighbors(self, node, dpid, ntype):
        result = []
        for edge in node.edges:
            other = edge.rnode if edge.lnode is node else edge.lnode
            if other.type != ntype:
                continue
            if ntype == 'host':
                result.append((other, None, None))
                continue
            other_dpid = self.node_id_to_dpid.get(other.id)
            port = self.adjacency.get(dpid, {}).get(other_dpid)
            result.append((other, other_dpid, port))
        return result


    # Ports of a switch that are NOT used for inter-switch links, i.e. the
    # ports that (may) connect to hosts. Used for proxy-ARP flooding.
    def _host_ports(self, dpid):
        all_ports = self.switch_ports.get(dpid, set())
        link_ports = set(self.adjacency.get(dpid, {}).values())
        return all_ports - link_ports


    # Edge switch dpid that a given host IP (10.pod.switch.id) is attached to,
    # derived from the fat-tree addressing scheme (Section 3.2 of the paper).
    def _edge_dpid_for_ip(self, ip):
        parts = ip.split('.')
        if len(parts) != 4:
            return None
        node_id = '10.{}.{}.1'.format(parts[1], parts[2])
        return self.node_id_to_dpid.get(node_id)


    # Install the prefix (downward) and suffix (upward) lookup tables on
    # every switch, following Section 3.5 / Algorithms 1 & 2 of the
    # fat-tree paper:
    #   - edge switches:  /32 entries for directly attached hosts (added
    #                      lazily once a host's port is learned, see
    #                      _learn_host) + a suffix table towards the
    #                      aggregation switches of the same pod.
    #   - agg switches:   /24 "pod prefix" entries towards each edge switch
    #                      of the same pod + a suffix table towards the
    #                      core switches.
    #   - core switches:  /16 "pod block" entries towards the (single) agg
    #                      switch of each pod that this core switch connects
    #                      to. No suffix table is needed at the top level.
    def _install_structural_flows(self):
        half = self.k // 2

        for i, node in enumerate(self.topo_net.switches):
            dpid = i + 1
            dp = self.datapaths.get(dpid)
            if dp is None:
                continue
            parser = dp.ofproto_parser

            if node.type == 'edge':
                # Suffix table: spread traffic destined to hosts with id
                # 'h' (the last octet of 10.pod.switch.h) over the
                # aggregation switches of this pod, round-robin on h.
                agg_neighbors = self._neighbors(node, dpid, 'agg')
                agg_neighbors.sort(key=lambda t: int(t[0].id.split('.')[2]))
                self._install_suffix_flows(dp, parser, agg_neighbors, half)

            elif node.type == 'agg':
                pod = node.id.split('.')[1]

                # Prefix table: 10.pod.<edge_idx>.0/24 -> port towards that
                # edge switch (intra-pod traffic).
                for other, other_dpid, port in self._neighbors(node, dpid, 'edge'):
                    if port is None:
                        continue
                    edge_idx = other.id.split('.')[2]
                    prefix_ip = '10.{}.{}.0'.format(pod, edge_idx)
                    match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                             ipv4_dst=(prefix_ip, '255.255.255.0'))
                    actions = [parser.OFPActionOutput(port)]
                    self.add_flow(dp, PRIORITY_POD_PREFIX, match, actions)

                # Suffix table: spread inter-pod traffic over the core
                # switches this aggregation switch connects to, round-robin
                # on the destination host id.
                core_neighbors = self._neighbors(node, dpid, 'core')
                core_neighbors.sort(key=lambda t: int(t[0].id.split('.')[2]))
                self._install_suffix_flows(dp, parser, core_neighbors, half)

            elif node.type == 'core':
                # Prefix table: 10.pod.0.0/16 -> port towards the (single)
                # aggregation switch of that pod this core switch connects to.
                for other, other_dpid, port in self._neighbors(node, dpid, 'agg'):
                    if port is None:
                        continue
                    pod = other.id.split('.')[1]
                    prefix_ip = '10.{}.0.0'.format(pod)
                    match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                             ipv4_dst=(prefix_ip, '255.255.0.0'))
                    actions = [parser.OFPActionOutput(port)]
                    self.add_flow(dp, PRIORITY_PODBLOCK_PREFIX, match, actions)


    # Install one suffix-matching flow per possible host id 'h' (the last
    # octet of a 10.pod.switch.h address), spreading them round-robin over
    # the given (ordered) list of upward neighbors.
    def _install_suffix_flows(self, dp, parser, up_neighbors, half):
        if not up_neighbors:
            return
        for h in range(2, half + 2):
            _other, _other_dpid, port = up_neighbors[(h - 2) % len(up_neighbors)]
            if port is None:
                continue
            match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                     ipv4_dst=('0.0.0.{}'.format(h), '0.0.0.255'))
            actions = [parser.OFPActionOutput(port)]
            self.add_flow(dp, PRIORITY_SUFFIX, match, actions)


    # Record where a host with the given IP/MAC is attached, the first time
    # we see traffic from it, and install its /32 "down" entry on its edge
    # switch.
    def _learn_host(self, dpid, port, ip, mac):
        if ip not in self.host_location:
            self.host_location[ip] = (dpid, port)
            self.host_mac[ip] = mac

            dp = self.datapaths.get(dpid)
            if dp is not None:
                parser = dp.ofproto_parser
                match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ipv4_dst=ip)
                actions = [parser.OFPActionOutput(port)]
                self.add_flow(dp, PRIORITY_HOST, match, actions)


    # Send a (proxy) ARP reply "target_ip is at target_mac" to req_mac,
    # out of the given datapath/port.
    def _send_arp_reply(self, datapath, port, target_ip, target_mac, req_ip, req_mac):
        parser = datapath.ofproto_parser

        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP,
                                            dst=req_mac, src=target_mac))
        pkt.add_protocol(arp.arp(opcode=arp.ARP_REPLY,
                                  src_mac=target_mac, src_ip=target_ip,
                                  dst_mac=req_mac, dst_ip=req_ip))
        pkt.serialize()

        actions = [parser.OFPActionOutput(port)]
        out = parser.OFPPacketOut(datapath=datapath,
                                   buffer_id=datapath.ofproto.OFP_NO_BUFFER,
                                   in_port=datapath.ofproto.OFPP_CONTROLLER,
                                   actions=actions, data=pkt.data)
        datapath.send_msg(out)


    # Handle an ARP packet: learn the sender's location/MAC (installing its
    # /32 flow), and either proxy-reply directly (if we already know the
    # target) or flood the request towards the target's edge switch so it
    # can reply for real.
    def _handle_arp(self, msg, pkt, eth):
        datapath = msg.datapath
        dpid = datapath.id
        in_port = msg.match['in_port']
        parser = datapath.ofproto_parser

        arp_pkt = pkt.get_protocol(arp.arp)

        self._learn_host(dpid, in_port, arp_pkt.src_ip, eth.src)

        if arp_pkt.opcode == arp.ARP_REQUEST:
            target_ip = arp_pkt.dst_ip

            if target_ip in self.host_mac:
                self._send_arp_reply(datapath, in_port,
                                      target_ip, self.host_mac[target_ip],
                                      arp_pkt.src_ip, eth.src)
                return

            # Target not yet known: flood the request out of the candidate
            # host ports of the edge switch that owns target_ip. This stays
            # confined to the leaf ports of a single switch, so it cannot
            # create a forwarding loop.
            dst_edge = self._edge_dpid_for_ip(target_ip)
            if dst_edge is None or dst_edge not in self.datapaths:
                return

            target_dp = self.datapaths[dst_edge]
            out_ports = self._host_ports(dst_edge)
            if dst_edge == dpid:
                out_ports = out_ports - {in_port}
            if not out_ports:
                return

            actions = [target_dp.ofproto_parser.OFPActionOutput(p) for p in out_ports]
            out = target_dp.ofproto_parser.OFPPacketOut(
                datapath=target_dp,
                buffer_id=target_dp.ofproto.OFP_NO_BUFFER,
                in_port=target_dp.ofproto.OFPP_CONTROLLER,
                actions=actions, data=msg.data)
            target_dp.send_msg(out)

        else:
            # Real ARP reply from the target host: forward it back (as a
            # proxy reply) to the original requester, whose location is
            # already known from its ARP request.
            req_loc = self.host_location.get(arp_pkt.dst_ip)
            if req_loc is None:
                return
            req_dpid, req_port = req_loc
            req_dp = self.datapaths.get(req_dpid)
            if req_dp is None:
                return
            self._send_arp_reply(req_dp, req_port,
                                  arp_pkt.src_ip, arp_pkt.src_mac,
                                  arp_pkt.dst_ip, arp_pkt.dst_mac)


    # Fallback for IP packets that reach the controller (this should not
    # normally happen once the structural + host flows are installed, but
    # can occur for the very first packets while topology discovery is
    # still in progress). Learn the source, make sure the structural flows
    # exist, and best-effort deliver this one packet by flooding it towards
    # the destination's edge switch.
    def _handle_ip(self, msg, pkt, eth):
        datapath = msg.datapath
        dpid = datapath.id
        in_port = msg.match['in_port']

        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        src_ip, dst_ip = ip_pkt.src, ip_pkt.dst

        self._learn_host(dpid, in_port, src_ip, eth.src)
        self._install_structural_flows()

        dst_edge = self._edge_dpid_for_ip(dst_ip)
        if dst_edge is None or dst_edge not in self.datapaths:
            return

        loc = self.host_location.get(dst_ip)
        if loc is not None:
            return  # the proper flow should now be installed; drop this one

        target_dp = self.datapaths[dst_edge]
        out_ports = self._host_ports(dst_edge)
        if dst_edge == dpid:
            out_ports = out_ports - {in_port}
        if not out_ports:
            return

        actions = [target_dp.ofproto_parser.OFPActionOutput(p) for p in out_ports]
        out = target_dp.ofproto_parser.OFPPacketOut(
            datapath=target_dp,
            buffer_id=target_dp.ofproto.OFP_NO_BUFFER,
            in_port=target_dp.ofproto.OFPP_CONTROLLER,
            actions=actions, data=msg.data)
        target_dp.send_msg(out)


    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            self._handle_arp(msg, pkt, eth)
            return

        if eth.ethertype == ether_types.ETH_TYPE_IP:
            self._handle_ip(msg, pkt, eth)
            return

        # Restrict routing to ARP/IP packets; drop anything else.
