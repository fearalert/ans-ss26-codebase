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

import os
import subprocess
import time

import mininet
import mininet.clean
from mininet.net import Mininet
from mininet.cli import CLI
from mininet.log import lg, info
from mininet.link import TCLink
from mininet.node import Node, OVSKernelSwitch, RemoteController
from mininet.topo import Topo
from mininet.util import waitListening, custom

import topo


class FattreeNet(Topo):
    """
    Create a fat-tree network in Mininet
    """

    def __init__(self, ft_topo):

        Topo.__init__(self)

        # Map graph nodes (from topo.Fattree) to Mininet node names.
        # Switches are named s1..sN in the same order as ft_topo.switches,
        # so that the dpid Mininet derives from the name ("sN" -> N)
        # corresponds to the index (1-based) of the switch in ft_topo.switches.
        # This lets the controller recover the addressing (and hence the
        # role/position) of every switch from its dpid alone.
        node_to_name = {}

        for i, switch in enumerate(ft_topo.switches):
            name = 's{}'.format(i + 1)
            self.addSwitch(name, cls=OVSKernelSwitch, protocols='OpenFlow13')
            node_to_name[switch] = name

        # Hosts get the IP address dictated by the fat-tree addressing
        # scheme (10.pod.switch.id), with a /8 mask so that every host
        # appears to be on the same (flat) subnet and always ARPs
        # directly for the destination host.
        for i, host in enumerate(ft_topo.servers):
            name = 'h{}'.format(i + 1)
            self.addHost(name, ip='{}/8'.format(host.id))
            node_to_name[host] = name

        # Add links. Every Edge object is referenced from both endpoints'
        # edge lists, so deduplicate on the Edge object's identity.
        seen_edges = set()
        for node in ft_topo.switches + ft_topo.servers:
            for edge in node.edges:
                if id(edge) in seen_edges:
                    continue
                seen_edges.add(id(edge))
                name1 = node_to_name[edge.lnode]
                name2 = node_to_name[edge.rnode]
                self.addLink(name1, name2, cls=TCLink, bw=15, delay='5ms')


def make_mininet_instance(graph_topo):

    net_topo = FattreeNet(graph_topo)
    net = Mininet(topo=net_topo, controller=None, autoSetMacs=True)
    net.addController('c0', controller=RemoteController,
                      ip="127.0.0.1", port=6653)
    return net


def run(graph_topo):

    # Run the Mininet CLI with a given topology
    lg.setLogLevel('info')
    # mininet.clean.cleanup()
    net = make_mininet_instance(graph_topo)

    info('*** Starting network ***\n')
    net.start()
    info('*** Running CLI ***\n')
    CLI(net)
    info('*** Stopping network ***\n')
    net.stop()
    mininet.clean.cleanup()


if __name__ == '__main__':
    ft_topo = topo.Fattree(4)
    run(ft_topo)
