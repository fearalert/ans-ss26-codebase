#!/usr/bin/env python3

"""
Plot the results produced by experiments/perf_compare.py
(results_sp.json / results_ft.json) on the lab VM.

Run with:
    .venv/bin/python3 experiments/plot_results.py
"""

import json
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))


def load(controller):
    path = os.path.join(HERE, 'results_{}.json'.format(controller))
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def main():
    sp = load('sp')
    ft = load('ft')

    if sp is None or ft is None:
        print("results_sp.json / results_ft.json not found.")
        print("Run experiments/perf_compare.py on a Mininet-capable host first:")
        print("    sudo .venv/bin/python3 experiments/perf_compare.py")
        return

    plots_dir = os.path.join(HERE, 'plots', 'live')
    os.makedirs(plots_dir, exist_ok=True)

    print("Ping loss: SP={}%  FT={}%".format(sp['ping_loss_pct'], ft['ping_loss_pct']))

    # --- Single-flow throughput ---
    labels = list(sp['single_flow'].keys())
    sp_vals = [sp['single_flow'][l]['client_mbps'] for l in labels]
    ft_vals = [ft['single_flow'][l]['client_mbps'] for l in labels]

    x = range(len(labels))
    width = 0.35
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.bar([i - width / 2 for i in x], sp_vals, width, label='SP routing', color='tab:blue')
    ax.bar([i + width / 2 for i in x], ft_vals, width, label='Two-level routing', color='tab:orange')
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.set_ylabel('Throughput (Mbit/s)')
    ax.set_title('Single-flow iperf throughput')
    ax.legend()
    fig.tight_layout()
    out_path = os.path.join(plots_dir, 'single_flow_throughput.png')
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print("Wrote", out_path)

    # --- Concurrent-flow aggregate throughput ---
    sp_conc = sp['concurrent_flow']
    ft_conc = ft['concurrent_flow']

    fig, axes = plt.subplots(1, 2, figsize=(9, 5))

    pair_labels = sp_conc['pairs']
    x = range(len(pair_labels))
    axes[0].bar([i - width / 2 for i in x], sp_conc['per_flow_mbps'], width,
                 label='SP routing', color='tab:blue')
    axes[0].bar([i + width / 2 for i in x], ft_conc['per_flow_mbps'], width,
                 label='Two-level routing', color='tab:orange')
    axes[0].set_xticks(list(x))
    axes[0].set_xticklabels(pair_labels)
    axes[0].set_ylabel('Throughput (Mbit/s)')
    axes[0].set_title('Per-flow throughput\n(2 concurrent inter-pod flows)')
    axes[0].legend()

    axes[1].bar(['SP', 'Two-level'],
                 [sp_conc['aggregate_mbps'], ft_conc['aggregate_mbps']],
                 color=['tab:blue', 'tab:orange'])
    axes[1].set_ylabel('Aggregate throughput (Mbit/s)')
    axes[1].set_title('Aggregate throughput\n(2 concurrent inter-pod flows)')

    fig.tight_layout()
    out_path = os.path.join(plots_dir, 'concurrent_flow_throughput.png')
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print("Wrote", out_path)


if __name__ == '__main__':
    main()
