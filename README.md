# A Paxos-Based Strongly Consistent Live Document Editor

In this project, we implement a strongly consistent live document editor with the
Paxos Consensus Protocol using coroutines introduced in C++20. The editor supports strongly consistent read/writes by each participating process, eliminating conflicts due to concurrent editing as in existing live document editors (e.g., Google docs, Live Share). The distributed feature keeps the service light-weight and easily manageable. We will test and report the correctness and latency of the editor as well as the Paxos implementation. If time allows, we will compare the performance with open-source c++ coroutine based Raft implementations, and implement additional optimizations such as log truncation and leader-based Paxos. A stretch goal is to ship it as a VS Code extension with a similar interface as the Live Share Extension.

Idea: localized ctrl+z, as compared to Live Share

## Build and Run

From repo root:

```bash
cmake -S . -B build
cmake --build build -j4
```

Run tests:

```bash
ctest --test-dir build --output-on-failure
```

Show node executable usage:

```bash
./build/paxos_node --help
```

Run one Paxos node per process (3 terminals):

Terminal 1:
```bash
./build/paxos_node --id 1 --nodes 1:15001,2:15002,3:15003
```

Terminal 2:
```bash
./build/paxos_node --id 2 --nodes 1:15001,2:15002,3:15003
```

Terminal 3:
```bash
./build/paxos_node --id 3 --nodes 1:15001,2:15002,3:15003
```

## Latency Evaluation (localhost)

Automated evaluation script:

```bash
python3 tools/eval_latency.py --binary ./build/paxos_node --commands 100 --warmup 10 --base-port 18000 --out-dir eval
```

Outputs:

- `eval/latency_samples.csv`: per-command latency samples
- `eval/latency_summary.txt`: p50/p95/p99 and throughput summary
- `eval/latency_figure.svg`: visualization figure

Detailed methodology and experiment variants:

- `docs/latency_evaluation.md`
