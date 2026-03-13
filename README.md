# A Paxos-Based Strongly Consistent Live Document Editor

In this project, we implement a strongly consistent live document editor with the
Paxos Consensus Protocol using coroutines introduced in C++20. The editor supports strongly consistent read/writes by each participating process, eliminating conflicts due to concurrent editing as in existing live document editors (e.g., Google docs, Live Share). The distributed feature keeps the service light-weight and easily manageable. We will test and report the correctness and latency of the editor as well as the Paxos implementation. If time allows, we will compare the performance with open-source c++ coroutine based Raft implementations, and implement additional optimizations such as log truncation and leader-based Paxos. A stretch goal is to ship it as a VS Code extension with a similar interface as the Live Share Extension.

Idea: localized ctrl+z, as compared to Live Share

## Dependencies
```bash
# Make sure Node.js is installed
node -v
npm -v

# Create a project folder
mkdir websocket_server
cd websocket_server
npm init -y

# Install ws library
npm install ws
```

npm install ws

node bridge.js

## Build and Run

From repo root:

```bash
cmake -S . -B build
cmake --build build -j4
```


Run local in-memory demo:

```bash
./build/paxos_demo
```

Run one Paxos node per process (3 terminals):

Node 1 - terminal 1:
```bash
./build/paxos_node --id 1 --nodes 1:15001,2:15002,3:15003
```

Node 1 - terminal 2:
```bash
node bridge.js
```

Node 1 - browser window
```bash
open test.html from your default browser
```

Nodes 2 and 3: do the same thing, except changing --id to 2 and 3, respectively

