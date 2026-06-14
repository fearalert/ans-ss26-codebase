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

class SPRouter(app_manager.RyuApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(SPRouter, self).__init__(*args, **kwargs)

        # Initialize the topology with #ports=4
        self.topo_net = topo.Fattree(4)

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
        # and host IP -> MAC, both learned at runtime from ARP/IP traffic
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
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                match=match, instructions=inst)
        datapath.send_msg(mod)


    # Ports of a switch that are NOT used for inter-switch links, i.e. the
    # ports that (may) connect to hosts. Used both for proxy-ARP flooding
    # and as the fallback output set for an edge switch's hosts.
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


    # Record where a host with the given IP/MAC is attached, the first time
    # we see traffic from it.
    def _learn_host(self, dpid, port, ip, mac):
        if ip not in self.host_location:
            self.host_location[ip] = (dpid, port)
            self.host_mac[ip] = mac


    # Self-implemented Dijkstra's algorithm over the switch-level adjacency
    # graph (all links have equal weight). Returns the list of dpids on a
    # shortest path from src to dst (both inclusive), or None if unreachable.
    def _shortest_path(self, src, dst):
        nodes = set(self.adjacency.keys())
        nodes.add(src)
        nodes.add(dst)

        dist = {n: float('inf') for n in nodes}
        prev = {}
        dist[src] = 0
        unvisited = set(nodes)

        while unvisited:
            cur = min(unvisited, key=lambda n: dist[n])
            if dist[cur] == float('inf'):
                break
            unvisited.remove(cur)
            if cur == dst:
                break
            for neigh, _port in self.adjacency.get(cur, {}).items():
                alt = dist[cur] + 1
                if alt < dist[neigh]:
                    dist[neigh] = alt
                    prev[neigh] = cur

        if dist.get(dst, float('inf')) == float('inf'):
            return None

        path = [dst]
        while path[-1] != src:
            path.append(prev[path[-1]])
        path.reverse()
        return path


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


    # Handle an ARP packet: learn the sender's location/MAC, and either
    # proxy-reply directly (if we already know the target) or flood the
    # request towards the target's edge switch so it can reply for real.
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

            actions = [parser.OFPActionOutput(p) for p in out_ports]
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


    # Handle an IPv4 packet: compute the shortest path from the switch where
    # the packet entered the network towards the destination's edge switch,
    # install matching flow entries along the way, and forward this packet.
    def _handle_ip(self, msg, pkt, eth):
        datapath = msg.datapath
        dpid = datapath.id
        in_port = msg.match['in_port']
        parser = datapath.ofproto_parser

        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        src_ip, dst_ip = ip_pkt.src, ip_pkt.dst

        self._learn_host(dpid, in_port, src_ip, eth.src)

        dst_edge = self._edge_dpid_for_ip(dst_ip)
        if dst_edge is None:
            return

        path = self._shortest_path(dpid, dst_edge)
        if path is None:
            return

        for idx, cur_dpid in enumerate(path):
            cur_dp = self.datapaths.get(cur_dpid)
            if cur_dp is None:
                continue
            cur_parser = cur_dp.ofproto_parser

            if idx < len(path) - 1:
                next_dpid = path[idx + 1]
                out_port = self.adjacency.get(cur_dpid, {}).get(next_dpid)
            else:
                loc = self.host_location.get(dst_ip)
                out_port = loc[1] if loc else None

            if out_port is not None:
                match = cur_parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                             ipv4_dst=dst_ip)
                actions = [cur_parser.OFPActionOutput(out_port)]
                self.add_flow(cur_dp, 10, match, actions)

            if cur_dpid == dpid:
                if out_port is not None:
                    out_actions = [parser.OFPActionOutput(out_port)]
                else:
                    candidates = self._host_ports(dpid) - {in_port}
                    out_actions = [parser.OFPActionOutput(p) for p in candidates]
                if not out_actions:
                    continue
                data = msg.data if msg.buffer_id == datapath.ofproto.OFP_NO_BUFFER else None
                out = parser.OFPPacketOut(datapath=datapath,
                                           buffer_id=msg.buffer_id,
                                           in_port=in_port,
                                           actions=out_actions, data=data)
                datapath.send_msg(out)


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
