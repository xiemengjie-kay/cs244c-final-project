#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cassert>
#include <chrono>
#include <cstdint>
#include <iostream>
#include <optional>
#include <thread>
#include <unordered_map>

#include "paxos_tcp_transport.hpp"

namespace {

std::uint16_t reserve_loopback_port() {
  const int fd = ::socket(AF_INET, SOCK_STREAM, 0);
  assert(fd >= 0);

  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(0);
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  const int bind_rc = ::bind(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
  assert(bind_rc == 0);

  socklen_t len = sizeof(addr);
  const int name_rc = ::getsockname(fd, reinterpret_cast<sockaddr*>(&addr), &len);
  assert(name_rc == 0);

  const std::uint16_t port = ntohs(addr.sin_port);
  ::close(fd);
  return port;
}

lp::DetachedTask receive_once(lp::Mailbox<Message>& inbox, std::optional<Message>& out) {
  out = co_await inbox.receive();
}

bool wait_for(std::uint32_t timeout_ms, const std::function<bool()>& predicate) {
  const auto start = std::chrono::steady_clock::now();
  while (true) {
    if (predicate()) {
      return true;
    }
    const auto elapsed = std::chrono::steady_clock::now() - start;
    if (std::chrono::duration_cast<std::chrono::milliseconds>(elapsed).count() >= timeout_ms) {
      return false;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(5));
  }
}

void test_send_and_receive_between_nodes() {
  const std::uint16_t p1 = reserve_loopback_port();
  const std::uint16_t p2 = reserve_loopback_port();
  assert(p1 != p2);

  const std::unordered_map<int, TcpNodeAddress> nodes{
      {1, TcpNodeAddress{.host = "127.0.0.1", .paxos_port = p1}},
      {2, TcpNodeAddress{.host = "127.0.0.1", .paxos_port = p2}},
  };

  lp::Runtime r1;
  lp::Runtime r2;
  lp::Mailbox<Message> inbox1(r1);
  lp::Mailbox<Message> inbox2(r2);

  PaxosTcpTransport t1(1, nodes);
  PaxosTcpTransport t2(2, nodes);
  t1.register_endpoint(1, &inbox1);
  t2.register_endpoint(2, &inbox2);
  t1.start();
  t2.start();

  std::optional<Message> received;
  auto rx = receive_once(inbox2, received);
  rx.start(r2);

  Message msg{};
  msg.type = MessageType::PREPARE;
  msg.from = 1;
  msg.to = 2;
  msg.term_or_ballot = 42;
  msg.payload = Prepare{.ballot = 42};
  t1.send(msg);

  const bool ok = wait_for(1000, [&]() {
    t1.pump_inbound();
    t2.pump_inbound();
    r2.run_steps(20);
    return received.has_value();
  });
  assert(ok);
  assert(received->from == 1);
  assert(received->to == 2);
  assert(received->type == MessageType::PREPARE);
  assert(received->term_or_ballot == 42);
  assert(std::get<Prepare>(received->payload).ballot == 42);

  t1.stop();
  t2.stop();
}

void test_loopback_delivery_and_crash_gate() {
  const std::uint16_t p1 = reserve_loopback_port();
  const std::unordered_map<int, TcpNodeAddress> nodes{
      {1, TcpNodeAddress{.host = "127.0.0.1", .paxos_port = p1}},
  };

  lp::Runtime runtime;
  lp::Mailbox<Message> inbox(runtime);
  PaxosTcpTransport transport(1, nodes);
  transport.register_endpoint(1, &inbox);
  transport.start();

  // When crashed, inbound should be dropped.
  transport.crash();
  Message dropped{};
  dropped.type = MessageType::HEARTBEAT;
  dropped.from = 1;
  dropped.to = 1;
  dropped.term_or_ballot = 1;
  dropped.payload = Heartbeat{.ballot = 1, .committed_up_to = 0};
  transport.send(dropped);

  for (int i = 0; i < 40; ++i) {
    transport.pump_inbound();
    assert(inbox.empty());
    std::this_thread::sleep_for(std::chrono::milliseconds(5));
  }

  transport.restore();
  std::optional<Message> received;
  auto rx = receive_once(inbox, received);
  rx.start(runtime);

  Message msg{};
  msg.type = MessageType::HEARTBEAT;
  msg.from = 1;
  msg.to = 1;
  msg.term_or_ballot = 9;
  msg.payload = Heartbeat{.ballot = 9, .committed_up_to = 7};
  transport.send(msg);

  const bool ok = wait_for(500, [&]() {
    transport.pump_inbound();
    runtime.run_steps(20);
    return received.has_value();
  });
  assert(ok);
  assert(received->type == MessageType::HEARTBEAT);
  const auto hb = std::get<Heartbeat>(received->payload);
  assert(hb.ballot == 9);
  assert(hb.committed_up_to == 7);

  transport.stop();
}

}  // namespace

int main() {
  test_send_and_receive_between_nodes();
  test_loopback_delivery_and_crash_gate();
  std::cout << "transport tests passed\n";
  return 0;
}
