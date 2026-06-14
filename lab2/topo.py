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

# Class for an edge in the graph
class Edge:
	def __init__(self):
		self.lnode = None
		self.rnode = None
	
	def remove(self):
		self.lnode.edges.remove(self)
		self.rnode.edges.remove(self)
		self.lnode = None
		self.rnode = None

# Class for a node in the graph
class Node:
	def __init__(self, id, type):
		self.edges = []
		self.id = id
		self.type = type

	# Add an edge connected to another node
	def add_edge(self, node):
		edge = Edge()
		edge.lnode = self
		edge.rnode = node
		self.edges.append(edge)
		node.edges.append(edge)
		return edge

	# Remove an edge from the node
	def remove_edge(self, edge):
		self.edges.remove(edge)

	# Decide if another node is a neighbor
	def is_neighbor(self, node):
		for edge in self.edges:
			if edge.lnode == node or edge.rnode == node:
				return True
		return False


class Fattree:

	def __init__(self, num_ports):
		self.servers = []
		self.switches = []
		self.generate(num_ports)

	def generate(self, num_ports):

		k = num_ports
		if k % 2 != 0:
			raise ValueError("num_ports (k) must be an even number")

		num_pods = k
		num_core = (k // 2) ** 2
		num_agg_per_pod = k // 2
		num_edge_per_pod = k // 2
		num_hosts_per_edge = k // 2

		# Core switches are addressed 10.k.j.i, with j, i in [1, k/2]
		# (j identifies the "row" of the core switch grid, i its "column").
		core_switches = {}
		for j in range(1, k // 2 + 1):
			for i in range(1, k // 2 + 1):
				core = Node('10.{}.{}.{}'.format(k, j, i), 'core')
				self.switches.append(core)
				core_switches[(j, i)] = core

		for pod in range(num_pods):

			# Edge (lower-layer) switches occupy positions [0, k/2 - 1]
			# within a pod and are addressed 10.pod.switch.1
			edge_switches = []
			for s in range(num_edge_per_pod):
				edge = Node('10.{}.{}.1'.format(pod, s), 'edge')
				self.switches.append(edge)
				edge_switches.append(edge)

			# Aggregation (upper-layer) switches occupy positions
			# [k/2, k - 1] within a pod and are addressed 10.pod.switch.1
			agg_switches = []
			for s in range(num_agg_per_pod, k):
				agg = Node('10.{}.{}.1'.format(pod, s), 'agg')
				self.switches.append(agg)
				agg_switches.append(agg)

			# Every edge switch connects to every aggregation switch
			# within the same pod
			for edge in edge_switches:
				for agg in agg_switches:
					edge.add_edge(agg)

			# Aggregation switch at relative position a (0-indexed) within
			# its pod connects to core switch (j, a + 1) for every
			# j in [1, k/2], so that consecutive aggregation switches in a
			# pod fan out across the k/2 "rows" of core switches.
			for a, agg in enumerate(agg_switches):
				for j in range(1, k // 2 + 1):
					agg.add_edge(core_switches[(j, a + 1)])

			# Each edge switch connects to k/2 hosts, addressed
			# 10.pod.switch.ID with ID in [2, k/2 + 1]
			for s, edge in enumerate(edge_switches):
				for host_id in range(2, num_hosts_per_edge + 2):
					host = Node('10.{}.{}.{}'.format(pod, s, host_id), 'host')
					self.servers.append(host)
					edge.add_edge(host)