#pragma once

#include <map>
#include <optional>
#include <queue>
#include <string>
#include <unordered_map>
#include <utility>
#include <variant>
#include <vector>

#include "nos.hpp"
#include "runtime.hpp"

namespace lp {

struct LogEntry {
  int slot;
  int ballot;
  std::string command;
};

struct Prepare {
  int ballot;
};

struct Promise {
  int ballot;
  bool ok;
  int from_id;
  int last_committed;
  std::vector<LogEntry> accepted_entries;
};

struct AcceptRequest {
  int ballot;
  int slot;
  std::string command;
};

struct Accepted {
  int ballot;
  int slot;
  bool ok;
  int from_id;
};

struct Commit {
  int ballot;
  int slot;
  std::string command;
};

struct Heartbeat {
  int ballot;
  int committed_up_to;
};

struct SyncRequest {
  int from_slot;
};

struct SyncData {
  std::vector<LogEntry> committed_entries;
};

using PaxosPayload =
    std::variant<Prepare, Promise, AcceptRequest, Accepted, Commit, Heartbeat, SyncRequest, SyncData>;

struct PaxosMessage {
  int src;
  int dst;
  PaxosPayload payload;
};

struct NetworkHooks {
  std::function<void(int node_id, Mailbox<PaxosMessage>* inbox)> register_endpoint;
  std::function<void(PaxosMessage msg)> send;
  std::function<bool(int node_id)> alive;
};

class PaxosNode {
 public:
  PaxosNode(int node_id, std::vector<int> all_nodes, Runtime& runtime, NetworkHooks hooks);

  void start();
  void submit_client_command(std::string command);

  int id() const { return id_; }
  bool is_leader() const { return is_leader_; }
  int leader_ballot() const { return active_ballot_; }
  int commit_index() const { return commit_index_; }
  const std::vector<std::string>& applied_commands() const { return applied_log_; }

 private:
  struct AcceptedValue {
    int ballot{-1};
    std::string command;
    bool committed{false};
  };

  struct PrepareState {
    int ballot{-1};
    std::unordered_map<int, Promise> promises;
    bool completed{false};
  };

  struct SlotProposal {
    int slot;
    std::string command;
    std::unordered_map<int, bool> accepts;
    bool committed{false};
  };

  DetachedTask message_loop();
  DetachedTask election_loop();
  DetachedTask heartbeat_loop();

  void on_message(PaxosMessage msg);
  void on_prepare(int from, const Prepare& prepare);
  void on_promise(int from, const Promise& promise);
  void on_accept_request(int from, const AcceptRequest& request);
  void on_accepted(int from, const Accepted& accepted);
  void on_commit(int from, const Commit& commit);
  void on_heartbeat(int from, const Heartbeat& heartbeat);
  void on_sync_request(int from, const SyncRequest& request);
  void on_sync_data(int from, const SyncData& data);

  void maybe_start_election();
  void become_leader(int ballot);
  void step_down_if_stale(int ballot);
  void propose_pending();
  void propose_slot(int slot, std::string command);
  void try_commit_slot(int slot);
  void apply_commits();
  std::vector<LogEntry> collect_accepted_entries() const;

  void send(int dst, PaxosPayload payload);
  void broadcast(PaxosPayload payload);

  int quorum() const;

  int id_;
  std::vector<int> all_nodes_;
  Runtime& runtime_;
  NetworkHooks network_;
  Mailbox<PaxosMessage> inbox_;

  bool is_leader_{false};
  int active_ballot_{-1};
  int promised_ballot_{-1};
  int election_round_{0};

  int commit_index_{0};
  int next_proposal_slot_{1};

  std::uint64_t last_leader_contact_{0};
  std::uint64_t election_timeout_ticks_;

  std::unordered_map<int, AcceptedValue> log_;
  std::vector<std::string> applied_log_;
  std::queue<std::string> pending_client_commands_;

  std::optional<PrepareState> prepare_state_;
  std::unordered_map<int, SlotProposal> proposals_;
};

class PaxosCluster {
 public:
  explicit PaxosCluster(int n);

  void start();
  void run_ticks(std::size_t max_steps = 100000);

  bool submit_to_leader(const std::string& command);
  bool submit_to_node(int node_id, const std::string& command);

  void crash(int node_id) { network_.crash(node_id); }
  void restore(int node_id) { network_.restore(node_id); }
  void partition(int a, int b) { network_.partition(a, b); }
  void heal(int a, int b) { network_.heal(a, b); }
  void heal_all() { network_.heal_all(); }

  std::vector<int> leader_ids() const;
  const PaxosNode& node(int node_id) const;

 private:
  Runtime runtime_;
  SimulatedNetwork<PaxosMessage> network_;
  std::vector<PaxosNode> nodes_;
};

}  // namespace lp
