# Evaluation Scripts

This folder contains experiment scripts and generated evaluation artifacts.

## Build Prerequisite

Build `paxos_node` once before running eval scripts:

```bash
cmake -S . -B build
cmake --build build -j4
```

## Common Conventions

- Script location: `eval/*.py`
- Output directory: pass `--out-dir`; each script writes its own `*_samples.csv`, `*_summary.txt`, and `*_figure.svg`
- `--mode both`: run experiment + write summary + write figure
- `--mode run`: run experiment + write summary (no figure)
- `--mode plot`: read existing CSV + regenerate summary/figure
- Timeout handling: timeouts stay in CSV and are reported in summary/legend; latency percentiles use successful samples only

## Script Index

### `eval_accept_to_commit.py`

Purpose:

- Measure leader-side Paxos latency from `accept_send -> commit_send`

Main options:

- `--cluster-size N` or `N1,N2,...`
- `--message-lengths 16,64,256,...`
- `--submit-mode round_robin|leader_only`
- `--mode both|run|plot`

Example:

```bash
python3 eval/eval_accept_to_commit.py \
  --mode both \
  --binary ./build/paxos_node \
  --cluster-size 3 \
  --submit-mode round_robin \
  --message-lengths 16,64,256,1024 \
  --commands 100 \
  --base-port 18000 \
  --out-dir eval/eval_accept_to_commit
```

### `eval_submit_to_majority_follower_commit.py`

Purpose:

- Measure end-to-end latency from client submit to majority follower commit
- Latency endpoint is based on follower `/status` polling (`commit_index`)

Main options:

- `--cluster-size N` or `N1,N2,...`
- `--message-length BYTES` or `--message-lengths B1,B2,...`
- `--submit-mode round_robin|follower_round_robin|fixed_node|fixed_follower|fixed`
- `--fixed-submit-node ID` (used by fixed modes)
- `--status-poll-ms` (poll interval for follower commit status)
- `--mode both|run|plot`

Submit mode notes:

- `round_robin`: rotate submitter across all nodes (leader + followers)
- `follower_round_robin`: rotate submitter across followers only
- `fixed_node`: always submit through `--fixed-submit-node` (can be leader or follower)
- `fixed_follower`: always submit through `--fixed-submit-node`, but trial is marked failed if that node is leader
- `fixed`: backward-compatible alias for `fixed_follower`

Example:

```bash
python3 eval/eval_submit_to_majority_follower_commit.py \
  --mode both \
  --binary ./build/paxos_node \
  --cluster-size 3,5,11 \
  --message-length 256 \
  --submit-mode round_robin \
  --commands 100 \
  --base-port 18000 \
  --out-dir eval/eval_submit_to_majority_follower_commit
```

### `eval_cold_leader_election.py`

Purpose:

- Measure cold leader-election latency after fresh cluster launch
- Metric uses trace timestamps: `first leader_elected - first node_start`

Main options:

- `--cluster-size N` or `N1,N2,...`
- `--trials K` (trials per cluster size)
- `--timeout-ms T`
- `--scan-ms S` (stdout scan interval for trace events)
- `--mode both|run|plot`

Example:

```bash
python3 eval/eval_cold_leader_election.py \
  --mode both \
  --binary ./build/paxos_node \
  --cluster-size 3,5,11,23,55 \
  --trials 30 \
  --timeout-ms 10000 \
  --scan-ms 5 \
  --base-port 18000 \
  --out-dir eval/eval_cold_leader_election
```

### `eval_leader_crash_reelection.py`

Purpose:

- Measure re-election latency after crashing the current leader
- Metric uses trace timestamps: `first new leader_elected - node_crashed`
- Sweep knob: election timeout base (heartbeat stays fixed)

Main options:

- `--cluster-size N`
- `--heartbeat-ticks H` (fixed)
- `--election-timeout-base T1,T2,...`
- `--election-timeout-step S` (default `3`)
- `--trials K`
- `--mode both|run|plot`

Example:

```bash
python3 eval/eval_leader_crash_reelection.py \
  --mode both \
  --binary ./build/paxos_node \
  --cluster-size 5 \
  --heartbeat-ticks 2 \
  --election-timeout-base 8,14,20 \
  --election-timeout-step 3 \
  --trials 30 \
  --timeout-ms 12000 \
  --scan-ms 5 \
  --base-port 18000 \
  --out-dir eval/eval_leader_crash_reelection
```

## Plot-Only Examples

```bash
python3 eval/eval_submit_to_majority_follower_commit.py \
  --mode plot \
  --input-csv eval/eval_submit_to_majority_follower_commit/submit_to_majority_follower_commit_samples_256_3,5,11_round_robin.csv \
  --out-dir eval/eval_submit_to_majority_follower_commit
```

```bash
python3 eval/eval_cold_leader_election.py \
  --mode plot \
  --input-csv eval/eval_cold_leader_election/cold_leader_election_samples_3,5,11,23,55_trials30.csv \
  --out-dir eval/eval_cold_leader_election
```

```bash
python3 eval/eval_leader_crash_reelection.py \
  --mode plot \
  --input-csv eval/eval_leader_crash_reelection/leader_crash_reelection_samples_n5_hb2_eto8,14,20_trials30.csv \
  --out-dir eval/eval_leader_crash_reelection
```
