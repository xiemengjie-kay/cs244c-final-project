#include "paxos_tcp_transport.hpp"

#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdint>
#include <cstring>
#include <limits>
#include <optional>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include "messsage.hpp"

constexpr std::size_t kMaxFrameBytes = 1U << 20;

static void write_i32(std::vector<std::uint8_t>& out, int value) {
  const std::uint32_t net = htonl(static_cast<std::uint32_t>(value));
  const auto* p = reinterpret_cast<const std::uint8_t*>(&net);
  out.insert(out.end(), p, p + sizeof(net));
}

static bool read_i32(const std::vector<std::uint8_t>& in, std::size_t& pos, int& value) {
  if (pos + sizeof(std::uint32_t) > in.size()) {
    return false;
  }
  std::uint32_t net = 0;
  std::memcpy(&net, in.data() + pos, sizeof(net));
  pos += sizeof(net);
  value = static_cast<int>(ntohl(net));
  return true;
}

static void write_string(std::vector<std::uint8_t>& out, const std::string& s) {
  write_i32(out, static_cast<int>(s.size()));
  out.insert(out.end(), s.begin(), s.end());
}

static bool read_string(const std::vector<std::uint8_t>& in, std::size_t& pos, std::string& s) {
  int len = 0;
  if (!read_i32(in, pos, len) || len < 0) {
    return false;
  }
  if (pos + static_cast<std::size_t>(len) > in.size()) {
    return false;
  }
  s.assign(reinterpret_cast<const char*>(in.data() + pos), static_cast<std::size_t>(len));
  pos += static_cast<std::size_t>(len);
  return true;
}

static void write_u64(std::vector<std::uint8_t>& out, std::uint64_t value) {
  std::uint64_t net = value;
#if __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
  net = ((net & 0x00000000000000FFULL) << 56) | ((net & 0x000000000000FF00ULL) << 40) |
        ((net & 0x0000000000FF0000ULL) << 24) | ((net & 0x00000000FF000000ULL) << 8) |
        ((net & 0x000000FF00000000ULL) >> 8) | ((net & 0x0000FF0000000000ULL) >> 24) |
        ((net & 0x00FF000000000000ULL) >> 40) | ((net & 0xFF00000000000000ULL) >> 56);
#endif
  const auto* p = reinterpret_cast<const std::uint8_t*>(&net);
  out.insert(out.end(), p, p + sizeof(net));
}

static bool read_u64(const std::vector<std::uint8_t>& in, std::size_t& pos, std::uint64_t& value) {
  if (pos + sizeof(std::uint64_t) > in.size()) {
    return false;
  }
  std::uint64_t net = 0;
  std::memcpy(&net, in.data() + pos, sizeof(net));
  pos += sizeof(net);
#if __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
  value = ((net & 0x00000000000000FFULL) << 56) | ((net & 0x000000000000FF00ULL) << 40) |
          ((net & 0x0000000000FF0000ULL) << 24) | ((net & 0x00000000FF000000ULL) << 8) |
          ((net & 0x000000FF00000000ULL) >> 8) | ((net & 0x0000FF0000000000ULL) >> 24) |
          ((net & 0x00FF000000000000ULL) >> 40) | ((net & 0xFF00000000000000ULL) >> 56);
#else
  value = net;
#endif
  return true;
}

static void write_u32(std::vector<std::uint8_t>& out, std::uint32_t value) {
  const std::uint32_t net = htonl(value);
  const auto* p = reinterpret_cast<const std::uint8_t*>(&net);
  out.insert(out.end(), p, p + sizeof(net));
}

static bool read_u32(const std::vector<std::uint8_t>& in, std::size_t& pos, std::uint32_t& value) {
  if (pos + sizeof(std::uint32_t) > in.size()) {
    return false;
  }
  std::uint32_t net = 0;
  std::memcpy(&net, in.data() + pos, sizeof(net));
  pos += sizeof(net);
  value = ntohl(net);
  return true;
}

static void write_value(std::vector<std::uint8_t>& out, const Value& value) {
  write_string(out, value.val);
}

static bool read_value(const std::vector<std::uint8_t>& in, std::size_t& pos, Value& value) {
  if (!read_string(in, pos, value.val)) {
    return false;
  }
  return true;
}

static std::vector<std::uint8_t> serialize(const Message& msg) {
  std::vector<std::uint8_t> out;
  out.reserve(256);

  write_i32(out, static_cast<int>(msg.from));
  write_i32(out, static_cast<int>(msg.to));
  write_i32(out, static_cast<int>(msg.type));
  write_u64(out, msg.term_or_ballot);

  std::visit(
      [&out](const auto& payload) {
        using T = std::decay_t<decltype(payload)>;
        if constexpr (std::is_same_v<T, Prepare>) {
          write_u64(out, payload.ballot);
        } else if constexpr (std::is_same_v<T, Promise>) {
          write_u64(out, payload.ballot);
          write_u64(out, payload.accepted_ballot);
          write_i32(out, payload.accepted_value.has_value() ? 1 : 0);
          if (payload.accepted_value.has_value()) {
            write_value(out, *payload.accepted_value);
          }
        } else if constexpr (std::is_same_v<T, Accept>) {
          write_u64(out, payload.ballot);
          write_u64(out, payload.slot);
          write_value(out, payload.value);
        } else if constexpr (std::is_same_v<T, Accepted>) {
          write_u64(out, payload.ballot);
          write_u64(out, payload.slot);
        } else if constexpr (std::is_same_v<T, Commit>) {
          write_u64(out, payload.slot);
          write_value(out, payload.value);
        } else if constexpr (std::is_same_v<T, Heartbeat>) {
          write_u64(out, payload.ballot);
          write_u64(out, payload.committed_up_to);
        } else if constexpr (std::is_same_v<T, ClientRequest>) {
          write_u32(out, payload.client_id);
          write_u32(out, payload.request_id);
          write_value(out, payload.value);
        } else if constexpr (std::is_same_v<T, ClientResponse>) {
          write_i32(out, payload.succ ? 1 : 0);
          write_i32(out, payload.isWrite ? 1 : 0);
        } else if constexpr (std::is_same_v<T, ForwardRequest>) {
          write_u32(out, payload.request.client_id);
          write_u32(out, payload.request.request_id);
          write_value(out, payload.request.value);
        }
      },
      msg.payload);
  return out;
}

static std::optional<Message> deserialize(const std::vector<std::uint8_t>& in) {
  Message msg{};
  int from = -1;
  int to = -1;
  int type = -1;
  std::size_t pos = 0;
  if (!read_i32(in, pos, from) || !read_i32(in, pos, to) || !read_i32(in, pos, type)) {
    return std::nullopt;
  }
  if (from < 0 || to < 0) {
    return std::nullopt;
  }
  msg.from = static_cast<NodeId>(from);
  msg.to = static_cast<NodeId>(to);

  if (!read_u64(in, pos, msg.term_or_ballot)) {
    return std::nullopt;
  }

  const auto type_enum = static_cast<MessageType>(type);
  msg.type = type_enum;

  switch (type_enum) {
    case MessageType::PREPARE: {
      Prepare p{};
      if (!read_u64(in, pos, p.ballot)) {
        return std::nullopt;
      }
      msg.payload = p;
      break;
    }
    case MessageType::PROMISE: {
      Promise p{};
      int has_value = 0;
      if (!read_u64(in, pos, p.ballot) || !read_u64(in, pos, p.accepted_ballot) || !read_i32(in, pos, has_value)) {
        return std::nullopt;
      }
      if (has_value != 0) {
        Value v{};
        if (!read_value(in, pos, v)) {
          return std::nullopt;
        }
        p.accepted_value = std::move(v);
      }
      msg.payload = std::move(p);
      break;
    }
    case MessageType::ACCEPT_REQUEST: {
      Accept p{};
      if (!read_u64(in, pos, p.ballot) || !read_u64(in, pos, p.slot) || !read_value(in, pos, p.value)) {
        return std::nullopt;
      }
      msg.payload = std::move(p);
      break;
    }
    case MessageType::ACCEPTED: {
      Accepted p{};
      if (!read_u64(in, pos, p.ballot) || !read_u64(in, pos, p.slot)) {
        return std::nullopt;
      }
      msg.payload = p;
      break;
    }
    case MessageType::COMMIT: {
      Commit p{};
      if (!read_u64(in, pos, p.slot) || !read_value(in, pos, p.value)) {
        return std::nullopt;
      }
      msg.payload = std::move(p);
      break;
    }
    case MessageType::HEARTBEAT: {
      Heartbeat p{};
      if (!read_u64(in, pos, p.ballot) || !read_u64(in, pos, p.committed_up_to)) {
        return std::nullopt;
      }
      msg.payload = p;
      break;
    }
    case MessageType::SYNC_REQUEST: {
      ForwardRequest p{};
      if (!read_u32(in, pos, p.request.client_id) || !read_u32(in, pos, p.request.request_id) ||
          !read_value(in, pos, p.request.value)) {
        return std::nullopt;
      }
      msg.payload = p;
      break;
    }
    case MessageType::SYNC_DATA: {
      ForwardRequest p{};
      if (!read_u32(in, pos, p.request.client_id) || !read_u32(in, pos, p.request.request_id) ||
          !read_value(in, pos, p.request.value)) {
        return std::nullopt;
      }
      msg.payload = std::move(p);
      break;
    }
    case MessageType::CLIENT_REQUEST: {
      ClientRequest p{};
      if (!read_u32(in, pos, p.client_id) || !read_u32(in, pos, p.request_id) || !read_value(in, pos, p.value)) {
        return std::nullopt;
      }
      msg.payload = std::move(p);
      break;
    }
    case MessageType::CLIENT_RESPONSE: {
      ClientResponse p{};
      int succ = 0;
      int is_write = 0;
      if (!read_i32(in, pos, succ) || !read_i32(in, pos, is_write)) {
        return std::nullopt;
      }
      p.succ = (succ != 0);
      p.isWrite = (is_write != 0);
      msg.payload = p;
      break;
    }
    case MessageType::FORWARD_REQUEST: {
      ForwardRequest p{};
      if (!read_u32(in, pos, p.request.client_id) || !read_u32(in, pos, p.request.request_id) ||
          !read_value(in, pos, p.request.value)) {
        return std::nullopt;
      }
      msg.payload = std::move(p);
      break;
    }
    default:
      return std::nullopt;
  }
  return msg;
}

static bool set_non_blocking(int fd) {
  const int flags = ::fcntl(fd, F_GETFL, 0);
  if (flags < 0) {
    return false;
  }
  return ::fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0;
}

static void set_no_sigpipe(int fd) {
#ifdef SO_NOSIGPIPE
  int one = 1;
  (void)::setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof(one));
#else
  (void)fd;
#endif
}

static bool send_all(int fd, const std::uint8_t* data, std::size_t size) {
  std::size_t sent = 0;
  while (sent < size) {
    const ssize_t n = ::send(fd, data + sent, size - sent, 0);
    if (n > 0) {
      sent += static_cast<std::size_t>(n);
      continue;
    }
    if (n == 0) {
      return false;
    }
    if (errno == EINTR) {
      continue;
    }
    return false;
  }
  return true;
}

static bool send_frame(int fd, const std::vector<std::uint8_t>& payload) {
  const std::uint32_t net_len = htonl(static_cast<std::uint32_t>(payload.size()));
  if (!send_all(fd, reinterpret_cast<const std::uint8_t*>(&net_len), sizeof(net_len))) {
    return false;
  }
  return send_all(fd, payload.data(), payload.size());
}

static int create_listener(std::uint16_t port) {
  int fd = ::socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0) {
    return -1;
  }

  int reuse = 1;
  (void)::setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
  set_no_sigpipe(fd);

  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(port);
  // addr.sin_addr.s_addr = htonl(INADDR_ANY);
  if (::bind(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
    ::close(fd);
    return -1;
  }
  if (::listen(fd, 64) != 0) {
    ::close(fd);
    return -1;
  }
  if (!set_non_blocking(fd)) {
    ::close(fd);
    return -1;
  }
  return fd;
}

static int connect_to_host_port(const std::string& host, std::uint16_t port) {
  addrinfo hints{};
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_STREAM;

  addrinfo* result = nullptr;
  const int rc = ::getaddrinfo(host.c_str(), std::to_string(port).c_str(), &hints, &result);
  if (rc != 0 || result == nullptr) {
    return -1;
  }

  int fd = -1;
  for (addrinfo* ai = result; ai != nullptr; ai = ai->ai_next) {
    fd = ::socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
    if (fd < 0) {
      continue;
    }
    set_no_sigpipe(fd);
    timeval timeout{};
    timeout.tv_sec = 0;
    timeout.tv_usec = 300000;
    (void)::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    (void)::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    if (::connect(fd, ai->ai_addr, ai->ai_addrlen) == 0) {
      break;
    }
    ::close(fd);
    fd = -1;
  }
  ::freeaddrinfo(result);
  return fd;
}

static std::string ensure_newline(std::string s) {
  if (s.empty() || s.back() != '\n') {
    s.push_back('\n');
  }
  return s;
}

PaxosTcpTransport::PaxosTcpTransport(int local_node_id,
                                     std::unordered_map<int, TcpNodeAddress> nodes)
    : local_node_id_(local_node_id), nodes_(std::move(nodes)) {
  if (!nodes_.contains(local_node_id_)) {
    throw std::runtime_error("local node id not found in node map");
  }
}

PaxosTcpTransport::~PaxosTcpTransport() { stop(); }

void PaxosTcpTransport::register_endpoint(int node_id, Mailbox<Message>* inbox) {
  if (node_id == local_node_id_) {
    inbox_ = inbox;
  }
}

void PaxosTcpTransport::send(Message msg) {
  std::lock_guard<std::mutex> lock(outbound_mu_);
  outbound_paxos_.push_back(std::move(msg));
}

bool PaxosTcpTransport::alive(int node_id) const {
  if (node_id == local_node_id_) {
    return !crashed_.load();
  }
  return true;
}

void PaxosTcpTransport::crash() { crashed_.store(true); }

void PaxosTcpTransport::restore() { crashed_.store(false); }

void PaxosTcpTransport::start() {
  if (running_.load()) {
    return;
  }

  node_listen_fd_ = create_listener(nodes_.at(local_node_id_).paxos_port);
  if (node_listen_fd_ < 0) {
    throw std::runtime_error("failed to create paxos listener");
  }

  running_.store(true);
  io_thread_ = std::make_unique<std::thread>([this]() { io_loop(); });
}

void PaxosTcpTransport::stop() {
  if (!running_.exchange(false)) {
    return;
  }
  if (io_thread_ && io_thread_->joinable()) {
    io_thread_->join();
  }
  io_thread_.reset();

  if (node_listen_fd_ >= 0) {
    ::close(node_listen_fd_);
    node_listen_fd_ = -1;
  }
}

void PaxosTcpTransport::pump_inbound() {
  if (inbox_ == nullptr) {
    return;
  }
  std::deque<Message> local;
  {
    std::lock_guard<std::mutex> lock(inbound_mu_);
    local.swap(inbound_paxos_);
  }
  while (!local.empty()) {
    inbox_->push(std::move(local.front()));
    local.pop_front();
  }
}

void PaxosTcpTransport::io_loop() {
  std::unordered_map<int, int> outbound_peer_fd_by_id;
  std::unordered_map<int, std::vector<std::uint8_t>> inbound_peer_buffers;

  while (running_.load()) {
    {
      std::deque<Message> outbound;
      {
        std::lock_guard<std::mutex> lock(outbound_mu_);
        outbound.swap(outbound_paxos_);
      }

      while (!outbound.empty()) {
        Message msg = std::move(outbound.front());
        outbound.pop_front();

        if (crashed_.load()) {
          continue;
        }
        
        // destination is itself
        if (static_cast<int>(msg.to) == local_node_id_) {
          std::lock_guard<std::mutex> lock(inbound_mu_);
          inbound_paxos_.push_back(std::move(msg));
          continue;
        }

        const auto dst = static_cast<int>(msg.to);
        const auto node_it = nodes_.find(dst);
        if (node_it == nodes_.end()) {
          continue;
        }

        int fd = -1;
        if (const auto fd_it = outbound_peer_fd_by_id.find(dst); fd_it != outbound_peer_fd_by_id.end()) {
          fd = fd_it->second;
        } else {
          fd = connect_to_host_port(node_it->second.host, node_it->second.paxos_port);
          if (fd < 0) {
            continue;
          }
          outbound_peer_fd_by_id[dst] = fd;
        }

        const auto frame = serialize(msg);
        if (send_frame(fd, frame)) {
          continue;
        }

        ::close(fd);
        outbound_peer_fd_by_id.erase(dst);
      }
    }

    fd_set readfds;
    FD_ZERO(&readfds);
    int max_fd = -1;

    if (node_listen_fd_ >= 0) {
      FD_SET(node_listen_fd_, &readfds);
      max_fd = std::max(max_fd, node_listen_fd_);
    }
    for (const auto& [fd, _] : inbound_peer_buffers) {
      FD_SET(fd, &readfds);
      max_fd = std::max(max_fd, fd);
    }

    timeval tv{};
    tv.tv_sec = 0;
    tv.tv_usec = 50000;
    // wait for up to 50ms for inbound data
    const int rc = ::select(max_fd + 1, &readfds, nullptr, nullptr, &tv);
    if (rc < 0) {
      if (errno == EINTR) {
        continue;
      }
      break;
    }
    if (rc == 0) {
      continue;
    }

    // handle new inbound connections
    if (node_listen_fd_ >= 0 && FD_ISSET(node_listen_fd_, &readfds)) {
      while (true) {
        sockaddr_storage addr{};
        socklen_t len = sizeof(addr);
        const int fd = ::accept(node_listen_fd_, reinterpret_cast<sockaddr*>(&addr), &len);
        if (fd < 0) {
          if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) {
            break;
          }
          break;
        }
        set_no_sigpipe(fd);
        if (!set_non_blocking(fd)) {
          ::close(fd);
          continue;
        }
        inbound_peer_buffers.emplace(fd, std::vector<std::uint8_t>{});
      }
    }

    std::vector<int> peer_fds;
    peer_fds.reserve(inbound_peer_buffers.size());
    for (const auto& [fd, _] : inbound_peer_buffers) {
      peer_fds.push_back(fd);
    }
    for (int fd : peer_fds) {
      if (!FD_ISSET(fd, &readfds)) {
        continue;
      }
      auto it = inbound_peer_buffers.find(fd);
      if (it == inbound_peer_buffers.end()) {
        continue;
      }

      bool peer_alive = true;
      while (true) {
        std::array<std::uint8_t, 4096> buf{};
        // receive data from the peer
        const ssize_t n = ::recv(fd, buf.data(), buf.size(), 0);
        if (n > 0) {
          // append the received data to the buffer
          it->second.insert(it->second.end(), buf.begin(), buf.begin() + n);
          continue;
        }
        if (n == 0) {
          peer_alive = false;
          break;
        }
        if (errno == EINTR) {
          continue;
        }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
          break;
        }
        peer_alive = false;
        break;
      }

      if (!peer_alive) {
        ::close(fd);
        inbound_peer_buffers.erase(it);
        continue;
      }

      auto& bytes = it->second;
      std::size_t offset = 0;
      std::deque<Message> decoded;
      // decode the messages from the buffer
      while (bytes.size() - offset >= sizeof(std::uint32_t)) {
        // read the length of the next message
        std::uint32_t net_len = 0;
        std::memcpy(&net_len, bytes.data() + offset, sizeof(net_len));
        // convert the length to network order
        const std::size_t frame_len = ntohl(net_len);
        // check if the length is valid
        if (frame_len == 0 || frame_len > kMaxFrameBytes) {
          peer_alive = false;
          break;
        }
        // check if the buffer has enough data for the next message
        if (bytes.size() - offset < sizeof(std::uint32_t) + frame_len) {
          break;
        }
        // extract the message from the buffer
        const std::size_t start = offset + sizeof(std::uint32_t);
        std::vector<std::uint8_t> frame(bytes.begin() + static_cast<std::ptrdiff_t>(start),
                                        bytes.begin() + static_cast<std::ptrdiff_t>(start + frame_len));
        offset += sizeof(std::uint32_t) + frame_len;

        auto msg = deserialize(frame);
        if (!msg.has_value()) {
          continue;
        }
        // check if the message is valid
        if (!msg.has_value()) {
          continue;
        }
        // check if the message is for this node
        if (static_cast<int>(msg->to) != local_node_id_ || crashed_.load()) {
          continue;
        }
        decoded.push_back(std::move(*msg));
      }
      if (!peer_alive) {
        ::close(fd);
        inbound_peer_buffers.erase(it);
        continue;
      }

      // remove the processed data from the buffer
      if (offset > 0) {
        bytes.erase(bytes.begin(), bytes.begin() + static_cast<std::ptrdiff_t>(offset));
      }

      // push the decoded messages to the inbound queue
      if (!decoded.empty()) {
        std::lock_guard<std::mutex> lock(inbound_mu_);
        while (!decoded.empty()) {
          inbound_paxos_.push_back(std::move(decoded.front()));
          decoded.pop_front();
        }
      }
    }
  }

  for (const auto& [_, fd] : outbound_peer_fd_by_id) {
    ::close(fd);
  }
  for (const auto& [fd, _] : inbound_peer_buffers) {
    ::close(fd);
  }
}

