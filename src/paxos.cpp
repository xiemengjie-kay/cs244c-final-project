#include "paxos.hpp"

#include <algorithm>
#include <cstddef>
#include <cstdlib>
#include <type_traits>

namespace lp {

PaxosNode::PaxosNode(int node_id, std::vector<int> all_nodes, Runtime& runtime, NetworkHooks hooks)
    : id_(node_id),
      all_nodes_(std::move(all_nodes)),
      runtime_(runtime),
      network_(std::move(hooks)),
      inbox_(runtime),
      election_timeout_ticks_(14 + static_cast<std::uint64_t>(node_id * 3)) {
  network_.register_endpoint(id_, &inbox_);
  last_leader_contact_ = runtime_.tick();
}

void PaxosNode::start() {
  auto m = message_loop();
  m.start(runtime_);
  auto e = election_loop();
  e.start(runtime_);
  auto h = heartbeat_loop();
  h.start(runtime_);
}

void PaxosNode::submit_client_command(std::string command) {
  pending_client_commands_.push(std::move(command));
  if (is_leader_) {
    propose_pending();
  }
}

DetachedTask PaxosNode::message_loop() {
  while (true) {
    auto msg = co_await inbox_.receive();
    on_message(std::move(msg));
  }
}

DetachedTask PaxosNode::election_loop() {
  while (true) {
    co_await SleepFor{runtime_, 3};
    maybe_start_election();
  }
}

DetachedTask PaxosNode::heartbeat_loop() {
  while (true) {
    co_await SleepFor{runtime_, 4};
    if (is_leader_ && network_.alive(id_)) {
      broadcast(Heartbeat{.ballot = active_ballot_, .committed_up_to = commit_index_});
    }
  }
}

void PaxosNode::on_message(PaxosMessage msg) {
  std::visit(
      [this, src = msg.src](auto&& payload) {
        using T = std::decay_t<decltype(payload)>;
        if constexpr (std::is_same_v<T, Prepare>) {
          on_prepare(src, payload);
        } else if constexpr (std::is_same_v<T, Promise>) {
          on_promise(src, payload);
        } else if constexpr (std::is_same_v<T, AcceptRequest>) {
          on_accept_request(src, payload);
        } else if constexpr (std::is_same_v<T, Accepted>) {
          on_accepted(src, payload);
        } else if constexpr (std::is_same_v<T, Commit>) {
          on_commit(src, payload);
        } else if constexpr (std::is_same_v<T, Heartbeat>) {
          on_heartbeat(src, payload);
        } else if constexpr (std::is_same_v<T, SyncRequest>) {
          on_sync_request(src, payload);
        } else if constexpr (std::is_same_v<T, SyncData>) {
          on_sync_data(src, payload);
        }
      },
      msg.payload);
}

void PaxosNode::on_prepare(int from, const Prepare& prepare) {
  bool ok = false;
  if (prepare.ballot >= promised_ballot_) {
    promised_ballot_ = prepare.ballot;
    ok = true;
    step_down_if_stale(prepare.ballot);
    last_leader_contact_ = runtime_.tick();
  }

  Promise promise{
      .ballot = prepare.ballot,
      .ok = ok,
      .from_id = id_,
      .last_committed = commit_index_,
      .accepted_entries = ok ? collect_accepted_entries() : std::vector<LogEntry>{},
  };
  send(from, promise);
}

void PaxosNode::on_promise(int /*from*/, const Promise& promise) {
  if (!prepare_state_.has_value()) {
    return;
  }
  auto& prep = *prepare_state_;
  if (prep.completed || prep.ballot != promise.ballot) {
    return;
  }
  if (!promise.ok) {
    if (promise.ballot > active_ballot_) {
      step_down_if_stale(promise.ballot);
    }
    return;
  }

  prep.promises[promise.from_id] = promise;
  if (static_cast<int>(prep.promises.size()) < quorum()) {
    return;
  }

  prep.completed = true;
  become_leader(promise.ballot);
}

void PaxosNode::on_accept_request(int from, const AcceptRequest& request) {
  bool ok = false;
  if (request.ballot >= promised_ballot_) {
    promised_ballot_ = request.ballot;
    step_down_if_stale(request.ballot);

    auto& accepted = log_[request.slot];
    if (!accepted.committed || accepted.command.empty()) {
      accepted.ballot = request.ballot;
      accepted.command = request.command;
    }
    ok = true;
  }

  send(from, Accepted{.ballot = ok ? request.ballot : promised_ballot_,
                      .slot = request.slot,
                      .ok = ok,
                      .from_id = id_});
}

void PaxosNode::on_accepted(int /*from*/, const Accepted& accepted) {
  if (!is_leader_) {
    return;
  }
  if (accepted.ballot > active_ballot_) {
    step_down_if_stale(accepted.ballot);
    return;
  }
  if (accepted.ballot != active_ballot_ || !accepted.ok) {
    return;
  }

  auto it = proposals_.find(accepted.slot);
  if (it == proposals_.end()) {
    return;
  }

  it->second.accepts[accepted.from_id] = true;
  try_commit_slot(accepted.slot);
}

void PaxosNode::on_commit(int /*from*/, const Commit& commit) {
  if (commit.ballot >= promised_ballot_) {
    promised_ballot_ = commit.ballot;
  }
  step_down_if_stale(commit.ballot);

  auto& accepted = log_[commit.slot];
  accepted.ballot = commit.ballot;
  accepted.command = commit.command;
  accepted.committed = true;

  next_proposal_slot_ = std::max(next_proposal_slot_, commit.slot + 1);
  apply_commits();
}

void PaxosNode::on_heartbeat(int from, const Heartbeat& heartbeat) {
  if (heartbeat.ballot >= promised_ballot_) {
    promised_ballot_ = heartbeat.ballot;
    if (from != id_) {
      step_down_if_stale(heartbeat.ballot);
    }
    last_leader_contact_ = runtime_.tick();
  }

  if (heartbeat.committed_up_to > commit_index_) {
    send(from, SyncRequest{.from_slot = commit_index_ + 1});
  }
}

void PaxosNode::on_sync_request(int from, const SyncRequest& request) {
  if (!is_leader_) {
    return;
  }

  std::vector<LogEntry> entries;
  for (int slot = request.from_slot; slot <= commit_index_; ++slot) {
    auto it = log_.find(slot);
    if (it != log_.end() && it->second.committed) {
      entries.push_back(LogEntry{.slot = slot, .ballot = it->second.ballot, .command = it->second.command});
    }
  }

  if (!entries.empty()) {
    send(from, SyncData{.committed_entries = std::move(entries)});
  }
}

void PaxosNode::on_sync_data(int /*from*/, const SyncData& data) {
  for (const auto& e : data.committed_entries) {
    auto& local = log_[e.slot];
    local.ballot = e.ballot;
    local.command = e.command;
    local.committed = true;
    next_proposal_slot_ = std::max(next_proposal_slot_, e.slot + 1);
  }
  apply_commits();
}

void PaxosNode::maybe_start_election() {
  if (!network_.alive(id_)) {
    return;
  }
  if (is_leader_) {
    return;
  }
  if (runtime_.tick() - last_leader_contact_ < election_timeout_ticks_) {
    return;
  }

  ++election_round_;
  int ballot = election_round_ * 100 + id_;
  if (ballot <= promised_ballot_) {
    ballot = promised_ballot_ + 1;
  }

  promised_ballot_ = ballot;
  active_ballot_ = ballot;
  last_leader_contact_ = runtime_.tick();
  prepare_state_ = PrepareState{.ballot = ballot};
  Promise self_promise{
      .ballot = ballot,
      .ok = true,
      .from_id = id_,
      .last_committed = commit_index_,
      .accepted_entries = collect_accepted_entries(),
  };
  prepare_state_->promises[id_] = std::move(self_promise);

  broadcast(Prepare{.ballot = ballot});
}

void PaxosNode::become_leader(int ballot) {
  is_leader_ = true;
  active_ballot_ = ballot;
  promised_ballot_ = std::max(promised_ballot_, ballot);
  last_leader_contact_ = runtime_.tick();

  std::unordered_map<int, LogEntry> chosen;
  if (prepare_state_.has_value()) {
    for (const auto& [_, promise] : prepare_state_->promises) {
      for (const auto& entry : promise.accepted_entries) {
        auto it = chosen.find(entry.slot);
        if (it == chosen.end() || entry.ballot > it->second.ballot) {
          chosen[entry.slot] = entry;
        }
      }
    }
  }

  for (const auto& [slot, entry] : chosen) {
    auto& accepted = log_[slot];
    if (accepted.command.empty() || accepted.ballot < entry.ballot) {
      accepted.ballot = entry.ballot;
      accepted.command = entry.command;
    }
    next_proposal_slot_ = std::max(next_proposal_slot_, slot + 1);
  }

  for (const auto& [slot, entry] : chosen) {
    propose_slot(slot, entry.command);
  }

  propose_pending();
}

void PaxosNode::step_down_if_stale(int ballot) {
  if (ballot <= active_ballot_) {
    return;
  }
  active_ballot_ = ballot;
  is_leader_ = false;
}

void PaxosNode::propose_pending() {
  if (!is_leader_) {
    return;
  }

  while (!pending_client_commands_.empty()) {
    while (log_.contains(next_proposal_slot_) && log_[next_proposal_slot_].committed) {
      ++next_proposal_slot_;
    }

    auto cmd = std::move(pending_client_commands_.front());
    pending_client_commands_.pop();
    propose_slot(next_proposal_slot_, std::move(cmd));
    ++next_proposal_slot_;
  }
}

void PaxosNode::propose_slot(int slot, std::string command) {
  if (!is_leader_) {
    return;
  }

  auto& proposal = proposals_[slot];
  proposal.slot = slot;
  proposal.command = command;
  proposal.accepts[id_] = true;

  auto& accepted = log_[slot];
  accepted.ballot = active_ballot_;
  accepted.command = command;

  broadcast(AcceptRequest{.ballot = active_ballot_, .slot = slot, .command = command});
  try_commit_slot(slot);
}

void PaxosNode::try_commit_slot(int slot) {
  if (!is_leader_) {
    return;
  }

  auto it = proposals_.find(slot);
  if (it == proposals_.end()) {
    return;
  }

  auto& proposal = it->second;
  if (proposal.committed || static_cast<int>(proposal.accepts.size()) < quorum()) {
    return;
  }

  proposal.committed = true;
  auto& accepted = log_[slot];
  accepted.committed = true;
  accepted.ballot = active_ballot_;
  accepted.command = proposal.command;

  broadcast(Commit{.ballot = active_ballot_, .slot = slot, .command = proposal.command});
  apply_commits();

  proposals_.erase(it);
}

void PaxosNode::apply_commits() {
  while (true) {
    auto next = commit_index_ + 1;
    auto it = log_.find(next);
    if (it == log_.end() || !it->second.committed) {
      break;
    }
    applied_log_.push_back(it->second.command);
    commit_index_ = next;
  }
}

std::vector<LogEntry> PaxosNode::collect_accepted_entries() const {
  std::vector<LogEntry> out;
  out.reserve(log_.size());
  for (const auto& [slot, accepted] : log_) {
    if (slot > commit_index_ && !accepted.command.empty()) {
      out.push_back(LogEntry{.slot = slot, .ballot = accepted.ballot, .command = accepted.command});
    }
  }
  return out;
}

void PaxosNode::send(int dst, PaxosPayload payload) {
  network_.send(PaxosMessage{.src = id_, .dst = dst, .payload = std::move(payload)});
}

void PaxosNode::broadcast(PaxosPayload payload) {
  for (int peer : all_nodes_) {
    send(peer, payload);
  }
}

int PaxosNode::quorum() const { return static_cast<int>(all_nodes_.size() / 2) + 1; }

PaxosCluster::PaxosCluster(int n) : network_(runtime_) {
  std::vector<int> ids;
  ids.reserve(n);
  for (int i = 1; i <= n; ++i) {
    ids.push_back(i);
  }

  network_.set_delay_fn([](const PaxosMessage& msg) {
    const int spread = std::abs(msg.src - msg.dst);
    return static_cast<std::uint64_t>(1 + (spread % 3));
  });

  nodes_.reserve(static_cast<std::size_t>(n));
  for (int id : ids) {
    NetworkHooks hooks{
        .register_endpoint = [this](int node_id, Mailbox<PaxosMessage>* inbox) {
          network_.register_endpoint(node_id, inbox);
        },
        .send = [this](PaxosMessage msg) { network_.send(std::move(msg)); },
        .alive = [this](int node_id) { return network_.alive(node_id); },
    };
    nodes_.emplace_back(id, ids, runtime_, std::move(hooks));
  }
}

void PaxosCluster::start() {
  for (auto& node : nodes_) {
    node.start();
  }
}

void PaxosCluster::run_ticks(std::size_t max_steps) { runtime_.run_steps(max_steps); }

bool PaxosCluster::submit_to_leader(const std::string& command) {
  for (auto& node : nodes_) {
    if (node.is_leader() && network_.alive(node.id())) {
      node.submit_client_command(command);
      return true;
    }
  }
  return false;
}

bool PaxosCluster::submit_to_node(int node_id, const std::string& command) {
  for (auto& node : nodes_) {
    if (node.id() == node_id && network_.alive(node.id())) {
      node.submit_client_command(command);
      return true;
    }
  }
  return false;
}

std::vector<int> PaxosCluster::leader_ids() const {
  std::vector<int> leaders;
  for (const auto& node : nodes_) {
    if (node.is_leader() && network_.alive(node.id())) {
      leaders.push_back(node.id());
    }
  }
  return leaders;
}

const PaxosNode& PaxosCluster::node(int node_id) const {
  auto it = std::find_if(nodes_.begin(), nodes_.end(), [node_id](const PaxosNode& n) { return n.id() == node_id; });
  return *it;
}

}  // namespace lp
