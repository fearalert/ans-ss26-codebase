#!/usr/bin/env python3

"""
Live Mininet performance-comparison experiment: SP routing vs. two-level
(fat-tree) routing.

This needs a real Mininet/Open vSwitch environment with root privileges
(the lab VM) -- it CANNOT run in a sandbox without OVS/root. For a
sandbox-friendly analysis of the same question, see
experiments/simulate_routing.py and experiments/PERFORMANCE.md, which
motivate the host pairs chosen below.

For each controller (sp_routing.py, ft_routing.py) this script:
  1. Starts `ryu-manager --observe-links <controller>.py` as a subprocess.
  2. Builds the k=4 fat-tree Mininet network (fat-tree.FattreeNet).
  3. Runs net.pingAll() to check all-pairs reachability (also exercises
     ARP handling and reactive/structural flow installation).
  4. Runs single-flow iperf (TCP) for three representative host pairs:
       - intra-edge   (h1 <-> h2,  0 inter-switch hops)
       - intra-pod    (h1 <-> h3,  2 inter-switch hops)
       - inter-pod    (h1 <-> h5,  4 inter-switch hops)
  5. Runs a concurrent two-flow iperf test from h1 to two inter-pod hosts
     whose IPs end in .2 and .3 respectively (h5 = 10.1.0.2 and
     h10 = 10.2.0.3). According to experiments/simulate_routing.py, SP
     routing sends both flows over the *same* edge->agg->core link
     (it always picks port 1 / the first equal-cost neighbour), while
     two-level routing spreads them over two disjoint links (suffix
     ".2" vs ".3"). The aggregate throughput of these two concurrent
     flows is therefore expected to be roughly 2x higher under two-level
     routing.
  6. Tears the network and controller down.

Results are written to results_<controller>.json; plot_results.py turns
both result files into comparison plots.

Usage (on the lab VM, from the code/ directory):
    sudo .venv/bin/python3 experiments/perf_compare.py
    sudo .venv/bin/python3 experiments/perf_compare.py --controller sp --seconds 10
    .venv/bin/python3 experiments/plot_results.py
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_DIR = os.path.dirname(HERE)
sys.path.insert(0, CODE_DIR)

# mininet/cli.py does `from select import poll, POLLIN` at module level.
# `select.poll` is Linux-only; some systems (macOS, certain Python builds)
# don't provide it. Since perf_compare.py never invokes the Mininet CLI,
# a no-op stub is enough to let the import succeed.
import select as _sel
if not hasattr(_sel, 'poll'):
    class _PollStub:
        def __init__(self): pass
        def register(self, fd, events=0): pass
        def unregister(self, fd): pass
        def poll(self, timeout=None): return []
    _sel.poll = _PollStub
    if not hasattr(_sel, 'POLLIN'):
        _sel.POLLIN = 1
del _sel

from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.link import TCLink
from mininet.log import setLogLevel, info
import mininet.clean

import topo


CONTROLLERS = {'sp': 'sp_routing.py', 'ft': 'ft_routing.py'}

# (src, dst, label) - see module docstring for the rationale behind h1/h5/h10
SINGLE_FLOW_PAIRS = [
    ('h1', 'h2', 'intra-edge'),   # 10.0.0.2 <-> 10.0.0.3
    ('h1', 'h3', 'intra-pod'),    # 10.0.0.2 <-> 10.0.1.2
    ('h1', 'h5', 'inter-pod'),    # 10.0.0.2 <-> 10.1.0.2
]

CONCURRENT_PAIRS = [
    ('h1', 'h5'),   # 10.0.0.2 -> 10.1.0.2  (dst suffix .2)
    ('h1', 'h10'),  # 10.0.0.2 -> 10.2.0.3  (dst suffix .3)
]


def load_fattree_net_module():
    spec = importlib.util.spec_from_file_location(
        "fattree_net_mod", os.path.join(CODE_DIR, "fat-tree.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_bw(bw_str):
    """Parse an iperf result string such as '10.5 Mbits/sec' into Mbit/s."""
    m = re.match(r'([\d.]+)\s+(\w)bits/sec', bw_str.strip())
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2)
    if unit == 'G':
        val *= 1000
    elif unit == 'K':
        val /= 1000
    return val


def run_experiment(controller_key, seconds=10):
    controller_file = CONTROLLERS[controller_key]

    log_path = os.path.join(HERE, 'ryu_{}.log'.format(controller_key))
    log_f = open(log_path, 'w')
    ryu_bin_venv = os.path.join(CODE_DIR, '.venv', 'bin', 'ryu-manager')
    ryu_bin = ryu_bin_venv if os.path.exists(ryu_bin_venv) else 'ryu-manager'
    ryu = subprocess.Popen(
        [ryu_bin, '--observe-links', controller_file],
        cwd=CODE_DIR, stdout=log_f, stderr=subprocess.STDOUT)

    # Give ryu-manager time to come up and bind to the OpenFlow port.
    time.sleep(3)

    fattree_mod = load_fattree_net_module()
    ft_topo = topo.Fattree(4)
    net_topo = fattree_mod.FattreeNet(ft_topo)
    net = Mininet(topo=net_topo, controller=None, autoSetMacs=True, link=TCLink)
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)

    results = {'controller': controller_key}
    try:
        net.start()
        # Let switches connect to the controller and topology discovery /
        # structural-flow installation (ft_routing) settle.
        time.sleep(8)

        info('*** Connectivity check (pingAll)\n')
        loss = net.pingAll()
        results['ping_loss_pct'] = loss

        info('*** Single-flow iperf tests\n')
        results['single_flow'] = {}
        for src, dst, label in SINGLE_FLOW_PAIRS:
            h_src, h_dst = net.get(src), net.get(dst)
            bw = net.iperf((h_src, h_dst), seconds=seconds)
            results['single_flow'][label] = {
                'src': src, 'dst': dst,
                'src_ip': h_src.IP(), 'dst_ip': h_dst.IP(),
                'server_mbps': parse_bw(bw[0]),
                'client_mbps': parse_bw(bw[1]),
            }

        info('*** Concurrent two-flow iperf test\n')
        servers = []
        for idx, (_s, d) in enumerate(CONCURRENT_PAIRS):
            h_dst = net.get(d)
            port = 5101 + idx
            h_dst.cmd('iperf -p {} -s -t {} &'.format(port, seconds + 5))
            servers.append((h_dst, port))
        time.sleep(1)

        procs = []
        for idx, (s, _d) in enumerate(CONCURRENT_PAIRS):
            h_src = net.get(s)
            h_dst, port = servers[idx]
            p = h_src.popen(
                'iperf -c {} -p {} -t {}'.format(h_dst.IP(), port, seconds),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            procs.append(p)

        concurrent_mbps = []
        for p in procs:
            out, _ = p.communicate()
            text = out.decode() if isinstance(out, bytes) else out
            bws = re.findall(r'([\d.]+\s+\w bits/sec)', text)
            concurrent_mbps.append(parse_bw(bws[-1]) if bws else None)

        for h_dst, _port in servers:
            h_dst.cmd('kill %iperf')

        results['concurrent_flow'] = {
            'pairs': ['{}->{}'.format(s, d) for s, d in CONCURRENT_PAIRS],
            'per_flow_mbps': concurrent_mbps,
            'aggregate_mbps': sum(b for b in concurrent_mbps if b is not None),
        }

    finally:
        net.stop()
        ryu.terminate()
        try:
            ryu.wait(timeout=10)
        except subprocess.TimeoutExpired:
            ryu.kill()
        log_f.close()
        mininet.clean.cleanup()

    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=10,
                         help='iperf duration per flow (default: 10s)')
    parser.add_argument('--controller', choices=['sp', 'ft', 'both'], default='both',
                         help='which controller(s) to test (default: both)')
    args = parser.parse_args()

    setLogLevel('info')

    controllers = ['sp', 'ft'] if args.controller == 'both' else [args.controller]
    for c in controllers:
        print("=== Running experiment for controller: {} ===".format(c))
        results = run_experiment(c, seconds=args.seconds)
        out_path = os.path.join(HERE, 'results_{}.json'.format(c))
        with open(out_path, 'w') as f:
            json.dump(results, f, indent=2)
        print("Wrote", out_path)
        print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()