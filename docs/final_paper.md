# Linearizing Paxos: Coroutine-Driven Consensus and Replication

## Abstract

This project implements a coroutine-driven Multi-Paxos replicated state machine in C++20 and evaluates it on a deterministic network object system (NOS) simulator with failure injection. The main hypothesis is that modern coroutine control flow can significantly improve implementation clarity without sacrificing protocol correctness. We build three persistent node coroutines (message handling, election, heartbeat), a deterministic event runtime, and a message-based NOS abstraction with crash/restore and partition controls. We then validate the system through deterministic integration tests that cover normal replication, leader failover, minority partition behavior, and node recovery catch-up. Results show that the coroutine structure yields a linearized protocol implementation with explicit state transitions and clean failure orchestration, while preserving key Multi-Paxos safety and liveness behavior in the tested scenarios.

## 1. Introduction

Consensus protocols are foundational to fault-tolerant distributed systems, yet implementations are often difficult to read because they interleave message callbacks, timers, and mutable state. Paxos is especially notorious: many developers understand the proof sketches but struggle with production-quality control flow.

This project explores whether C++20 coroutines can make Paxos implementation more direct by preserving sequential control structure while remaining event-driven and non-blocking. We focus on:

1. A coroutine-native implementation of Multi-Paxos.
2. A deterministic, testable NOS layer for protocol experimentation.
3. Failure injection to exercise safety and liveness edge cases.

## 2. System Goals and Scope

### 2.1 Goals

- Implement a readable Multi-Paxos replicated log in C++20.
- Avoid callback-heavy code paths by using coroutine loops.
- Provide deterministic testing with crash and partition injection.
- Validate expected consensus behavior under adversity.

### 2.2 Non-goals

- WAN performance optimization.
- Persistent storage and disk recovery.
- Dynamic membership and production hardening.

## 3. Architecture

The implementation is organized into three layers.

### 3.1 Runtime Layer

A deterministic single-threaded event runtime drives coroutine execution. It supports ready queues, timer queues, and bounded step execution. This allows tests to progress protocol time deterministically without wall-clock dependence.

Core abstractions:

- `Runtime`: schedules coroutine handles and timed wakeups.
- `DetachedTask`: fire-and-forget coroutine wrapper.
- `SleepFor`: awaitable timer primitive.

### 3.2 NOS Layer

The NOS simulator models message transport and fault boundaries.

- `Mailbox<T>`: coroutine-friendly receive queue.
- `SimulatedNetwork<PaxosMessage>`: endpoint registry and message routing.
- Failure injection controls:
  - `crash(node_id)` / `restore(node_id)`
  - `partition(a,b)` / `heal(a,b)` / `heal_all()`

This layer intentionally keeps semantics deterministic and in-memory to maximize debuggability and test reproducibility.

### 3.3 Consensus Layer

Each replica runs Multi-Paxos roles (proposer/acceptor/learner) inside a single `PaxosNode` state machine.

Main persistent coroutines per node:

- `message_loop()`: awaits and dispatches protocol messages.
- `election_loop()`: triggers prepare phase on timeout.
- `heartbeat_loop()`: leader keepalive and commit advertisement.

Protocol messages include `Prepare`, `Promise`, `AcceptRequest`, `Accepted`, `Commit`, `Heartbeat`, and synchronization messages (`SyncRequest`, `SyncData`) for follower catch-up.

## 4. Protocol Design

### 4.1 Leader Election

Nodes track `promised_ballot` and initiate election with monotonic ballot numbers (`round * 100 + node_id`). A candidate gathers quorum `Promise` responses, then becomes leader for that ballot.

### 4.2 Value Recovery and Re-proposal

When becoming leader, a node merges accepted values from quorum promises and re-proposes highest-ballot values for each slot before serving new client commands. This preserves Paxos safety while enabling log continuation.

### 4.3 Replication and Commit

For each slot, the leader issues `AcceptRequest`. Once quorum `Accepted` responses arrive, it broadcasts `Commit`. Replicas apply committed commands in slot order, maintaining a linearizable applied prefix.

### 4.4 Catch-up Path

Heartbeats carry committed index metadata. A lagging follower sends `SyncRequest` and the leader responds with `SyncData` entries, allowing crashed/restored nodes to rejoin without full-state transfer.

## 5. Failure Injection Methodology

We evaluate the implementation through deterministic integration tests over a 3-node cluster.

### 5.1 Test Cases

1. Basic replication: elect leader, submit three commands, verify all replicas share identical committed prefix.
2. Leader failover: crash current leader, elect new leader, ensure continued commit progress.
3. Partition behavior: isolate one node in minority, attempt write to minority leader candidate, verify no commit; majority continues and converges after heal.
4. Crash/restore catch-up: crash follower, commit commands on quorum, restore follower, verify synchronization and ordered apply.

### 5.2 Correctness Criteria

- Safety: no divergent committed prefix among non-faulty replicas.
- Liveness (under partial synchrony assumptions represented in simulation): majority-connected nodes continue to commit after leader failure or partition of minority.

## 6. Results

All implemented tests pass consistently under deterministic execution.

Observed outcomes:

- Leader election stabilizes and writes commit under healthy network.
- Minority partitions cannot commit independent writes (quorum protection).
- Majority partitions retain progress and later reconcile with healed nodes.
- Restored replicas catch up using sync messages and preserve command order.

Although this environment does not model realistic asynchronous delays or disk persistence, it provides high confidence in control-flow correctness and key Paxos invariants in the tested fault model.

## 7. Discussion

### 7.1 Coroutine benefits

Compared to callback-style event handlers, coroutine loops improved readability in three ways:

- Temporal logic is explicit (`co_await` for receive and timers).
- Control paths are linear and easier to audit.
- Tests can drive the runtime deterministically without thread races.

### 7.2 Trade-offs

- Current simulator bypasses realistic transport timing and packet loss distributions.
- Lack of persistence means crash semantics are process-level, not storage-level.
- Static membership limits applicability to reconfiguration-heavy systems.

## 8. Threats to Validity

- Deterministic runtime may hide timing-dependent bugs that occur under high jitter.
- 3-node tests validate core behavior but do not stress scalability.
- Absence of disk faults and torn writes leaves durability untested.

## 9. Future Work

1. Dynamic membership with joint consensus.
2. WAL persistence + replay-based recovery.
3. Snapshot and log compaction with install-snapshot RPC.
4. Richer NOS delay/drop models for throughput-latency studies.
5. Property-based or model-checking integration for stronger invariant coverage.

## 10. Conclusion

The project demonstrates that coroutine-driven C++20 can linearize Multi-Paxos implementation structure while preserving the core behavior expected from consensus replication under crash and partition faults. The resulting codebase is concise, deterministic, and directly testable, making it suitable as both a final project artifact and a foundation for advanced extensions such as reconfiguration and snapshotting.

## Reproducibility

Build and test:

```bash
cmake -S . -B build
cmake --build build -j4
ctest --test-dir build --output-on-failure
```
