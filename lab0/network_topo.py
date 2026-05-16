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

#!/usr/bin/python

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import OVSSwitch
from mininet.link import TCLink
from mininet.log import setLogLevel, info

class BridgeTopo(Topo):
    "Creat a bridge-like customized network topology according to Figure 1 in the lab0 description."

    def __init__(self):

        Topo.__init__(self)


        # adding switches, s1 and s2

        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')

        # adding hosts h1 - h4
        # also specify the ip to match the 10.0.0.x requirements, where x should match the host number (1 − 4), e.g., 10.0.0.2 for h2
        h1 = self.addHost('h1', ip='10.0.0.1')
        h2 = self.addHost('h2', ip='10.0.0.2')
        h3 = self.addHost('h3', ip='10.0.0.3')
        h4 = self.addHost('h4', ip='10.0.0.4')

        # adding links
        #Link between host and switch
        self.addLink(h1, s1, bw=15, delay='10ms')
        self.addLink(h2, s1, bw=15, delay='10ms')
        self.addLink(h3, s2, bw=15, delay='10ms')
        self.addLink(h4, s2, bw=15, delay='10ms')

        #Link between switch to switch
        self.addLink(s1, s2, bw=20, delay='45ms')



topos = {'bridge': (lambda: BridgeTopo())}

def runExperiment():
    "Create and test the network"
    topo = BridgeTopo()
    
    # Initialize the network with TCLink for bandwidth/delay and OVS
    net = Mininet(topo=topo, link=TCLink, switch=OVSSwitch, controller=None)

    info('*** Starting network\n')
    net.start()

    info('*** Testing connectivity\n')
    net.pingAll()

    info('*** Running Iperf between h1 and h3\n')
    h1, h3 = net.get('h1', 'h3')
    
    # This runs a TCP iperf test and prints results to console
    net.iperf((h1, h3))

    info('*** Stopping network\n')
    net.stop()


if __name__ == '__main__':
    # Set log level to see what Mininet is doing
    setLogLevel('info')
    runExperiment()