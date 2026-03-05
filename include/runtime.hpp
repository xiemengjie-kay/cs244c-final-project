#pragma once

#include <coroutine>
#include <cstdint>
#include <deque>
#include <functional>
#include <queue>
#include <stdexcept>
#include <utility>
#include <vector>

namespace lp {

class Runtime {
 public:
  using Handle = std::coroutine_handle<>;

  struct TimedItem {
    std::uint64_t at_tick;
    std::size_t seq;
    Handle handle;

    bool operator<(const TimedItem& other) const {
      if (at_tick != other.at_tick) {
        return at_tick > other.at_tick;
      }
      return seq > other.seq;
    }
  };

  void schedule(Handle handle, std::uint64_t delay = 0) {
    if (!handle || handle.done()) {
      return;
    }
    if (delay == 0) {
      ready_.push_back(handle);
      return;
    }
    timers_.push(TimedItem{.at_tick = tick_ + delay, .seq = seq_++, .handle = handle});
  }

  std::uint64_t tick() const { return tick_; }

  bool run_one() {
    if (ready_.empty()) {
      if (timers_.empty()) {
        return false;
      }
      tick_ = timers_.top().at_tick;
      while (!timers_.empty() && timers_.top().at_tick == tick_) {
        ready_.push_back(timers_.top().handle);
        timers_.pop();
      }
    }

    auto handle = ready_.front();
    ready_.pop_front();
    if (!handle.done()) {
      handle.resume();
    }
    return true;
  }

  void run_until_idle(std::size_t max_steps = 1000000) {
    std::size_t steps = 0;
    while (steps < max_steps && run_one()) {
      ++steps;
    }
    if (steps == max_steps) {
      throw std::runtime_error("Runtime exceeded max steps");
    }
  }

  void run_steps(std::size_t steps) {
    for (std::size_t i = 0; i < steps; ++i) {
      if (!run_one()) {
        return;
      }
    }
  }

 private:
  std::uint64_t tick_{0};
  std::size_t seq_{0};
  std::deque<Handle> ready_;
  std::priority_queue<TimedItem> timers_;
};

class DetachedTask {
 public:
  struct promise_type {
    DetachedTask get_return_object() {
      return DetachedTask(std::coroutine_handle<promise_type>::from_promise(*this));
    }
    std::suspend_always initial_suspend() noexcept { return {}; }
    std::suspend_never final_suspend() noexcept { return {}; }
    void return_void() noexcept {}
    void unhandled_exception() { std::terminate(); }
  };

  using Handle = std::coroutine_handle<promise_type>;

  explicit DetachedTask(Handle handle) : handle_(handle) {}
  DetachedTask(const DetachedTask&) = delete;
  DetachedTask& operator=(const DetachedTask&) = delete;

  DetachedTask(DetachedTask&& other) noexcept : handle_(other.handle_) { other.handle_ = {}; }

  DetachedTask& operator=(DetachedTask&& other) noexcept {
    if (this != &other) {
      if (handle_) {
        handle_.destroy();
      }
      handle_ = other.handle_;
      other.handle_ = {};
    }
    return *this;
  }

  ~DetachedTask() {
    if (handle_) {
      handle_.destroy();
    }
  }

  void start(Runtime& runtime) {
    runtime.schedule(handle_);
    handle_ = {};
  }

 private:
  Handle handle_;
};

struct SleepFor {
  Runtime& runtime;
  std::uint64_t ticks;

  bool await_ready() const noexcept { return ticks == 0; }
  void await_suspend(std::coroutine_handle<> handle) const { runtime.schedule(handle, ticks); }
  void await_resume() const noexcept {}
};

}  // namespace lp
