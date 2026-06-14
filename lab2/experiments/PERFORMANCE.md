<!-- @format -->

# Performance comparison: shortest-path vs. two-level routing

This note documents the performance-comparison experiments for the k=4
fat-tree, the code that produces them, and an explanation of the results
grounded in `sp_routing.py` and `ft_routing.py`.

Two complementary experiments are provided:

1. **`simulate_routing.py`** - a pure-Python simulation that runs the
   _actual_ routing-decision code of both controllers (Dijkstra in
   `SPRouter._shortest_path`, and the structural flow tables installed by
   `FTRouter._install_structural_flows`/`_install_suffix_flows`) against a
   realistic, Mininet-derived port/adjacency map, and computes per-link
   load for all 240 ordered host-pair flows. This runs anywhere (no
   Mininet/OVS/root needed) and is the basis for the analysis below.

2. **`perf_compare.py` / `plot_results.py`** - a live Mininet experiment
   (ping + iperf) that runs the real network with each controller in turn.
   This requires root and Open vSwitch (the lab VM) and is described in
   [Live experiment](#live-experiment) below.

## How to run

```sh
# 1. Pure-Python simulation (works anywhere, e.g. in this sandbox)
.venv/bin/python3 experiments/simulate_routing.py
# -> experiments/link_load_report_k4.txt
# -> experiments/plots/k4/*.png

# 2. Live Mininet experiment (lab VM only, needs root + OVS)
sudo .venv/bin/python3 experiments/perf_compare.py
.venv/bin/python3 experiments/plot_results.py
# -> experiments/results_{sp,ft}.json
# -> experiments/plots/live/*.png
```

## Simulation results (k=4, 240 ordered host-pair flows)

Full numbers: [`link_load_report_k4.txt`](link_load_report_k4.txt).
Plots: [`plots/k4/`](plots/k4/).

### 1. Both schemes are valid shortest-path routing

For every one of the 240 ordered host pairs, the SP path length and the FT
path length are **identical** (min/avg/max = 0/3.47/4 switch-to-switch hops
for both). This is expected and is a basic correctness check: a fat-tree's
two-level routing tables (Section 3.5 of the fat-tree paper) only choose
_among_ the multiple equal-cost shortest paths between a pod and the core -
they never produce a longer path than plain shortest-path routing. The two
schemes differ only in **which** of the several equal-cost paths is chosen
for a given flow, not in path length.

### 2. SP routing concentrates traffic on a single core switch

`SPRouter._shortest_path` is a textbook Dijkstra over `self.adjacency`. All
links have weight 1, so for any (src-edge, dst-edge) pair there are several
equal-cost shortest paths (one per intermediate agg/core switch). The
tie-break is `min(unvisited, key=lambda n: dist[n])`, which - because dpids
are small integers stored in a Python `set` - deterministically prefers the
**lowest-numbered** switch among equally-close candidates.

The effect on the agg→core layer (16 directed links, 2 per aggregation
switch):

|     | total flows | avg/link | max/link | std dev | idle links |
| --- | ----------- | -------- | -------- | ------- | ---------- |
| SP  | 192         | 12.0     | **48**   | 20.78   | 12/16      |
| FT  | 192         | 12.0     | **24**   | 12.00   | 8/16       |

and on the edge→aggregation layer (16 directed links, 2 per edge switch):

|     | total flows | avg/link | max/link | std dev  | idle links |
| --- | ----------- | -------- | -------- | -------- | ---------- |
| SP  | 224         | 14.0     | **28**   | 14.00    | 8/16       |
| FT  | 224         | 14.0     | **14**   | **0.00** | 0/16       |

Under SP, **every** edge switch sends 100% of its inter-edge traffic out of
port 1 (towards the lower-dpid aggregation switch in its pod); port 2 is
completely idle. That aggregation switch in turn sends 100% of its
core-bound traffic out of its port 1, towards core switch dpid 1. As a
result **all** inter-pod traffic in the whole network funnels through a
single core switch (dpid 1) - the other 3 of the 4 core switches (and 12 of
16 agg→core links) carry zero traffic at all (see
[`plots/k4/agg_core_link_load.png`](plots/k4/agg_core_link_load.png) and
[`plots/k4/edge_agg_link_load.png`](plots/k4/edge_agg_link_load.png)).

### 3. Two-level routing spreads traffic across multiple paths

`FTRouter._install_structural_flows`/`_install_suffix_flows` implement the
fat-tree paper's suffix tables: at an edge switch, the outgoing port towards
the aggregation layer is chosen as a function of the _destination host id_
(the last IP octet, `h`), round-robin over the `k/2` aggregation neighbours
(`up_neighbors[(h-2) % len(up_neighbors)]`); at an aggregation switch, the
port towards the core is chosen the same way as a function of `h`.

For k=4 there are exactly two possible host ids per edge switch (`.2` and
`.3`), and exactly `k/2 = 2` up-links per layer, so this round-robin is a
clean bijection: traffic to `*.*.*.2` destinations always leaves an edge
switch via port 1 and traffic to `*.*.*.3` destinations always leaves via
port 2.

This has two measurable effects:

- **Edge→aggregation load becomes perfectly even**: both up-links of every
  edge switch carry exactly 14 of the 28 flows that leave that switch
  (std dev = 0.00, vs. 14.00 for SP, where one link carries all 28 and the
  other carries 0).
- **Core-switch diversity doubles**: FT uses 2 of the 4 core switches
  (dpids 1 and 4) instead of SP's 1 of 4, and the busiest agg→core link
  carries 24 flows instead of 48 - exactly half.

(FT does not use _all four_ core switches for k=4. This is a structural
consequence of the suffix table being keyed on the destination host id:
because the edge layer already routes `.2`-destined and `.3`-destined
traffic onto disjoint aggregation switches, each aggregation switch only
ever sees _one_ of the two suffix values, so its own suffix table - which
re-keys on the _same_ value - only ever exercises one of its two core ports.
With `k/2 = 2` suffix classes and `k/2 = 2` up-links per layer this
composition of two bijections is itself a bijection, covering only `k/2`
of the `(k/2)^2` core switches. The mechanism still does what the paper asks

- consistent per-destination paths with port-level spreading at every switch
- and it already cuts the maximum link load and increases core-switch
  fan-out by 2x relative to SP's single-path concentration.)

### Summary

| metric (k=4, 240 flows)       | SP routing | Two-level routing |
| ----------------------------- | ---------- | ----------------- |
| Distinct core switches used   | 1 / 4      | 2 / 4             |
| Max load on any agg→core link | 48         | 24                |
| Max load on any edge→agg link | 28         | 14                |
| Std dev of edge→agg load      | 14.0       | 0.0               |

Two-level routing achieves the same path _lengths_ as shortest-path routing
while roughly **halving** the worst-case link load and **doubling**
core-switch utilisation, by deterministically spreading flows across the
multiple equal-cost paths a fat-tree provides - exactly the property
Al-Fares et al. motivate in Section 3.5 of the fat-tree paper.

## Live experiment

`perf_compare.py` automates the same comparison on real Mininet/OVS:

1. Starts `ryu-manager --observe-links <controller>.py`.
2. Builds the `FattreeNet` topology and starts Mininet.
3. `net.pingAll()` - sanity check that every host can reach every other host
   under both controllers (exercises ARP handling, topology/port discovery,
   and reactive/structural flow installation).
4. Single-flow `iperf` for three pairs with 0, 2 and 4 inter-switch hops
   (`h1<->h2`, `h1<->h3`, `h1<->h5`). Both controllers route these flows
   along equal-length paths with no contention, so throughput should be
   similar between SP and FT for each pair (each is limited by the
   per-link `15 Mbit/s` `TCLink`).
5. A **concurrent** two-flow test: `h1` (`10.0.0.2`) simultaneously sends to
   `h5` (`10.1.0.2`, suffix `.2`) and `h10` (`10.2.0.3`, suffix `.3`).
   Based on the simulation above:
   - Under **SP**, both flows leave `h1`'s edge switch via the same port and
     traverse the same agg→core link (SP ignores the destination suffix), so
     they contend for one `15 Mbit/s` link - aggregate throughput is
     expected to be roughly `15 Mbit/s`.
   - Under **two-level routing**, the `.2`-destined and `.3`-destined flows
     are routed onto two disjoint edge→agg→core links, so each flow gets
     close to the full `15 Mbit/s` - aggregate throughput is expected to be
     roughly **2x** that of SP (~`30 Mbit/s`).

Run `plot_results.py` afterwards to generate
`plots/live/single_flow_throughput.png` and
`plots/live/concurrent_flow_throughput.png` from `results_sp.json` /
`results_ft.json`.

This sandbox has no root access / Open vSwitch / `mn`, so `perf_compare.py`
could not be executed here; `simulate_routing.py` exercises the same
controller code paths and topology and is the evidence for the analysis
above. `perf_compare.py` is ready to run on the lab VM as described.
