#pragma once

#include <atomic>
#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "nos.hpp"
#include "paxos.hpp"


struct TcpNodeAddress {
  std::string host;
  std::uint16_t paxos_port;
};

class PaxosTcpTransport {
 public:
  PaxosTcpTransport(int local_node_id, std::unordered_map<int, TcpNodeAddress> nodes);
  ~PaxosTcpTransport();

  void register_endpoint(int node_id, Mailbox<Message>* inbox);
  void send(Message msg);
  bool alive(int node_id) const;

  void crash();
  void restore();

  void start();
  void stop();

  // Bridge data from network thread to runtime thread.
  void pump_inbound();

 private:
  int local_node_id_;
  std::unordered_map<int, TcpNodeAddress> nodes_;

  Mailbox<Message>* inbox_{nullptr};

  std::atomic<bool> running_{false};
  std::atomic<bool> crashed_{false};
  int node_listen_fd_{-1};
  std::unique_ptr<std::thread> io_thread_;

  std::mutex inbound_mu_;
  std::deque<Message> inbound_paxos_;

  std::mutex outbound_mu_;
  std::deque<Message> outbound_paxos_;

  void io_loop();
};

