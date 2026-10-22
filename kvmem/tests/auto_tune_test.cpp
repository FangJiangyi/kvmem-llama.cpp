#include "kvmem/auto_tune.hpp"
#include <iostream>
#include <stdexcept>

using namespace kvmem;

void check(bool condition, const char * message) {
    if (!condition) throw std::runtime_error(message);
}
template<class F> void rejects(F f) {
    bool rejected = false;
    try { f(); } catch (const std::invalid_argument &) { rejected = true; }
    check(rejected, "infeasible/invalid request was accepted");
}

int main() {
    try {
        AutoTuneRequest r;
        r.context = 2048;
        r.preferred_budget = 1024;
        r.preferred_chunk = 256;
        r.device_reserve_bytes = 100;
        r.host_reserve_bytes = 200;
        r.host_available_bytes = 1000000;
        r.device_free_bytes = {1000000, 1000000};
        const auto price = [&](const AutoTuneCandidate & c) {
            const uint64_t window = (c.budget + c.reserve) / 64 * r.lanes;
            // Independent stepped workspace cost and asymmetric devices.
            return AutoTuneCost{{1000 + window * 100 + c.chunk * 2,
                                 2000 + window * 180 + (c.chunk + 127) / 128 * 300},
                                1000, 128};
        };
        auto p = auto_tune(r, price);
        check(p.budget == 1024 && p.chunk == 256 && p.reserve == 256, "ample resource plan");
        r.budget = 512; r.reserve = 128; r.chunk = 64; r.host_bytes = 30000;
        p = auto_tune(r, price);
        check(p.budget == 512 && p.reserve == 128 && p.chunk == 64 && p.host_bytes == 30000,
              "explicit settings changed");
        r.device_free_bytes[1] = 2100;
        rejects([&] { auto_tune(r, price); });
        r.device_free_bytes[1] = 1000000;
        r.host_bytes = 127;
        rejects([&] { auto_tune(r, price); });
        r.host_bytes.reset(); r.budget.reset(); r.reserve.reset(); r.chunk.reset();

        // Exhaustive feasible-set oracle: compare the selected plan with every
        // page-aligned B and chunk, over both device bottlenecks, RAM and concurrency.
        for (uint32_t lanes : {1U, 2U, 8U}) {
            r.lanes = lanes;
            for (uint64_t free0 : {1000ULL, 4500ULL, 10000ULL, 50000ULL}) {
                for (uint64_t free1 : {2100ULL, 6000ULL, 15000ULL, 100000ULL}) {
                    for (uint64_t ram : {1000ULL, 4000ULL, 10000ULL, 50000ULL}) {
                        r.device_free_bytes = {free0, free1}; r.host_available_bytes = ram;
                        std::optional<AutoTuneCandidate> expected;
                        for (uint32_t c : {256U, 128U, 64U}) {
                            for (uint32_t b = 128; b <= 1024; b += 64) {
                                if (b + c < 256) continue;
                                const auto cost = price({b, c, c});
                                if (cost.device_bytes[0] + (cost.device_bytes[0] + 9) / 10 + 100 > free0 ||
                                    cost.device_bytes[1] + (cost.device_bytes[1] + 9) / 10 + 100 > free1) continue;
                                const auto spare = ram > 1200 ? ram - 1200 : 0;
                                const auto h = (spare / 2) / 128 * 128;
                                if (h < uint64_t(r.context) / 64 * lanes * 128) continue;
                                if (!expected || b > expected->budget || (b == expected->budget && c > expected->chunk))
                                    expected = AutoTuneCandidate{b, c, c};
                            }
                        }
                        if (!expected) rejects([&] { auto_tune(r, price); });
                        else {
                            p = auto_tune(r, price);
                            check(p.budget == expected->budget && p.chunk == expected->chunk,
                                  "search disagrees with exhaustive feasible-set oracle");
                        }
                    }
                }
            }
        }
        r.lanes = 1; r.context = 256; r.device_free_bytes = {1000000, 1000000};
        r.host_available_bytes = 1000000;
        p = auto_tune(r, price);
        check(p.budget + p.reserve == 256 && p.budget >= 128, "small context minimum");
        r.context = 255;
        rejects([&] { auto_tune(r, price); });
        r.context = 2048; r.chunk = 65;
        rejects([&] { auto_tune(r, price); });
        r.chunk = 256; r.reserve = 128;
        rejects([&] { auto_tune(r, price); });
        r.chunk.reset(); r.reserve.reset(); r.minimum_budget = 1536;
        p = auto_tune(r, price);
        check(p.budget >= 1536, "sink/recent minimum was ignored");
        r.minimum_budget = 1920;
        p = auto_tune(r, price);
        check(p.budget == 1920 && p.chunk == 128,
              "preferred reserve incorrectly ruled out a feasible pinned-history window");
        AutoTuneRequest fixed_chunk;
        fixed_chunk.context = 2048;
        fixed_chunk.chunk = 128;
        fixed_chunk.device_reserve_bytes = 0;
        fixed_chunk.host_reserve_bytes = 0;
        fixed_chunk.device_free_bytes = {1000000, 1000000};
        fixed_chunk.host_available_bytes = 1000000;
        p = auto_tune(fixed_chunk, price);
        check(p.budget == 1920 && p.reserve == 128,
              "fixed small chunk left the automatic reserve's larger soft budget in place");
        AutoTuneRequest conservative;
        conservative.context = 65536;
        conservative.device_free_bytes = {2ULL << 30};
        conservative.host_available_bytes = 8ULL << 30;
        const auto steady_cost = [](const AutoTuneCandidate &) {
            return AutoTuneCost{{64ULL << 20}, 256ULL << 20, 8192};
        };
        p = auto_tune(conservative, steady_cost);
        check(p.budget == 32768 && p.chunk == 512 && p.reserve == 512,
              "default plan sacrificed history capacity or used an oversized automatic chunk");
        check(p.host_bytes == ((8ULL << 30) - (2ULL << 30) - (256ULL << 20)) / 2,
              "automatic Host budget did not leave half the RAM after overhead and reserve");
        conservative.budget = 128; conservative.reserve = 128; conservative.chunk = 128;
        const uint64_t required = (1024ULL << 20) + (64ULL << 20) + ((64ULL << 20) + 9) / 10;
        conservative.device_free_bytes = {required - 1};
        rejects([&] { auto_tune(conservative, steady_cost); });
        conservative.device_free_bytes = {required};
        p = auto_tune(conservative, steady_cost);
        check(p.budget == 128, "growth-margin boundary rejected a feasible explicit plan");
        conservative.device_free_bytes = {2ULL << 30};
        conservative.host_available_bytes = (2ULL << 30) + (256ULL << 20) + (8ULL << 20);
        rejects([&] { auto_tune(conservative, steady_cost); });
        conservative.host_bytes = 4ULL << 20;
        p = auto_tune(conservative, steady_cost);
        check(p.host_bytes == (4ULL << 20), "explicit smaller history bound was changed");
        r.maximum_device_tokens = 128;
        rejects([&] { auto_tune(r, price); });
        r.minimum_budget = 128; r.maximum_device_tokens = UINT32_MAX;
        r.device_reserve_bytes = UINT64_MAX;
        rejects([&] { auto_tune(r, price); });
        bool overflow = false;
        try { tune_mul(UINT64_MAX, 2); } catch (const std::overflow_error &) { overflow = true; }
        check(overflow, "memory multiplication wrapped");
        std::cout << "auto_tune_test: resource oracle and boundary checks passed\n";
        return 0;
    } catch (const std::exception & e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
