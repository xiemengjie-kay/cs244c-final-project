#include <algorithm>
#include <chrono>
#include <cstdint>
#include <exception>
#include <iostream>
#include <mutex>
#include <queue>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

#include "paxos.hpp"
#include "paxos_tcp_transport.hpp"
#include "interface.hpp"

int main(int argc, char** argv) {
  int node_id = -1;
  std::string nodes_spec;

  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "--id" && i + 1 < argc) {
      node_id = std::stoi(argv[++i]);
    } else if (arg == "--nodes" && i + 1 < argc) {
      nodes_spec = argv[++i];
    } else if (arg == "--help") {
      usage(argv[0]);
      return 0;
    }
  }

  if (node_id <= 0 || nodes_spec.empty()) {
    usage(argv[0]);
    return 1;
  }

  try {
    auto nodes = parse_nodes(nodes_spec);
    std::cout << "Starting " << node_id << " in a cluster of " << nodes.size() << std::endl;

    if (!nodes.contains(node_id)) {
      throw std::runtime_error("--id does not exist in --nodes");
    }

    std::vector<int> all_nodes;
    all_nodes.reserve(nodes.size());
    for (const auto& [id, _] : nodes) {
      (void)_;
      all_nodes.push_back(id);
    }
    std::sort(all_nodes.begin(), all_nodes.end());

    Runtime runtime;
    PaxosTcpTransport transport(node_id, nodes);

    NetworkHooks hooks{
        .register_endpoint = [&transport](int id, Mailbox<Message>* inbox) {
          transport.register_endpoint(id, inbox);
        },
        .send = [&transport](Message msg) { transport.send(std::move(msg)); },
        .alive = [&transport](int id) { return transport.alive(id); },
    };

    PaxosNode node(node_id, all_nodes, runtime, std::move(hooks));
    std::cout << "Starting " << node_id << " in a cluster of " << all_nodes.size() << std::endl;
    
    node.start();
    transport.start();

    std::mutex input_mu;
    std::queue<std::string> input_queue;
    bool stop = false;

    // application code starts

    // catch terminal input in a separate thread
    // TODO: turn this into catching events from browser
    std::thread input_thread([&]() {
      std::string line;
      while (std::getline(std::cin, line)) {
        std::lock_guard<std::mutex> lock(input_mu);
        input_queue.push(std::move(line));
      }
      std::lock_guard<std::mutex> lock(input_mu);
      input_queue.push("/quit");
    });

    std::cout << "node " << node_id << " started on paxos@" << nodes[node_id].host << ":" << nodes[node_id].paxos_port
              << "\n";

    int last_commit = 0;
    while (!stop) {
      transport.pump_inbound();
      runtime.run_steps(1);

      auto handle_command = [&](std::string cmd) {
        if (cmd == "/status") {
          std::ostringstream out;
          out << "node " << node_id << " leader=" << (node.is_leader() ? "yes" : "no")
              << " ballot=" << node.leader_ballot() << " commit_index=" << node.commit_index();
          std::cout << out.str() << "\n";
          return;
        }
        if (cmd == "/crash") {
          transport.crash();
          std::cout << "node crashed\n";
          return;
        }
        if (cmd == "/restore") {
          transport.restore();
          std::cout << "node restored\n";
          return;
        }
        if (cmd == "/quit") {
          stop = true;
          return;
        }
        node.submit_client_command(std::move(cmd));
      };

      // serve terminal commands
      {
        std::lock_guard<std::mutex> lock(input_mu);
        while (!input_queue.empty()) {
          std::string cmd = std::move(input_queue.front());
          input_queue.pop();
          handle_command(std::move(cmd));
        }
      }

      // handle committed entries
      if (node.commit_index() > last_commit) {
        const auto& applied = node.applied_commands();
        for (int i = last_commit; i < node.commit_index(); ++i) {
          std::string command  = applied[static_cast<std::size_t>(i)];
          EditOperation op;
          bool parsed = parse_command(command, op);
          if (parsed) {
            // TODO: turn this into actual commands on browser instead of just printing
            std::cout << (int)op.type << " " << op.position << " " << op.text << " " << op.length << "\n";
          }
          const std::string committed =
              "node " + std::to_string(node_id) + " committed slot " + std::to_string(i + 1) +
              ": " + applied[static_cast<std::size_t>(i)];
          std::cout << committed << "\n";
        }
        last_commit = node.commit_index();
      }

      std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }

    transport.stop();
    if (input_thread.joinable()) {
      input_thread.join();
    }
    return 0;
  } catch (const std::exception& ex) {
    std::cerr << "fatal: " << ex.what() << "\n";
    return 1;
  }
}
