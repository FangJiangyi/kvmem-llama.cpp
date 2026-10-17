#include "kvmem/monitor.hpp"
#include "kvmem/monitor_hardware.hpp"
#include <atomic>
#include <cstdlib>
#include <iostream>
#include <thread>

static void check(bool condition) { if (!condition) std::abort(); }
int main() {
    kvmem::monitor monitor;
    auto a = monitor.begin("/v1/chat/completions");
    auto b = monitor.begin("/v1/responses");
    monitor.update(a, {{"prompt_tokens", 80}, {"cached_tokens", 40}, {"generated_tokens", 2}});
    check(monitor.snapshot()["active"].size() == 2);
    monitor.finish(a, "completed");
    monitor.finish(a, "disconnected"); // failed delivery after generation: one row, corrected outcome
    auto data = monitor.snapshot();
    check(data["recent"].size() == 1 && data["recent"][0]["status"] == "disconnected");
    check(data["active"][0]["generated_tokens"].is_null());
    check(data["recent"][0]["cached_tokens"] == 40);
    monitor.finish(b, "error");
    std::atomic<bool> done{false};
    std::thread reader([&] { while (!done) check(monitor.snapshot()["recent"].size() <= 100); });
    for (int i = 0; i < 250; ++i) {
        const auto id = monitor.begin("test");
        monitor.update(id, {{"generated_tokens", i}});
        monitor.finish(id, "completed");
    }
    done = true; reader.join();
    check(monitor.snapshot()["recent"].size() == 100);
    for (int i = 0; i < 513; ++i) monitor.begin("queued");
    check(monitor.snapshot()["active"].size() == 512);
    check(monitor.snapshot()["dropped_records"] == 1);
    check(monitor.snapshot()["server_instance"] != kvmem::monitor{}.snapshot()["server_instance"]);
    kvmem::monitor_hardware hardware;
    for (int i = 0; i < 50 && hardware.snapshot()["sampled_at_ms"].is_null(); ++i)
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    check(!hardware.snapshot()["sampled_at_ms"].is_null());
    const auto sampled = hardware.snapshot();
    for (const auto & gpu : sampled["gpus"]) {
        check(gpu.contains("uuid") && gpu.contains("pci_bus_id"));
    }
    std::cout << hardware.snapshot().dump() << '\n';
}
