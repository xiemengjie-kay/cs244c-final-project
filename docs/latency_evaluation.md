# Latency Evaluation Plan (Same-Machine Paxos)

This evaluation targets your current TCP-based multi-process Paxos implementation (`paxos_node`).

## Why this is better than only node-to-node latency

Measuring only pairwise node RTT misses consensus behavior. For Paxos, you should measure:

1. Leader commit latency: `client submit -> leader commit`.
2. Cluster commit latency: `client submit -> all replicas committed`.
3. Follower replication lag: `leader commit -> follower commit`.
4. Throughput under load: committed operations per second.
5. Initial leader election time.

Together, these capture transport + protocol + scheduling effects.

## Automated harness

Use `tools/eval_latency.py`.

What it does:

- Launches 3 `paxos_node` processes on localhost TCP ports.
- Detects current leader via `/status`.
- Sends warmup commands.
- Sends `N` measured commands to the leader.
- Parses per-node commit lines from stdout.
- Computes latency distributions and throughput.
- Generates:
  - `eval/latency_samples.csv`
  - `eval/latency_summary.txt`
  - `eval/latency_figure.svg`

## Commands

```bash
cmake -S . -B build
cmake --build build -j4
python3 tools/eval_latency.py --binary ./build/paxos_node --commands 100 --warmup 10 --base-port 18000 --out-dir eval
```

## Figure produced

`eval/latency_figure.svg` has:

- Left panel: CDF of leader commit latency vs cluster commit latency.
- Right panel: p50/p95 bars for leader latency, cluster latency, and follower lag.
- Footer: throughput and initial leader election time.

## Suggested additional experiments

Run multiple workloads and compare figures:

```bash
python3 tools/eval_latency.py --binary ./build/paxos_node --commands 50  --base-port 18100 --out-dir eval_50
python3 tools/eval_latency.py --binary ./build/paxos_node --commands 200 --base-port 18200 --out-dir eval_200
python3 tools/eval_latency.py --binary ./build/paxos_node --commands 500 --base-port 18300 --out-dir eval_500
```

Also evaluate fault behavior:

- Crash leader mid-run (`/crash`) and measure time-to-new-leader + post-failover commit latency.
- Crash/restore follower and measure catch-up lag from commit logs.
