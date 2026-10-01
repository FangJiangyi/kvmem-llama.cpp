#pragma once
#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <vector>

// Slot admission only; inference never runs under this mutex. Queued requests
// retain FIFO order, and disconnected waiters do not consume a lane.
class kvmem_lane_pool {
public:
    class lease {
    public:
        const size_t index;
        ~lease() { release(); }
        lease(const lease &) = delete;
        lease & operator=(const lease &) = delete;
        void release() {
            if (!pool_) return;
            auto * pool = pool_;
            pool_ = nullptr;
            { std::lock_guard<std::mutex> lock(pool->mu_); pool->busy_[index] = false; }
            pool->changed_.notify_all();
        }
    private:
        friend class kvmem_lane_pool;
        lease(kvmem_lane_pool & pool, size_t lane) : index(lane), pool_(&pool) {}
        kvmem_lane_pool * pool_;
    };
    explicit kvmem_lane_pool(size_t count) : busy_(count, false) {
        if (count == 0) throw std::invalid_argument("lane pool cannot be empty");
    }

    template<class Cancelled>
    std::shared_ptr<lease> acquire(Cancelled cancelled) {
        std::unique_lock<std::mutex> lock(mu_);
        int waiter;
        waiting_.push_back(&waiter);
        for (;;) {
            if (cancelled()) {
                waiting_.erase(std::find(waiting_.begin(), waiting_.end(), &waiter));
                changed_.notify_all();
                return {};
            }
            if (waiting_.front() == &waiter) {
                for (size_t n = 0; n < busy_.size(); ++n) {
                    const size_t lane = (next_ + n) % busy_.size();
                    if (busy_[lane]) continue;
                    auto result = std::shared_ptr<lease>(new lease(*this, lane));
                    busy_[lane] = true;
                    next_ = (lane + 1) % busy_.size();
                    waiting_.pop_front();
                    changed_.notify_all();
                    return result;
                }
            }
            changed_.wait_for(lock, std::chrono::milliseconds(50));
        }
    }
private:
    std::mutex mu_;
    std::condition_variable changed_;
    std::vector<bool> busy_;
    std::deque<const int *> waiting_;
    size_t next_ = 0;
};
