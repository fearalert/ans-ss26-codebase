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

"""
Standalone correctness/sanity-check and visualization tool for topo.Fattree.

For a range of fat-tree sizes (k = num_ports) it:
  - Builds the graph with topo.Fattree(k)
  - Checks that the number of switches/hosts of each type matches the
    counts derived from the fat-tree paper (Section 2/3.2)
  - Checks that every node has the expected degree
  - Checks that the number of links of each type (edge-host, edge-agg,
    agg-core) matches the expected counts
  - For small k, draws a layered plot of the topology (core / aggregation
    / edge / hosts) to PNG files under topology_plots/

Run with:
    .venv/bin/python3 verify_topo.py
"""

import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import networkx as nx

import topo


def build_nx_graph(ft):
    """Convert a topo.Fattree instance into a networkx.Graph."""
    g = nx.Graph()

    for node in ft.switches + ft.servers:
        g.add_node(node.id, type=node.type)

    seen_edges = set()
    for node in ft.switches + ft.servers:
        for edge in node.edges:
            if id(edge) in seen_edges:
                continue
            seen_edges.add(id(edge))
            g.add_edge(edge.lnode.id, edge.rnode.id)

    return g


def link_type_counts(ft):
    """Count links by (type(lnode), type(rnode)) pair, deduplicated."""
    seen_edges = set()
    counts = {}
    for node in ft.switches + ft.servers:
        for edge in node.edges:
            if id(edge) in seen_edges:
                continue
            seen_edges.add(id(edge))
            t = tuple(sorted((edge.lnode.type, edge.rnode.type)))
            counts[t] = counts.get(t, 0) + 1
    return counts


def sanity_check(k):
    """Run the sanity checks for a fat-tree with num_ports = k.

    Returns (ok, report_lines).
    """
    ft = topo.Fattree(k)

    expected_core = (k // 2) ** 2
    expected_edge = k * (k // 2)
    expected_agg = k * (k // 2)
    expected_hosts = k ** 3 // 4

    actual_core = sum(1 for s in ft.switches if s.type == 'core')
    actual_edge = sum(1 for s in ft.switches if s.type == 'edge')
    actual_agg = sum(1 for s in ft.switches if s.type == 'agg')
    actual_hosts = len(ft.servers)

    checks = []
    checks.append(("core switches", actual_core, expected_core))
    checks.append(("edge switches", actual_edge, expected_edge))
    checks.append(("agg switches", actual_agg, expected_agg))
    checks.append(("hosts", actual_hosts, expected_hosts))

    # Degree checks: every switch should have degree k, every host degree 1
    bad_degrees = []
    for s in ft.switches:
        if len(s.edges) != k:
            bad_degrees.append((s.id, s.type, len(s.edges), k))
    for h in ft.servers:
        if len(h.edges) != 1:
            bad_degrees.append((h.id, h.type, len(h.edges), 1))

    # Link type counts: edge-host == edge-agg == agg-core == k^3/4
    counts = link_type_counts(ft)
    expected_link = k ** 3 // 4
    link_checks = []
    for pair, label in [(('edge', 'host'), 'edge-host'),
                         (('agg', 'edge'), 'edge-agg'),
                         (('agg', 'core'), 'agg-core')]:
        actual = counts.get(pair, 0)
        link_checks.append((label, actual, expected_link))

    ok = all(a == e for _, a, e in checks)
    ok = ok and not bad_degrees
    ok = ok and all(a == e for _, a, e in link_checks)

    lines = []
    lines.append("k = {}".format(k))
    lines.append("  Node counts (actual / expected):")
    for label, actual, expected in checks:
        status = "OK" if actual == expected else "MISMATCH"
        lines.append("    {:14s}: {:4d} / {:4d}  [{}]".format(label, actual, expected, status))

    lines.append("  Degree checks: switches should have degree {}, hosts degree 1".format(k))
    if bad_degrees:
        for nid, ntype, actual, expected in bad_degrees:
            lines.append("    MISMATCH: node {} ({}) has degree {}, expected {}".format(
                nid, ntype, actual, expected))
    else:
        lines.append("    OK: all {} switches have degree {}, all {} hosts have degree 1".format(
            len(ft.switches), k, len(ft.servers)))

    lines.append("  Link counts (actual / expected = k^3/4 = {}):".format(expected_link))
    for label, actual, expected in link_checks:
        status = "OK" if actual == expected else "MISMATCH"
        lines.append("    {:10s}: {:4d} / {:4d}  [{}]".format(label, actual, expected, status))

    total_links = sum(counts.values())
    lines.append("  Total links: {} (expected {})".format(total_links, 3 * expected_link))

    lines.append("  Overall: {}".format("PASS" if ok else "FAIL"))
    lines.append("")

    return ok, lines


def plot_topology(k, out_dir):
    """Draw a layered plot (core/agg/edge/hosts) of a fat-tree with the
    given k and save it to out_dir/fattree_k<k>.png.
    """
    ft = topo.Fattree(k)
    g = build_nx_graph(ft)

    layer_y = {'core': 3, 'agg': 2, 'edge': 1, 'host': 0}
    layer_color = {'core': 'tab:red', 'agg': 'tab:orange',
                    'edge': 'tab:green', 'host': 'tab:blue'}

    # Group nodes by type, preserving the order in which Fattree.generate()
    # created them (this keeps switches belonging to the same pod next to
    # each other, and hosts under their edge switch).
    by_type = {'core': [], 'agg': [], 'edge': [], 'host': []}
    for s in ft.switches:
        by_type[s.type].append(s.id)
    for h in ft.servers:
        by_type['host'].append(h.id)

    pos = {}
    for ntype, ids in by_type.items():
        n = len(ids)
        for i, node_id in enumerate(ids):
            # Center the row of nodes for this layer, scale x so that the
            # widest layer (hosts) spans [0, n-1].
            x = i * (max(len(by_type['host']) - 1, 1) / max(n - 1, 1)) if n > 1 else \
                (max(len(by_type['host']) - 1, 1) / 2.0)
            pos[node_id] = (x, layer_y[ntype])

    fig, ax = plt.subplots(figsize=(max(6, len(by_type['host']) * 0.6), 6))

    for ntype, ids in by_type.items():
        nx.draw_networkx_nodes(g, pos, nodelist=ids, node_color=layer_color[ntype],
                                node_size=250 if ntype != 'host' else 120,
                                label=ntype, ax=ax)

    nx.draw_networkx_edges(g, pos, alpha=0.4, ax=ax)

    if k <= 4:
        nx.draw_networkx_labels(g, pos, font_size=6, ax=ax)

    ax.set_title("Fat-tree topology (k = {})".format(k))
    ax.legend(scatterpoints=1, loc='upper center', bbox_to_anchor=(0.5, -0.02), ncol=4)
    ax.axis('off')
    ax.margins(0.08)

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'fattree_k{}.png'.format(k))
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    return out_path


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, 'topology_plots')
    report_path = os.path.join(here, 'topology_report.txt')

    report_lines = []
    report_lines.append("Fat-tree topology generation: sanity-check report")
    report_lines.append("=" * 55)
    report_lines.append("")

    all_ok = True
    for k in (2, 4, 6, 8):
        ok, lines = sanity_check(k)
        all_ok = all_ok and ok
        report_lines.extend(lines)

    report_lines.append("All checks passed: {}".format(all_ok))

    report_text = "\n".join(report_lines)
    print(report_text)

    with open(report_path, 'w') as f:
        f.write(report_text + "\n")

    # Only plot small topologies - larger ones become unreadable.
    for k in (2, 4):
        path = plot_topology(k, out_dir)
        print("Wrote plot:", path)


if __name__ == '__main__':
    main()
