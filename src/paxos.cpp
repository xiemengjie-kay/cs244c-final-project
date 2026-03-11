#include "paxos.hpp"

#include <arpa/inet.h>

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <type_traits>

void write_i32(std::string& out, int value) {
  std::uint32_t net = htonl(static_cast<std::uint32_t>(value));
  out.append(reinterpret_cast<const char*>(&net), sizeof(net));
}

bool read_i32(const std::string& in, std::size_t& pos, int& value) {
  if (pos + sizeof(std::uint32_t) > in.size()) {
    return false;
  }
  std::uint32_t net = 0;
  std::memcpy(&net, in.data() + pos, sizeof(net));
  pos += sizeof(net);
  value = static_cast<int>(ntohl(net));
  return true;
}

void write_string(std::string& out, const std::string& s) {
  write_i32(out, static_cast<int>(s.size()));
  out.append(s);
}

bool read_string(const std::string& in, std::size_t& pos, std::string& s) {
  int len = 0;
  if (!read_i32(in, pos, len) || len < 0) {
    return false;
  }
  if (pos + static_cast<std::size_t>(len) > in.size()) {
    return false;
  }
  s.assign(in.data() + pos, static_cast<std::size_t>(len));
  pos += static_cast<std::size_t>(len);
  return true;
}

Value make_value(std::string text) {
  Value v{};
  v.isRead = false;
  v.row = 0;
  v.column_range[0] = 0;
  v.column_range[1] = 0;
  v.val = std::move(text);
  return v;
}

std::string encode_entries_blob(const std::vector<PaxosNode::RecoveredEntry>& entries) {
  std::string blob;
  write_i32(blob, static_cast<int>(entries.size()));
  for (const auto& e : entries) {
    write_i32(blob, e.slot);
    write_i32(blob, e.ballot);
    write_string(blob, e.command);
  }
  return blob;
}

bool decode_entries_blob(const std::string& blob, std::vector<PaxosNode::RecoveredEntry>& entries) {
  entries.clear();
  std::size_t pos = 0;
  int n = 0;
  if (!read_i32(blob, pos, n) || n < 0) {
    return false;
  }
  entries.reserve(static_cast<std::size_t>(n));
  for (int i = 0; i < n; ++i) {
    PaxosNode::RecoveredEntry e{};
    if (!read_i32(blob, pos, e.slot) || !read_i32(blob, pos, e.ballot) || !read_string(blob, pos, e.command)) {
      return false;
    }
    entries.push_back(std::move(e));
  }
  return true;
}

MessageType payload_type(const Payload& payload) {
  return std::visit(
      [](const auto& p) -> MessageType {
        using T = std::decay_t<decltype(p)>;
        if constexpr (std::is_same_v<T, Prepare>) {
          return MessageType::PREPARE;
        } else if constexpr (std::is_same_v<T, Promise>) {
          return MessageType::PROMISE;
        } else if constexpr (std::is_same_v<T, Accept>) {
          return MessageType::ACCEPT_REQUEST;
        } else if constexpr (std::is_same_v<T, Accepted>) {
          return MessageType::ACCEPTED;
        } else if constexpr (std::is_same_v<T, Commit>) {
          return MessageType::COMMIT;
        } else if constexpr (std::is_same_v<T, Heartbeat>) {
          return MessageType::HEARTBEAT;
        } else if constexpr (std::is_same_v<T, ForwardRequest>) {
          return MessageType::FORWARD_REQUEST;
        } else {
          return MessageType::FORWARD_REQUEST;
        }
      },
      payload);
}

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
      broadcast(Heartbeat{
          .ballot = static_cast<std::uint64_t>(active_ballot_),
          .committed_up_to = static_cast<std::uint64_t>(commit_index_),
      });
    }
  }
}

void PaxosNode::on_message(Message msg) {
  const int src = static_cast<int>(msg.from);
  std::visit(
      [this, src, msg_type = msg.type, ballot = static_cast<int>(msg.term_or_ballot)](auto&& payload) {
        using T = std::decay_t<decltype(payload)>;
        if constexpr (std::is_same_v<T, Prepare>) {
          on_prepare(src, payload);
        } else if constexpr (std::is_same_v<T, Promise>) {
          on_promise(src, payload);
        } else if constexpr (std::is_same_v<T, Accept>) {
          on_accept_request(src, payload);
        } else if constexpr (std::is_same_v<T, Accepted>) {
          on_accepted(src, payload);
        } else if constexpr (std::is_same_v<T, Commit>) {
          on_commit(src, payload, ballot);
        } else if constexpr (std::is_same_v<T, Heartbeat>) {
          on_heartbeat(src, payload);
        } else if constexpr (std::is_same_v<T, ForwardRequest>) {
          if (msg_type == MessageType::SYNC_REQUEST) {
            on_sync_request(src, payload);
          } else if (msg_type == MessageType::SYNC_DATA) {
            on_sync_data(src, payload);
          }
        }
      },
      msg.payload);
}

void PaxosNode::on_prepare(int from, const Prepare& prepare) {
  bool ok = false;
  const int ballot = static_cast<int>(prepare.ballot);
  if (ballot >= promised_ballot_) {
    promised_ballot_ = ballot;
    ok = true;
    step_down_if_stale(ballot);
    last_leader_contact_ = runtime_.tick();
  }

  Promise promise{};
  promise.ballot = static_cast<std::uint64_t>(ballot);
  promise.accepted_ballot = static_cast<std::uint64_t>(commit_index_);
  if (ok) {
    promise.accepted_value = make_value(encode_entries_blob(collect_accepted_entries()));
  }
  send(from, promise);
}

void PaxosNode::on_promise(int from, const Promise& promise) {
  if (!prepare_state_.has_value()) {
    return;
  }
  auto& prep = *prepare_state_;
  const int ballot = static_cast<int>(promise.ballot);
  if (prep.completed || prep.ballot != ballot) {
    return;
  }
  const bool ok = promise.accepted_value.has_value();
  if (!ok) {
    if (ballot > active_ballot_) {
      step_down_if_stale(ballot);
    }
    return;
  }

  prep.promises[from] = promise;
  if (static_cast<int>(prep.promises.size()) < quorum()) {
    return;
  }

  prep.completed = true;
  become_leader(ballot);
}

void PaxosNode::on_accept_request(int from, const Accept& request) {
  bool ok = false;
  const int ballot = static_cast<int>(request.ballot);
  const int slot = static_cast<int>(request.slot);
  if (ballot >= promised_ballot_) {
    promised_ballot_ = ballot;
    step_down_if_stale(ballot);

    auto& accepted = log_[slot];
    if (!accepted.committed || accepted.command.empty()) {
      accepted.ballot = ballot;
      accepted.command = request.value.val;
    }
    ok = true;
  }

  send_typed(from,
             Accepted{
                 .ballot = static_cast<std::uint64_t>(ok ? ballot : promised_ballot_),
                 .slot = static_cast<std::uint64_t>(slot),
             },
             MessageType::ACCEPTED);
}

void PaxosNode::on_accepted(int from, const Accepted& accepted) {
  if (!is_leader_) {
    return;
  }
  const int ballot = static_cast<int>(accepted.ballot);
  if (ballot > active_ballot_) {
    step_down_if_stale(ballot);
    return;
  }
  if (ballot != active_ballot_) {
    return;
  }

  const int slot = static_cast<int>(accepted.slot);
  auto it = proposals_.find(slot);
  if (it == proposals_.end()) {
    return;
  }

  it->second.accepts[from] = true;
  try_commit_slot(slot);
}

void PaxosNode::on_commit(int /*from*/, const Commit& commit, int commit_ballot) {
  if (commit_ballot >= promised_ballot_) {
    promised_ballot_ = commit_ballot;
  }
  step_down_if_stale(commit_ballot);

  const int slot = static_cast<int>(commit.slot);
  auto& accepted = log_[slot];
  accepted.ballot = commit_ballot;
  accepted.command = commit.value.val;
  accepted.committed = true;

  next_proposal_slot_ = std::max(next_proposal_slot_, slot + 1);
  apply_commits();
}

void PaxosNode::on_heartbeat(int from, const Heartbeat& heartbeat) {
  const int hb_ballot = static_cast<int>(heartbeat.ballot);
  if (hb_ballot >= promised_ballot_) {
    promised_ballot_ = hb_ballot;
    if (from != id_) {
      step_down_if_stale(hb_ballot);
    }
    last_leader_contact_ = runtime_.tick();
  }

  if (static_cast<int>(heartbeat.committed_up_to) > commit_index_) {
    ForwardRequest req{};
    req.request.client_id = 0;
    req.request.request_id = static_cast<std::uint32_t>(commit_index_ + 1);
    req.request.value = make_value("");
    send_typed(from, req, MessageType::SYNC_REQUEST);
  }
}

void PaxosNode::on_sync_request(int from, const ForwardRequest& request) {
  if (!is_leader_) {
    return;
  }

  std::vector<RecoveredEntry> entries;
  const int from_slot = static_cast<int>(request.request.request_id);
  for (int slot = from_slot; slot <= commit_index_; ++slot) {
    auto it = log_.find(slot);
    if (it != log_.end() && it->second.committed) {
      entries.push_back(RecoveredEntry{.slot = slot, .ballot = it->second.ballot, .command = it->second.command});
    }
  }

  if (!entries.empty()) {
    ForwardRequest data{};
    data.request.client_id = 0;
    data.request.request_id = static_cast<std::uint32_t>(from_slot);
    data.request.value = make_value(encode_entries_blob(entries));
    send_typed(from, data, MessageType::SYNC_DATA);
  }
}

void PaxosNode::on_sync_data(int /*from*/, const ForwardRequest& data) {
  std::vector<RecoveredEntry> entries;
  if (!decode_entries_blob(data.request.value.val, entries)) {
    return;
  }
  for (const auto& e : entries) {
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
  Promise self_promise{};
  self_promise.ballot = static_cast<std::uint64_t>(ballot);
  self_promise.accepted_ballot = static_cast<std::uint64_t>(commit_index_);
  self_promise.accepted_value = make_value(encode_entries_blob(collect_accepted_entries()));
  prepare_state_->promises[id_] = std::move(self_promise);

  broadcast(Prepare{.ballot = static_cast<std::uint64_t>(ballot)});
}

void PaxosNode::become_leader(int ballot) {
  is_leader_ = true;
  active_ballot_ = ballot;
  promised_ballot_ = std::max(promised_ballot_, ballot);
  last_leader_contact_ = runtime_.tick();

  std::unordered_map<int, RecoveredEntry> chosen;
  if (prepare_state_.has_value()) {
    for (const auto& [_, promise] : prepare_state_->promises) {
      if (!promise.accepted_value.has_value()) {
        continue;
      }
      std::vector<RecoveredEntry> entries;
      if (!decode_entries_blob(promise.accepted_value->val, entries)) {
        continue;
      }
      for (const auto& entry : entries) {
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

  Accept req{};
  req.ballot = static_cast<std::uint64_t>(active_ballot_);
  req.slot = static_cast<std::uint64_t>(slot);
  req.value = make_value(command);
  broadcast(req);
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

  Commit commit{};
  commit.slot = static_cast<std::uint64_t>(slot);
  commit.value = make_value(proposal.command);
  broadcast(commit);
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

std::vector<PaxosNode::RecoveredEntry> PaxosNode::collect_accepted_entries() const {
  std::vector<RecoveredEntry> out;
  out.reserve(log_.size());
  for (const auto& [slot, accepted] : log_) {
    if (slot > commit_index_ && !accepted.command.empty()) {
      out.push_back(RecoveredEntry{.slot = slot, .ballot = accepted.ballot, .command = accepted.command});
    }
  }
  return out;
}

void PaxosNode::send(int dst, Payload payload) {
  const MessageType type = payload_type(payload);
  send_typed(dst, std::move(payload), type);
}

void PaxosNode::send_typed(int dst, Payload payload, MessageType type) {
  std::uint64_t term_or_ballot = 0;
  std::visit(
      [&term_or_ballot, type, this](const auto& p) {
        using T = std::decay_t<decltype(p)>;
        if constexpr (std::is_same_v<T, Prepare> || std::is_same_v<T, Promise> || std::is_same_v<T, Accept> ||
                      std::is_same_v<T, Accepted> || std::is_same_v<T, Heartbeat>) {
          term_or_ballot = p.ballot;
        } else if constexpr (std::is_same_v<T, Commit>) {
          if (type == MessageType::COMMIT) {
            term_or_ballot = static_cast<std::uint64_t>(active_ballot_);
          }
        }
      },
      payload);

  network_.send(Message{
      .type = type,
      .from = static_cast<NodeId>(id_),
      .to = static_cast<NodeId>(dst),
      .term_or_ballot = term_or_ballot,
      .payload = std::move(payload),
  });
}

void PaxosNode::broadcast(Payload payload) {
  for (int peer : all_nodes_) {
    send(peer, payload);
  }
}

int PaxosNode::quorum() const { return static_cast<int>(all_nodes_.size() / 2) + 1; }
