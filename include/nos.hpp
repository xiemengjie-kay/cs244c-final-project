#pragma once

#include <coroutine>
#include <cstdint>
#include <deque>
#include <functional>
#include <optional>
#include <set>
#include <unordered_map>
#include <utility>

#include "runtime.hpp"

namespace lp {

template <typename T>
class Mailbox {
 public:
  explicit Mailbox(Runtime& runtime) : runtime_(runtime) {}

  void push(T item) {
    queue_.push_back(std::move(item));
    if (!waiters_.empty()) {
      auto waiter = waiters_.front();
      waiters_.pop_front();
      runtime_.schedule(waiter);
    }
  }

  class ReceiveAwaitable {
   public:
    explicit ReceiveAwaitable(Mailbox& mailbox) : mailbox_(mailbox) {}

    bool await_ready() const noexcept { return !mailbox_.queue_.empty(); }

    void await_suspend(std::coroutine_handle<> handle) { mailbox_.waiters_.push_back(handle); }

    T await_resume() {
      T out = std::move(mailbox_.queue_.front());
      mailbox_.queue_.pop_front();
      return out;
    }

   private:
    Mailbox& mailbox_;
  };

  ReceiveAwaitable receive() { return ReceiveAwaitable(*this); }
  bool empty() const { return queue_.empty(); }

 private:
  Runtime& runtime_;
  std::deque<T> queue_;
  std::deque<std::coroutine_handle<>> waiters_;
};

template <typename Message>
class SimulatedNetwork {
 public:
  using DelayFn = std::function<std::uint64_t(const Message&)>;

  explicit SimulatedNetwork(Runtime& runtime)
      : runtime_(runtime), delay_fn_([](const Message&) { return 1; }) {}

  struct Endpoint {
    int node_id;
    Mailbox<Message>* inbox;
    bool alive{true};
  };

  void register_endpoint(int node_id, Mailbox<Message>* inbox) {
    endpoints_[node_id] = Endpoint{.node_id = node_id, .inbox = inbox, .alive = true};
  }

  void set_delay_fn(DelayFn fn) { delay_fn_ = std::move(fn); }

  void crash(int node_id) {
    auto it = endpoints_.find(node_id);
    if (it != endpoints_.end()) {
      it->second.alive = false;
    }
  }

  void restore(int node_id) {
    auto it = endpoints_.find(node_id);
    if (it != endpoints_.end()) {
      it->second.alive = true;
    }
  }

  bool alive(int node_id) const {
    auto it = endpoints_.find(node_id);
    return it != endpoints_.end() && it->second.alive;
  }

  void partition(int a, int b) {
    auto key = ordered_pair(a, b);
    blocked_.insert(key);
  }

  void heal(int a, int b) { blocked_.erase(ordered_pair(a, b)); }

  void heal_all() { blocked_.clear(); }

  void send(Message msg) {
    const int src = msg.src;
    const int dst = msg.dst;

    auto src_it = endpoints_.find(src);
    auto dst_it = endpoints_.find(dst);
    if (src_it == endpoints_.end() || dst_it == endpoints_.end()) {
      return;
    }

    if (!src_it->second.alive || !dst_it->second.alive) {
      return;
    }

    if (blocked_.contains(ordered_pair(src, dst))) {
      return;
    }

    (void)delay_fn_;
    dst_it->second.inbox->push(std::move(msg));
  }

 private:
  static std::pair<int, int> ordered_pair(int a, int b) {
    if (a <= b) {
      return {a, b};
    }
    return {b, a};
  }

  Runtime& runtime_;
  DelayFn delay_fn_;
  std::unordered_map<int, Endpoint> endpoints_;
  std::set<std::pair<int, int>> blocked_;
};

}  // namespace lp
