#include <iostream>

#include "paxos.hpp"

int main() {
  lp::PaxosCluster cluster(3);
  cluster.start();

  cluster.run_ticks(1000);

  cluster.submit_to_leader("set x=1");
  cluster.submit_to_leader("set y=2");
  cluster.run_ticks(2000);

  for (int id = 1; id <= 3; ++id) {
    const auto& node = cluster.node(id);
    std::cout << "node " << id << " committed=" << node.commit_index() << "\n";
    for (const auto& cmd : node.applied_commands()) {
      std::cout << "  - " << cmd << "\n";
    }
  }

  return 0;
}
