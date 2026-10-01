#include "kvmem-lane-pool.h"
#include <atomic>
#include <cassert>
#include <future>
#include <iostream>

int main() {
    using namespace std::chrono_literals;
    kvmem_lane_pool pool(2);
    auto available = [] { return false; };
    auto first = pool.acquire(available);
    auto second = pool.acquire(available);
    assert(first && second && first->index != second->index);

    std::promise<void> queued;
    std::atomic<bool> cancel{false};
    bool announced = false;
    auto waiting = std::async(std::launch::async, [&] {
        return pool.acquire([&] {
            if (!announced) { announced = true; queued.set_value(); }
            return cancel.load();
        });
    });
    queued.get_future().wait();
    assert(waiting.wait_for(100ms) == std::future_status::timeout);
    cancel = true;
    assert(waiting.wait_for(2s) == std::future_status::ready);
    assert(!waiting.get());

    // Two enqueued waiters are admitted in order, with no duplicate lease.
    std::promise<void> entered_a, entered_b;
    auto wait_on = [&](std::promise<void>& entered) {
        return std::async(std::launch::async, [&pool, &entered] {
            bool announced = false;
            return pool.acquire([&] {
                if (!announced) { announced = true; entered.set_value(); }
                return false;
            });
        });
    };
    auto a = wait_on(entered_a);
    entered_a.get_future().wait();
    auto b = wait_on(entered_b);
    entered_b.get_future().wait();
    const auto freed = first->index;
    first.reset();
    assert(a.wait_for(2s) == std::future_status::ready);
    auto third = a.get();
    assert(third->index == freed);
    assert(b.wait_for(100ms) == std::future_status::timeout);
    second.reset();
    assert(b.wait_for(2s) == std::future_status::ready);
    auto fourth = b.get();
    assert(fourth->index != third->index);
    third->release();
    third->release(); // explicit completion plus destruction is idempotent
    auto reused = pool.acquire(available);
    assert(reused->index == freed);
    std::cout << "lane pool: passed\n";
}
