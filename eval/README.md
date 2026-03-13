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
- Output directory: pass `--out-dir`; each script should write into its own subfolder
- Typical artifact `*_samples.csv`: raw samples
- Typical artifact `*_summary.txt`: computed metrics
- Typical artifact `*_figure.svg`: visualization
- Timeout handling: keep timeout points in CSV
- Timeout handling: exclude timeout points from latency percentile math
- Timeout handling: report timeout/failure rate explicitly

## Script Index

### `eval_accept_to_commit.py`

Purpose:

- Evaluate exact leader-side Paxos latency from `accept_send -> commit_send`
- Uses node trace lines emitted by `--eval-trace`

Main options:

- `--mode both|run|plot`
- `--message-lengths 16,64,256,...`
- `--cluster-size N`
- `--submit-mode round_robin|leader_only`

Examples:

```bash
# Run experiments + summary + figure
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

```bash
# Recreate summary/figure from an existing CSV only
python3 eval/eval_accept_to_commit.py \
  --mode plot \
  --input-csv eval/eval_accept_to_commit/accept_to_commit_samples.csv \
  --out-dir eval/eval_accept_to_commit
```

## Adding New Eval Scripts

When adding a new script under `eval/`, append a new section in `Script Index` with:

- one-line purpose
- important flags
- one `run` example
- one `plot`/post-process example (if supported)
