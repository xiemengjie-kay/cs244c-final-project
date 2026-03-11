#pragma once

#include <map>
#include <optional>
#include <queue>
#include <string>
#include <unordered_map>
#include <utility>
#include <variant>
#include <vector>

#include "messsage.hpp"
#include "nos.hpp"
#include "runtime.hpp"

struct NetworkHooks {
  std::function<void(int node_id, Mailbox<Message>* inbox)> register_endpoint;
  std::function<void(Message msg)> send;
  std::function<bool(int node_id)> alive;
};

class PaxosNode {
 public:
  struct RecoveredEntry {
    int slot{0};
    int ballot{0};
    std::string command;
  };

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

  void on_message(Message msg);
  void on_prepare(int from, const Prepare& prepare);
  void on_promise(int from, const Promise& promise);
  void on_accept_request(int from, const Accept& request);
  void on_accepted(int from, const Accepted& accepted);
  void on_commit(int from, const Commit& commit, int commit_ballot);
  void on_heartbeat(int from, const Heartbeat& heartbeat);
  void on_sync_request(int from, const ForwardRequest& request);
  void on_sync_data(int from, const ForwardRequest& data);

  void maybe_start_election();
  void become_leader(int ballot);
  void step_down_if_stale(int ballot);
  void propose_pending();
  void propose_slot(int slot, std::string command);
  void try_commit_slot(int slot);
  void apply_commits();
  std::vector<RecoveredEntry> collect_accepted_entries() const;

  void send(int dst, Payload payload);
  void send_typed(int dst, Payload payload, MessageType type);
  void broadcast(Payload payload);

  int quorum() const;

  int id_;
  std::vector<int> all_nodes_;
  Runtime& runtime_;
  NetworkHooks network_;
  Mailbox<Message> inbox_;

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

