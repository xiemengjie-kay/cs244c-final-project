# Linearizing Paxos: Coroutine-Driven Consensus and Replication

A C++20 implementation of Multi-Paxos built around coroutine-based control flow and a deterministic Network Object System (NOS) simulator.

This project targets CS244C-style systems experimentation: readable consensus logic, explicit failure injection, and deterministic tests for safety/liveness properties.

## What is implemented

- C++20 coroutine runtime (`Runtime`, `DetachedTask`, `SleepFor`)
- NOS abstraction with mailbox endpoints and fault injection
  - node crash / restore
  - pairwise network partitions / healing
- Multi-Paxos replicated log
  - leader election via prepare/promise
  - phase-2 accept/accepted
  - commit propagation
  - leader heartbeat + follower catch-up (`SyncRequest` / `SyncData`)
- Deterministic end-to-end tests
  - basic replication
  - leader failover
  - partition behavior (minority write rejection)
  - crash/restore catch-up

## Repository layout

- `include/runtime.hpp`: deterministic event scheduler + coroutine primitives
- `include/nos.hpp`: mailbox and simulated network object system
- `include/paxos.hpp`: protocol messages, node API, and cluster API
- `src/paxos.cpp`: Multi-Paxos implementation
- `src/main.cpp`: runnable demo
- `tests/test_paxos.cpp`: deterministic integration tests
- `docs/final_paper.md`: final report

## Build and test

```bash
cmake -S . -B build
cmake --build build -j4
ctest --test-dir build --output-on-failure
```

Run demo:

```bash
./build/paxos_demo
```

## Isolation guarantee

All commands are executed inside the project workspace (`build/` artifacts only). No global packages were installed and no system files were modified.

## Design summary

### Coroutine linearization

Each node runs long-lived coroutines:

- `message_loop()`: awaits inbound protocol messages and dispatches handlers
- `election_loop()`: periodically checks timeout and starts phase-1
- `heartbeat_loop()`: leader heartbeat and commit index advertisement

This removes callback nesting and keeps protocol transitions close to textbook Paxos flow.

### NOS layer

`SimulatedNetwork<PaxosMessage>` provides endpoint registration and controlled faults while preserving deterministic execution order. Communication is message-based through `Mailbox<T>` awaitables.

### Consensus mechanics

- Ballots are monotonically increasing and node-scoped (`round*100 + node_id`)
- Leader election gathers quorum `Promise` messages
- Leader re-proposes highest-ballot accepted values recovered from promises
- Quorum `Accepted` responses trigger `Commit`
- Followers request missing committed entries from heartbeat leaders

## Experimental scope covered

- Safety checks: log prefix equality across replicas in tests
- Liveness under failover: new leader commits after original leader crash
- Network adversity: minority partition cannot commit
- Recovery: restored nodes synchronize with leader and apply missing commits

## Limitations and future work

- Deterministic in-memory NOS (not socket/RDMA transport)
- Static membership only (no reconfiguration)
- No persistent storage (state is in-memory)
- No snapshot compaction yet (interfaces can be extended to add this)

Potential extensions:

1. Dynamic membership via joint consensus.
2. Persistent WAL + crash recovery from disk.
3. Snapshot/compaction with installation RPC.
4. Delay/drop distributions in NOS for latency-throughput curves.

## Status

Project is complete and runnable end-to-end with tests and final paper deliverables.
