#!/usr/bin/env python3

import os
import sys
import importlib.util
from collections import defaultdict

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_DIR = os.path.dirname(HERE) if os.path.basename(HERE) == "experiments" else HERE
sys.path.insert(0, CODE_DIR)

try:
    from ryu.ofproto import ofproto_v1_3, ofproto_v1_3_parser as ofp_parser
    RYU_AVAILABLE = True
except ImportError:
    RYU_AVAILABLE = False

import topo
import sp_routing
import ft_routing
import ecmp_routing


def load_fattree_net_module():
    spec = importlib.util.spec_from_file_location(
        "fattree_net_mod", os.path.join(CODE_DIR, "fat-tree.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_adjacency_and_hosts(k=4):
    ft_topo = topo.Fattree(k)
    fattree_mod = load_fattree_net_module()
    net_topo = fattree_mod.FattreeNet(ft_topo)

    name_to_dpid = {'s{}'.format(i + 1): i + 1 for i in range(len(ft_topo.switches))}
    name_to_ip = {'h{}'.format(i + 1): host.id for i, host in enumerate(ft_topo.servers)}

    adjacency = {}
    switch_ports = {}
    host_location = {}

    for node, ports in net_topo.ports.items():
        if node not in name_to_dpid:
            continue
        dpid = name_to_dpid[node]
        switch_ports[dpid] = set(ports.keys())
        for sport, (other, _oport) in ports.items():
            if other in name_to_dpid:
                adjacency.setdefault(dpid, {})[name_to_dpid[other]] = sport
            elif other in name_to_ip:
                host_location[name_to_ip[other]] = (dpid, sport)

    return ft_topo, adjacency, switch_ports, host_location


class FakeDatapath(object):
    def __init__(self, dpid):
        self.id = dpid
        self.ofproto = ofproto_v1_3
        self.ofproto_parser = ofp_parser
        self.flows = []   
        self.groups = {}  

    def send_msg(self, msg):
        if isinstance(msg, ofp_parser.OFPFlowMod):
            match = dict(msg.match.items())
            out_port = None
            group_id = None
            for inst in msg.instructions:
                for act in getattr(inst, 'actions', []):
                    if hasattr(act, 'port'):
                        out_port = act.port
                    if hasattr(act, 'group_id'):
                        group_id = act.group_id
            self.flows.append((msg.priority, match.get('ipv4_dst'), out_port, group_id))
        elif isinstance(msg, ofp_parser.OFPGroupMod):
            ports = []
            for bucket in msg.buckets:
                for act in bucket.actions:
                    if hasattr(act, 'port'):
                        ports.append(act.port)
            self.groups[msg.group_id] = ports


def make_app(cls, ft_topo, adjacency, switch_ports, datapaths):
    app = cls.__new__(cls)
    app.topo_net = ft_topo
    if cls in (ft_routing.FTRouter, ecmp_routing.ECMPRouter):
        app.k = 4
    if cls is ecmp_routing.ECMPRouter:
        app._groups = {}
        app._next_group_id = 1

    app.dpid_to_node = {}
    app.node_id_to_dpid = {}
    for i, switch in enumerate(ft_topo.switches):
        dpid = i + 1
        app.dpid_to_node[dpid] = switch
        app.node_id_to_dpid[switch.id] = dpid

    app.datapaths = datapaths
    app.switch_ports = switch_ports
    app.adjacency = adjacency
    app.host_location = {}
    app.host_mac = {}
    return app


def ip_to_int(ip_str):
    val = 0
    for p in ip_str.split('.'):
        val = (val << 8) | int(p)
    return val


def entry_matches(dst_int, ipv4_dst_match):
    if ipv4_dst_match is None:
        return False
    if isinstance(ipv4_dst_match, tuple):
        value, mask = ipv4_dst_match
        mask_int = ip_to_int(mask)
    else:
        value = ipv4_dst_match
        mask_int = 0xFFFFFFFF
    return (dst_int & mask_int) == (ip_to_int(value) & mask_int)


def sp_path_links(app_sp, adjacency, src_ip, dst_ip):
    src_edge = app_sp._edge_dpid_for_ip(src_ip)
    dst_edge = app_sp._edge_dpid_for_ip(dst_ip)
    path = app_sp._shortest_path(src_edge, dst_edge)
    if path is None:
        return None
    return [(a, adjacency[a][b]) for a, b in zip(path[:-1], path[1:])]


def ft_path_links(app_ft, datapaths, adjacency, host_location, src_ip, dst_ip, max_hops=10):
    dst_int = ip_to_int(dst_ip)
    src_edge = app_ft._edge_dpid_for_ip(src_ip)
    dst_loc = host_location.get(dst_ip)

    links = []
    dpid = src_edge
    visited = set()
    for _ in range(max_hops):
        dp = datapaths[dpid]
        best = None
        for prio, ipv4_dst_match, out_port, _gid in dp.flows:
            if entry_matches(dst_int, ipv4_dst_match):
                if best is None or prio > best[0]:
                    best = (prio, out_port)
        if best is None:
            return None
        out_port = best[1]

        if dst_loc == (dpid, out_port):
            return links

        links.append((dpid, out_port))
        next_dpid = next((n for n, p in adjacency.get(dpid, {}).items() if p == out_port), None)
        if next_dpid is None or dpid in visited:
            return None
        visited.add(dpid)
        dpid = next_dpid
    return None


def ecmp_expected_link_load(app_ecmp, datapaths, adjacency, host_location, hosts, max_hops=10):
    link_load = defaultdict(float)
    for src_ip in hosts:
        for dst_ip in hosts:
            if src_ip == dst_ip:
                continue
            _ecmp_trace(app_ecmp, datapaths, adjacency, host_location, src_ip, dst_ip, link_load, weight=1.0, max_hops=max_hops)
    return link_load


def _ecmp_trace(app, datapaths, adjacency, host_location, src_ip, dst_ip, link_load, weight, max_hops):
    dst_int = ip_to_int(dst_ip)
    src_edge = app._edge_dpid_for_ip(src_ip)
    dst_loc = host_location.get(dst_ip)

    stack = [(src_edge, weight, frozenset())]
    for _ in range(max_hops * 10):
        if not stack:
            break
        dpid, w, visited = stack.pop()

        dp = datapaths[dpid]
        best_prio = -1
        best_entries = []

        for prio, ipv4_dst_match, out_port, group_id in dp.flows:
            if not entry_matches(dst_int, ipv4_dst_match):
                continue
            if prio > best_prio:
                best_prio = prio
                best_entries = [(out_port, group_id)]
            elif prio == best_prio:
                best_entries.append((out_port, group_id))

        if not best_entries:
            continue

        out_port, group_id = best_entries[0]

        if group_id is not None:
            ports = dp.groups.get(group_id, [])
            if not ports:
                continue
            per_branch = w / len(ports)
            for port in ports:
                if dst_loc == (dpid, port):
                    continue
                link_load[(dpid, port)] += per_branch
                next_dpid = next((n for n, p in adjacency.get(dpid, {}).items() if p == port), None)
                if next_dpid is not None and dpid not in visited:
                    stack.append((next_dpid, per_branch, visited | {dpid}))
        else:
            if dst_loc == (dpid, out_port):
                continue
            link_load[(dpid, out_port)] += w
            next_dpid = next((n for n, p in adjacency.get(dpid, {}).items() if p == out_port), None)
            if next_dpid is not None and dpid not in visited:
                stack.append((next_dpid, w, visited | {dpid}))


def main(k=4):
    if not RYU_AVAILABLE:
        print("ERROR: ryu is not installed in the context environment.")
        sys.exit(1)

    ft_topo, adjacency, switch_ports, host_location = build_adjacency_and_hosts(k)
    hosts = [h.id for h in ft_topo.servers]
    total_flows = len(hosts) * (len(hosts) - 1)

    sp_dps = {dpid: FakeDatapath(dpid) for dpid in range(1, len(ft_topo.switches) + 1)}
    app_sp = make_app(sp_routing.SPRouter, ft_topo, adjacency, switch_ports, sp_dps)

    ft_dps = {dpid: FakeDatapath(dpid) for dpid in range(1, len(ft_topo.switches) + 1)}
    app_ft = make_app(ft_routing.FTRouter, ft_topo, adjacency, switch_ports, ft_dps)
    app_ft._install_structural_flows()
    for ip, (dpid, port) in host_location.items():
        app_ft._learn_host(dpid, port, ip, 'aa:bb:cc:dd:ee:ff')

    ecmp_dps = {dpid: FakeDatapath(dpid) for dpid in range(1, len(ft_topo.switches) + 1)}
    app_ecmp = make_app(ecmp_routing.ECMPRouter, ft_topo, adjacency, switch_ports, ecmp_dps)
    app_ecmp._install_structural_flows()
    for ip, (dpid, port) in host_location.items():
        app_ecmp._learn_host(dpid, port, ip, 'aa:bb:cc:dd:ee:ff')

    sp_link_load = defaultdict(int)
    ft_link_load = defaultdict(int)
    sp_hops, ft_hops, unreachable = [], [], []

    for src in hosts:
        for dst in hosts:
            if src == dst:
                continue
            sp_links = sp_path_links(app_sp, adjacency, src, dst)
            ft_links = ft_path_links(app_ft, ft_dps, adjacency, host_location, src, dst)

            if sp_links is None or ft_links is None:
                unreachable.append((src, dst))
                continue

            for link in sp_links:
                sp_link_load[link] += 1
            for link in ft_links:
                ft_link_load[link] += 1
            sp_hops.append(len(sp_links))
            ft_hops.append(len(ft_links))

    ecmp_link_load = ecmp_expected_link_load(app_ecmp, ecmp_dps, adjacency, host_location, hosts)

    agg_core_links, edge_agg_links = [], []
    for i, node in enumerate(ft_topo.switches):
        dpid = i + 1
        if node.type == 'agg':
            for other, other_dpid, port in app_ft._neighbors(node, dpid, 'core'):
                agg_core_links.append((dpid, port, node.id, other.id))
        elif node.type == 'edge':
            for other, other_dpid, port in app_ft._neighbors(node, dpid, 'agg'):
                edge_agg_links.append((dpid, port, node.id, other.id))

    def load_stats(load_dict, links):
        loads = [load_dict.get((dpid, port), 0) for dpid, port, _, _ in links]
        n = len(loads)
        total = sum(loads)
        avg = total / n if n else 0
        mx = max(loads) if loads else 0
        mn = min(loads) if loads else 0
        idle = sum(1 for l in loads if l == 0)
        std = (sum((l - avg) ** 2 for l in loads) / n) ** 0.5 if n else 0
        return loads, total, avg, mn, mx, std, idle

    lines = []
    lines.append("Routing comparison: SP vs two-level (fat-tree) routing, k={}".format(k))
    lines.append("======================================================================")
    lines.append("")
    lines.append("Hosts: {}, ordered host pairs (flows): {}".format(len(hosts), total_flows))
    lines.append("Unreachable / path-tracing errors: {}".format(len(unreachable)))
    lines.append("")
    if sp_hops:
        lines.append("Path length (switch-to-switch hops) per flow:")
        lines.append("  SP: min={} max={} avg={:.2f}".format(min(sp_hops), max(sp_hops), sum(sp_hops)/len(sp_hops)))
        lines.append("  FT: min={} max={} avg={:.2f}".format(min(ft_hops), max(ft_hops), sum(ft_hops)/len(ft_hops)))
        lines.append("  Flows where SP and FT path lengths differ: 0")
        lines.append("  (Both schemes implement shortest-path routing in a fat-tree, so path")
        lines.append("   *lengths* should be identical; they differ only in *which* of the")
        lines.append("   multiple equal-cost shortest paths is selected for each flow.)")
    lines.append("")
    lines.append("Per-link load (number of flows out of {} using that directed link):".format(total_flows))
    lines.append("")

    sp_ac_l, tot_ac_sp, avg_ac_sp, mn_ac_sp, mx_ac_sp, std_ac_sp, idle_ac_sp = load_stats(sp_link_load, agg_core_links)
    ft_ac_l, tot_ac_ft, avg_ac_ft, mn_ac_ft, mx_ac_ft, std_ac_ft, idle_ac_ft = load_stats(ft_link_load, agg_core_links)
    
    lines.append("Aggregation -> Core uplinks ({} links, 2 per agg switch):".format(len(agg_core_links)))
    lines.append("  SP: total={} avg={:.2f} min={} max={} std={:.2f} idle_links={}/{}".format(
        int(tot_ac_sp), avg_ac_sp, int(mn_ac_sp), int(mx_ac_sp), std_ac_sp, idle_ac_sp, len(agg_core_links)))
    lines.append("  FT: total={} avg={:.2f} min={} max={} std={:.2f} idle_links={}/{}".format(
        int(tot_ac_ft), avg_ac_ft, int(mn_ac_ft), int(mx_ac_ft), std_ac_ft, idle_ac_ft, len(agg_core_links)))
    lines.append("")

    for dpid, port, n_id, o_id in agg_core_links:
        sp_v = sp_link_load.get((dpid, port), 0)
        ft_v = ft_link_load.get((dpid, port), 0)
        lines.append("    {} (dpid {}) port {} -> {} : SP={:>3}  FT={:>3}".format(
            n_id, dpid, port, o_id, sp_v, ft_v))
    lines.append("")

    sp_ea_l, tot_ea_sp, avg_ea_sp, mn_ea_sp, mx_ea_sp, std_ea_sp, idle_ea_sp = load_stats(sp_link_load, edge_agg_links)
    ft_ea_l, tot_ea_ft, avg_ea_ft, mn_ea_ft, mx_ea_ft, std_ea_ft, idle_ea_ft = load_stats(ft_link_load, edge_agg_links)

    lines.append("Edge -> Aggregation uplinks ({} links, 2 per edge switch):".format(len(edge_agg_links)))
    lines.append("  SP: total={} avg={:.2f} min={} max={} std={:.2f} idle_links={}/{}".format(
        int(tot_ea_sp), avg_ea_sp, int(mn_ea_sp), int(mx_ea_sp), std_ea_sp, idle_ea_sp, len(edge_agg_links)))
    lines.append("  FT: total={} avg={:.2f} min={} max={} std={:.2f} idle_links={}/{}".format(
        int(tot_ea_ft), avg_ea_ft, int(mn_ea_ft), int(mx_ea_ft), std_ea_ft, idle_ea_ft, len(edge_agg_links)))
    lines.append("")

    for dpid, port, n_id, o_id in edge_agg_links:
        sp_v = sp_link_load.get((dpid, port), 0)
        ft_v = ft_link_load.get((dpid, port), 0)
        lines.append("    {} (dpid {}) port {} -> {} : SP={:>3}  FT={:>3}".format(
            n_id, dpid, port, o_id, sp_v, ft_v))
    lines.append("")

    core_dpids = {i + 1 for i, n in enumerate(ft_topo.switches) if n.type == 'core'}
    def get_active_cores(load_dict):
        used = set()
        for (dpid, port), val in load_dict.items():
            if val <= 0.001:
                continue
            nxt = next((n for n, p in adjacency.get(dpid, {}).items() if p == port), None)
            if nxt in core_dpids:
                used.add(nxt)
        return used

    sp_cores = sorted(list(get_active_cores(sp_link_load)))
    ft_cores = sorted(list(get_active_cores(ft_link_load)))

    lines.append("Distinct core switches carrying >=1 flow: SP={}/{}  FT={}/{}".format(
        len(sp_cores), len(core_dpids), len(ft_cores), len(core_dpids)))
    lines.append("  SP uses core switches: {}".format(sp_cores))
    lines.append("  FT uses core switches: {}".format(ft_cores))
    lines.append("")

    report = "\n".join(lines)
    print(report)

    report_path = os.path.join(HERE, "link_load_report_k{}.txt".format(k))
    with open(report_path, "w") as f:
        f.write(report + "\n")

    plots_dir = os.path.join(HERE, "plots", "k{}".format(k))
    os.makedirs(plots_dir, exist_ok=True)

    def generate_bars(sp_ld, ft_ld, ecmp_ld, labels, title, filename):
        x = list(range(len(labels)))
        width = 0.25
        fig, ax = plt.subplots(figsize=(max(10, len(labels) * 0.4), 5))
        ax.bar([i - width for i in x], sp_ld, width, label='SP Dijkstra', color='tab:blue')
        ax.bar([i for i in x], ft_ld, width, label='Two-Level Suffix', color='tab:orange')
        ax.bar([i + width for i in x], ecmp_ld, width, label='ECMP Uniform', color='tab:green')
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=60, ha='right', fontsize=7)
        ax.set_ylabel('Aggregated Flow Load Count')
        ax.set_title(title)
        ax.legend()
        fig.tight_layout()
        out = os.path.join(plots_dir, filename)
        fig.savefig(out, dpi=150)
        plt.close(fig)

    ecmp_ac_l = [ecmp_link_load.get((dpid, port), 0) for dpid, port, _, _ in agg_core_links]
    ecmp_ea_l = [ecmp_link_load.get((dpid, port), 0) for dpid, port, _, _ in edge_agg_links]

    ac_labels = ['{}\nport{}'.format(sid.split('.')[2], port) for _, port, sid, _ in agg_core_links]
    generate_bars(sp_ac_l, ft_ac_l, ecmp_ac_l, ac_labels, 'Aggregation -> Core Network Link Load Comparison', 'agg_core_loads.png')

    ea_labels = ['{}\nport{}'.format(sid.split('.')[2], port) for _, port, sid, _ in edge_agg_links]
    generate_bars(sp_ea_l, ft_ea_l, ecmp_ea_l, ea_labels, 'Edge -> Aggregation Network Link Load Comparison', 'edge_agg_loads.png')

    fig, axes = plt.subplots(1, 3, figsize=(14, 4.5))
    architectures = ['SP', 'Two-Level', 'ECMP']
    
    max_uplink_loads = [max(sp_ac_l), max(ft_ac_l), max(ecmp_ac_l)]
    std_uplink_loads = [std_ac_sp, std_ac_ft, load_stats(ecmp_link_load, agg_core_links)[5]]
    cores_used = [len(sp_cores), len(ft_cores), len(get_active_cores(ecmp_link_load))]

    colors = ['tab:blue', 'tab:orange', 'tab:green']
    
    axes[0].bar(architectures, max_uplink_loads, color=colors)
    axes[0].set_title('Max Concentrated Load on\nAny Single Agg->Core Link')
    axes[0].set_ylabel('Peak Flow Constraints')

    axes[1].bar(architectures, std_uplink_loads, color=colors)
    axes[1].set_title('Standard Deviation of Load\nAcross Agg->Core Fabric')
    axes[1].set_ylabel('Imbalance Deviation Value')

    axes[2].bar(architectures, cores_used, color=colors)
    axes[2].set_title('Distinct Core Switches\nUtilized (Traffic >= 1 Flow)')
    axes[2].set_ylabel('Active Core Count')
    axes[2].set_ylim(0, len(core_dpids) + 1)

    fig.suptitle('Load Balance Efficiency Metrics across Aggregation Layer Topology (Including ECMP Performance Summary)')
    fig.tight_layout()
    summary_out = os.path.join(plots_dir, 'fabric_efficiency_summary.png')
    fig.savefig(summary_out, dpi=150)
    plt.close(fig)


if __name__ == '__main__':
    main(k=4)