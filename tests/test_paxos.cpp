#include <cassert>
#include <iostream>
#include <string>
#include <vector>

#include "paxos.hpp"

namespace {

void advance(lp::PaxosCluster& cluster, int rounds = 1, std::size_t steps_per_round = 300) {
  for (int i = 0; i < rounds; ++i) {
    cluster.run_ticks(steps_per_round);
  }
}

int wait_for_single_leader(lp::PaxosCluster& cluster, int rounds = 20) {
  for (int i = 0; i < rounds; ++i) {
    advance(cluster, 1);
    const auto leaders = cluster.leader_ids();
    if (leaders.size() == 1) {
      return leaders.front();
    }
  }
  return -1;
}

void assert_logs_prefix_equal(const lp::PaxosCluster& cluster, int expected_commits) {
  const auto& base = cluster.node(1).applied_commands();
  assert(static_cast<int>(base.size()) >= expected_commits);

  for (int id = 2; id <= 3; ++id) {
    const auto& log = cluster.node(id).applied_commands();
    assert(static_cast<int>(log.size()) >= expected_commits);
    for (int i = 0; i < expected_commits; ++i) {
      assert(log[static_cast<std::size_t>(i)] == base[static_cast<std::size_t>(i)]);
    }
  }
}

void test_basic_replication() {
  lp::PaxosCluster cluster(3);
  cluster.start();

  const int leader = wait_for_single_leader(cluster);
  assert(leader != -1);

  assert(cluster.submit_to_leader("cmd:a"));
  assert(cluster.submit_to_leader("cmd:b"));
  assert(cluster.submit_to_leader("cmd:c"));
  advance(cluster, 20);

  assert_logs_prefix_equal(cluster, 3);
}

void test_leader_failover() {
  lp::PaxosCluster cluster(3);
  cluster.start();

  int leader = wait_for_single_leader(cluster);
  assert(leader != -1);

  assert(cluster.submit_to_leader("before-fail"));
  advance(cluster, 10);

  cluster.crash(leader);
  advance(cluster, 12);

  int new_leader = wait_for_single_leader(cluster, 30);
  assert(new_leader != -1);
  assert(new_leader != leader);

  assert(cluster.submit_to_leader("after-fail"));
  advance(cluster, 20);

  assert(cluster.node(new_leader).commit_index() >= 2);
}

void test_partition_and_recovery() {
  lp::PaxosCluster cluster(3);
  cluster.start();

  const int leader = wait_for_single_leader(cluster);
  assert(leader != -1);

  int a = 1;
  int b = 2;
  int c = 3;
  if (leader == 1) {
    a = 2;
    b = 3;
    c = 1;
  } else if (leader == 2) {
    a = 1;
    b = 3;
    c = 2;
  } else {
    a = 1;
    b = 2;
    c = 3;
  }

  cluster.partition(c, a);
  cluster.partition(c, b);

  assert(cluster.submit_to_node(c, "minority-write"));
  advance(cluster, 15);

  assert(cluster.node(c).commit_index() == 0);

  int majority_leader = -1;
  for (int i = 0; i < 20; ++i) {
    advance(cluster, 1);
    auto leaders = cluster.leader_ids();
    for (int id : leaders) {
      if (id == a || id == b) {
        majority_leader = id;
      }
    }
    if (majority_leader != -1) {
      break;
    }
  }

  assert(majority_leader != -1);
  assert(cluster.submit_to_node(majority_leader, "majority-write"));
  advance(cluster, 20);

  cluster.heal_all();
  advance(cluster, 30);

  assert(cluster.node(1).commit_index() >= 1);
  assert(cluster.node(2).commit_index() >= 1);
  assert(cluster.node(3).commit_index() >= 1);

  const auto& log1 = cluster.node(1).applied_commands();
  const auto& log2 = cluster.node(2).applied_commands();
  const auto& log3 = cluster.node(3).applied_commands();

  assert(!log1.empty() && !log2.empty() && !log3.empty());
  assert(log1[0] == "majority-write");
  assert(log2[0] == "majority-write");
  assert(log3[0] == "majority-write");
}

void test_crash_restore_catchup() {
  lp::PaxosCluster cluster(3);
  cluster.start();

  int leader = wait_for_single_leader(cluster);
  assert(leader != -1);

  int follower = leader == 1 ? 2 : 1;
  cluster.crash(follower);

  assert(cluster.submit_to_leader("x1"));
  assert(cluster.submit_to_leader("x2"));
  advance(cluster, 20);

  cluster.restore(follower);
  advance(cluster, 40);

  assert(cluster.node(follower).commit_index() >= 2);
  const auto& log = cluster.node(follower).applied_commands();
  assert(log[0] == "x1");
  assert(log[1] == "x2");
}

}  // namespace

int main() {
  test_basic_replication();
  test_leader_failover();
  test_partition_and_recovery();
  test_crash_restore_catchup();
  std::cout << "All tests passed\n";
  return 0;
}
