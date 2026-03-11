#include <iostream>
#include <string>

#include "paxos.hpp"

namespace {

int wait_for_single_leader(lp::PaxosCluster& cluster, int rounds = 30, std::size_t steps_per_round = 300) {
  for (int i = 0; i < rounds; ++i) {
    cluster.run_ticks(steps_per_round);
    const auto leaders = cluster.leader_ids();
    if (leaders.size() == 1) {
      return leaders.front();
    }
  }
  return -1;
}

}  // namespace

int main() {
  lp::PaxosCluster cluster(3);
  cluster.start();

  const int leader = wait_for_single_leader(cluster);
  if (leader == -1) {
    std::cerr << "failed to elect a single leader\n";
    return 1;
  }

  std::cout << "leader elected: node " << leader << "\n";
  cluster.submit_to_leader("demo:edit-1");
  cluster.submit_to_leader("demo:edit-2");
  cluster.submit_to_leader("demo:edit-3");
  cluster.run_ticks(2000);

  for (int id = 1; id <= 3; ++id) {
    const auto& node = cluster.node(id);
    std::cout << "node " << id << " commit_index=" << node.commit_index() << "\n";
    const auto& log = node.applied_commands();
    for (std::size_t i = 0; i < log.size(); ++i) {
      std::cout << "  slot " << (i + 1) << ": " << log[i] << "\n";
    }
  }

  return 0;
}
