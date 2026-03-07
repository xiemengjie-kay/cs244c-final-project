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

Run Paxos with 3 processes (open 3 terminals, one command each):

Terminal 1:
```bash
./build/paxos_demo --id 1 --ports 1:15001,2:15002,3:15003
```

Terminal 2:
```bash
./build/paxos_demo --id 2 --ports 1:15001,2:15002,3:15003
```

Terminal 3:
```bash
./build/paxos_demo --id 3 --ports 1:15001,2:15002,3:15003
```
