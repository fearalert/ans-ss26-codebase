#!/usr/bin/env python3

"""
ECMP (Equal-Cost Multi-Path) routing for the k=4 fat-tree.

Bonus task — extends the two-level routing scheme so that ALL (k/2)^2
equal-cost paths between pods are utilised simultaneously, rather than
the k/2 paths that the deterministic suffix table covers.

Design
------
The two-level (FTRouter) scheme already assigns each flow to one of the
equal-cost paths by hashing on the destination host-id (last IP octet).
With k=4 that gives k/2 = 2 active core switches out of (k/2)^2 = 4.

ECMP generalises this by hashing on the *full* 5-tuple
(src-IP, dst-IP, src-port, dst-port, protocol) so that different flows
to the *same* destination can take *different* paths.  We achieve this
with OpenFlow 1.3 **group tables** (type SELECT): OVS applies an
internal hash of the packet header to choose one bucket (= one output
port) from the group, giving per-flow consistent path selection without
per-flow controller state.

Flow-table layout (per switch, in decreasing priority)
-------------------------------------------------------
Priority 40 — /32 host delivery (same as FTRouter, lazy on ARP learn)
Priority 30 — /24 pod-prefix (same as FTRouter, intra-pod downward)
Priority 20 — /16 pod-block prefix on core switches (same as FTRouter)
Priority 10 — ECMP suffix group rules on edge and aggregation switches
               (replaces FTRouter's single-port suffix flows)

At priority 10, instead of a single OFPActionOutput we install a
reference to an OFP GROUP entry (type SELECT, one bucket per upward
neighbour).  OVS's SELECT group hashes src-IP XOR dst-IP (and optionally
transport ports via the Nicira hash extension) to pick a bucket
deterministically per flow.

Group-table management
----------------------
Each (switch, set-of-ports) combination needs exactly one SELECT group.
We create groups lazily the first time _install_structural_flows() runs
and reuse them on subsequent calls (which can happen if topology
discovery fires more than once).  A dict  self._groups  maps
(dpid, frozenset-of-ports) -> group_id.

Result
------
- Edge switches: both aggregation ports are in one SELECT group
  -> flows with different (src,dst) pairs are spread across both agg
     neighbours rather than pinned to one by the destination host-id.
- Aggregation switches: both core ports are in one SELECT group per
  suffix entry -> all four core switches can be reached.
- Core switches: unchanged (one /16 prefix per pod; no branching there).

With k=4 this activates all 4 core switches for inter-pod traffic (vs.
FTRouter's 2 and SPRouter's 1), achieving full bisection bandwidth for
diverse traffic mixes.
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, ipv4, arp

from ryu.topology import event
from ryu.topology.api import get_switch, get_link
from ryu.app.wsgi import ControllerBase

import topo


# Flow priorities — identical to ft_routing so the two files are comparable
PRIORITY_HOST         = 40
PRIORITY_POD_PREFIX   = 30
PRIORITY_PODBLOCK_PREFIX = 20
PRIORITY_SUFFIX       = 10


class ECMPRouter(app_manager.RyuApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(ECMPRouter, self).__init__(*args, **kwargs)

        self.topo_net = topo.Fattree(4)
        self.k = 4

        # Same dpid<->node mapping as sp_routing / ft_routing:
        # switch s(i+1) has dpid i+1 and corresponds to topo_net.switches[i].
        self.dpid_to_node = {}
        self.node_id_to_dpid = {}
        for i, switch in enumerate(self.topo_net.switches):
            dpid = i + 1
            self.dpid_to_node[dpid] = switch
            self.node_id_to_dpid[switch.id] = dpid

        self.datapaths   = {}   # dpid -> Datapath
        self.switch_ports = {}  # dpid -> set(port_no)
        self.adjacency   = {}   # dpid -> {neighbor_dpid: local_port}
        self.host_location = {} # host_ip -> (dpid, port)
        self.host_mac    = {}   # host_ip -> MAC string

        # (dpid, frozenset(ports)) -> group_id
        # Tracks SELECT groups already installed so we never send OFPGC_ADD
        # for the same (switch, port-set) twice.
        self._groups = {}
        self._next_group_id = 1  # monotonically increasing group-id counter


    # Topology discovery


    @set_ev_cls(event.EventSwitchEnter)
    def get_topology_data(self, ev):
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

        self._install_structural_flows()


    # Switch connect
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto  = datapath.ofproto
        parser   = datapath.ofproto_parser

        self.datapaths[datapath.id] = datapath

        # Table-miss: send unknown packets to controller
        match   = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)


    # Flow / group helpers


    def add_flow(self, datapath, priority, match, actions):
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                              actions)]
        mod  = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                  match=match, instructions=inst)
        datapath.send_msg(mod)

    def add_flow_goto_group(self, datapath, priority, match, group_id):
        """Install a flow entry whose action is 'go to group group_id'."""
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser
        actions = [parser.OFPActionGroup(group_id=group_id)]
        inst    = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                                 actions)]
        mod     = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                     match=match, instructions=inst)
        datapath.send_msg(mod)

    def get_or_create_select_group(self, datapath, ports):
        """Return the group_id of a SELECT group covering exactly `ports`.

        Creates the group (OFPGC_ADD) if it does not exist yet for this
        datapath.  If only one port is provided we fall back to a plain
        output action (no group table entry needed).
        """
        key = (datapath.id, frozenset(ports))
        if key in self._groups:
            return self._groups[key]

        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser

        gid = self._next_group_id
        self._next_group_id += 1

        buckets = []
        for port in sorted(ports):
            actions = [parser.OFPActionOutput(port)]
            bucket  = parser.OFPBucket(
                weight     = 1,
                watch_port = port,
                watch_group= ofproto.OFPG_ANY,
                actions    = actions,
            )
            buckets.append(bucket)

        group_mod = parser.OFPGroupMod(
            datapath = datapath,
            command  = ofproto.OFPGC_ADD,
            type_    = ofproto.OFPGT_SELECT,
            group_id = gid,
            buckets  = buckets,
        )
        datapath.send_msg(group_mod)

        self._groups[key] = gid
        return gid


    # Topology helpers  (identical to ft_routing)


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

    def _host_ports(self, dpid):
        all_ports  = self.switch_ports.get(dpid, set())
        link_ports = set(self.adjacency.get(dpid, {}).values())
        return all_ports - link_ports

    def _edge_dpid_for_ip(self, ip):
        parts = ip.split('.')
        if len(parts) != 4:
            return None
        node_id = '10.{}.{}.1'.format(parts[1], parts[2])
        return self.node_id_to_dpid.get(node_id)


    # Structural flow installation — ECMP variant


    def _install_structural_flows(self):
        """Install prefix and ECMP-suffix flows on every switch.

        Core switches: same /16 prefix rules as FTRouter (no branching
        needed at the top of the tree).

        Aggregation and edge switches: for each possible destination
        host-id h, install a suffix-match flow that points to a SELECT
        GROUP containing ALL upward ports rather than just one. OVS will
        hash the 5-tuple to pick one port per flow, spreading traffic
        evenly across all equal-cost paths.
        """
        half = self.k // 2

        for i, node in enumerate(self.topo_net.switches):
            dpid = i + 1
            dp   = self.datapaths.get(dpid)
            if dp is None:
                continue
            parser = dp.ofproto_parser

            if node.type == 'edge':
                agg_neighbors = self._neighbors(node, dpid, 'agg')
                agg_neighbors.sort(key=lambda t: int(t[0].id.split('.')[2]))
                self._install_ecmp_suffix_flows(dp, parser, agg_neighbors, half)

            elif node.type == 'agg':
                pod = node.id.split('.')[1]

                # /24 intra-pod prefix entries (unchanged from FTRouter)
                for other, other_dpid, port in self._neighbors(node, dpid, 'edge'):
                    if port is None:
                        continue
                    edge_idx  = other.id.split('.')[2]
                    prefix_ip = '10.{}.{}.0'.format(pod, edge_idx)
                    match = parser.OFPMatch(
                        eth_type  = ether_types.ETH_TYPE_IP,
                        ipv4_dst  = (prefix_ip, '255.255.255.0'))
                    self.add_flow(dp, PRIORITY_POD_PREFIX, match,
                                  [parser.OFPActionOutput(port)])

                # ECMP suffix entries toward all core neighbours
                core_neighbors = self._neighbors(node, dpid, 'core')
                core_neighbors.sort(key=lambda t: int(t[0].id.split('.')[2]))
                self._install_ecmp_suffix_flows(dp, parser, core_neighbors, half)

            elif node.type == 'core':
                # /16 pod-block prefix entries (unchanged from FTRouter)
                for other, other_dpid, port in self._neighbors(node, dpid, 'agg'):
                    if port is None:
                        continue
                    pod       = other.id.split('.')[1]
                    prefix_ip = '10.{}.0.0'.format(pod)
                    match = parser.OFPMatch(
                        eth_type = ether_types.ETH_TYPE_IP,
                        ipv4_dst = (prefix_ip, '255.255.0.0'))
                    self.add_flow(dp, PRIORITY_PODBLOCK_PREFIX, match,
                                  [parser.OFPActionOutput(port)])

    def _install_ecmp_suffix_flows(self, dp, parser, up_neighbors, half):
        """Install one ECMP suffix flow per host-id h.

        For each h, collect ALL upward ports (not just one) and put them
        in a SELECT group so OVS can hash among them.  If only one port
        is available (e.g. topology not yet fully discovered) we fall
        back to a plain output action.
        """
        if not up_neighbors:
            return

        # Collect every upward port that is currently known
        all_up_ports = [port for _node, _dpid, port in up_neighbors
                        if port is not None]
        if not all_up_ports:
            return

        if len(all_up_ports) == 1:
            # Only one path available: plain output, no group needed
            port = all_up_ports[0]
            for h in range(2, half + 2):
                match = parser.OFPMatch(
                    eth_type = ether_types.ETH_TYPE_IP,
                    ipv4_dst = ('0.0.0.{}'.format(h), '0.0.0.255'))
                self.add_flow(dp, PRIORITY_SUFFIX, match,
                              [parser.OFPActionOutput(port)])
        else:
            # Multiple equal-cost paths: one SELECT group shared by all h
            gid = self.get_or_create_select_group(dp, all_up_ports)
            for h in range(2, half + 2):
                match = parser.OFPMatch(
                    eth_type = ether_types.ETH_TYPE_IP,
                    ipv4_dst = ('0.0.0.{}'.format(h), '0.0.0.255'))
                self.add_flow_goto_group(dp, PRIORITY_SUFFIX, match, gid)


    # Host learning  (identical to ft_routing)


    def _learn_host(self, dpid, port, ip, mac):
        if ip not in self.host_location:
            self.host_location[ip] = (dpid, port)
            self.host_mac[ip]      = mac

            dp = self.datapaths.get(dpid)
            if dp is not None:
                parser = dp.ofproto_parser
                match  = parser.OFPMatch(
                    eth_type = ether_types.ETH_TYPE_IP,
                    ipv4_dst = ip)
                actions = [parser.OFPActionOutput(port)]
                self.add_flow(dp, PRIORITY_HOST, match, actions)


    # ARP handling  (identical to ft_routing)


    def _send_arp_reply(self, datapath, port, target_ip, target_mac,
                        req_ip, req_mac):
        parser = datapath.ofproto_parser
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(
            ethertype = ether_types.ETH_TYPE_ARP,
            dst = req_mac, src = target_mac))
        pkt.add_protocol(arp.arp(
            opcode  = arp.ARP_REPLY,
            src_mac = target_mac, src_ip = target_ip,
            dst_mac = req_mac,    dst_ip = req_ip))
        pkt.serialize()
        actions = [parser.OFPActionOutput(port)]
        out = parser.OFPPacketOut(
            datapath   = datapath,
            buffer_id  = datapath.ofproto.OFP_NO_BUFFER,
            in_port    = datapath.ofproto.OFPP_CONTROLLER,
            actions    = actions,
            data       = pkt.data)
        datapath.send_msg(out)

    def _handle_arp(self, msg, pkt, eth):
        datapath = msg.datapath
        dpid     = datapath.id
        in_port  = msg.match['in_port']

        arp_pkt = pkt.get_protocol(arp.arp)
        self._learn_host(dpid, in_port, arp_pkt.src_ip, eth.src)

        if arp_pkt.opcode == arp.ARP_REQUEST:
            target_ip = arp_pkt.dst_ip
            if target_ip in self.host_mac:
                self._send_arp_reply(datapath, in_port,
                                     target_ip, self.host_mac[target_ip],
                                     arp_pkt.src_ip, eth.src)
                return

            dst_edge = self._edge_dpid_for_ip(target_ip)
            if dst_edge is None or dst_edge not in self.datapaths:
                return

            target_dp  = self.datapaths[dst_edge]
            out_ports  = self._host_ports(dst_edge)
            if dst_edge == dpid:
                out_ports = out_ports - {in_port}
            if not out_ports:
                return

            actions = [target_dp.ofproto_parser.OFPActionOutput(p)
                       for p in out_ports]
            out = target_dp.ofproto_parser.OFPPacketOut(
                datapath  = target_dp,
                buffer_id = target_dp.ofproto.OFP_NO_BUFFER,
                in_port   = target_dp.ofproto.OFPP_CONTROLLER,
                actions   = actions,
                data      = msg.data)
            target_dp.send_msg(out)

        else:
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


    # IP fallback  (identical to ft_routing)


    def _handle_ip(self, msg, pkt, eth):
        datapath = msg.datapath
        dpid     = datapath.id
        in_port  = msg.match['in_port']

        ip_pkt         = pkt.get_protocol(ipv4.ipv4)
        src_ip, dst_ip = ip_pkt.src, ip_pkt.dst

        self._learn_host(dpid, in_port, src_ip, eth.src)
        self._install_structural_flows()

        dst_edge = self._edge_dpid_for_ip(dst_ip)
        if dst_edge is None or dst_edge not in self.datapaths:
            return

        loc = self.host_location.get(dst_ip)
        if loc is not None:
            return  # proper flow now installed; drop this in-flight packet

        target_dp = self.datapaths[dst_edge]
        out_ports = self._host_ports(dst_edge)
        if dst_edge == dpid:
            out_ports = out_ports - {in_port}
        if not out_ports:
            return

        actions = [target_dp.ofproto_parser.OFPActionOutput(p)
                   for p in out_ports]
        out = target_dp.ofproto_parser.OFPPacketOut(
            datapath  = target_dp,
            buffer_id = target_dp.ofproto.OFP_NO_BUFFER,
            in_port   = target_dp.ofproto.OFPP_CONTROLLER,
            actions   = actions,
            data      = msg.data)
        target_dp.send_msg(out)


    # Packet-in dispatcher


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