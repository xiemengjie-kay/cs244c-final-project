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


}  // namespace lp
