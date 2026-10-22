#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <limits>
#include <optional>
#include <stdexcept>
#include <utility>
#include <vector>

namespace kvmem {

// Resource selection, not a throughput prediction. The adapter prices each candidate
// with its real model planner, including native KV scales, state, graphs and workspace.
struct AutoTuneRequest {
    uint32_t context = 8192, lanes = 1, page = 64;
    uint32_t minimum_budget = 128, minimum_reserve = 64;
    uint32_t minimum_chunk = 64, minimum_window = 256;
    uint32_t preferred_budget = 32768, preferred_chunk = 512;
    uint32_t maximum_device_tokens = UINT32_MAX;
    std::optional<uint32_t> budget, reserve, chunk;
    std::optional<uint64_t> host_bytes;
    std::vector<uint64_t> device_free_bytes;
    uint64_t host_available_bytes = 0;
    uint64_t device_reserve_bytes = uint64_t(1024) << 20;
    uint64_t host_reserve_bytes = uint64_t(2048) << 20;
};

struct AutoTuneCandidate {
    uint32_t budget = 0, reserve = 0, chunk = 0;
};
struct AutoTuneCost {
    std::vector<uint64_t> device_bytes;
    uint64_t host_overhead_bytes = 0; // checkpoint/state/statistics/staging, outside H
    uint64_t host_page_bytes = 0;     // one all-layer native archive page, including MTP
};
struct AutoTunePlan : AutoTuneCandidate {
    uint64_t host_bytes = 0;
    AutoTuneCost cost;
    uint32_t candidates = 0;
};

inline uint64_t tune_add(uint64_t a, uint64_t b) {
    if (b > UINT64_MAX - a) throw std::overflow_error("KVMem auto memory sum overflow");
    return a + b;
}
inline uint64_t tune_mul(uint64_t a, uint64_t b) {
    if (b && a > UINT64_MAX / b) throw std::overflow_error("KVMem auto memory product overflow");
    return a * b;
}

// For each fixed R/chunk the adapter's reservations must not decrease with B.
// This lets page-space binary search find the largest feasible history budget.
inline AutoTunePlan auto_tune(const AutoTuneRequest & r,
        const std::function<AutoTuneCost(const AutoTuneCandidate &)> & price) {
    const auto aligned = [&](uint32_t n) { return n && n % r.page == 0; };
    if (!r.page || !r.lanes || r.device_free_bytes.empty() ||
        !aligned(r.minimum_budget) || !aligned(r.minimum_reserve) || !aligned(r.minimum_chunk) ||
        !aligned(r.preferred_chunk) || !aligned(r.preferred_budget) ||
        (r.budget && (!aligned(*r.budget) || *r.budget < r.minimum_budget)) ||
        (r.reserve && (!aligned(*r.reserve) || *r.reserve < r.minimum_reserve)) ||
        (r.chunk && (!aligned(*r.chunk) || *r.chunk < r.minimum_chunk)) || (r.host_bytes && !*r.host_bytes)) {
        throw std::invalid_argument("KVMem auto requires positive page-aligned B/R/chunk and resources");
    }
    std::vector<uint32_t> chunks;
    if (r.chunk) chunks.push_back(*r.chunk);
    else {
        for (uint32_t c = r.preferred_chunk; c >= r.minimum_chunk; c = (c / 2 / r.page) * r.page) {
            if (!r.reserve || c <= *r.reserve) chunks.push_back(c);
            if (c == r.page) break;
        }
    }
    // Keep a stable quality target while testing smaller workspaces. Reclaiming
    // every unused reserve token would always prefer a tiny chunk on short contexts.
    const uint32_t preferred_r = r.reserve.value_or(std::max(r.minimum_reserve,
        std::min(r.chunk.value_or(r.preferred_chunk), (r.context / 4 / r.page) * r.page)));
    const uint32_t target = r.budget.value_or(std::max(r.minimum_budget,
        std::min(r.preferred_budget,
            r.context > preferred_r ? (r.context - preferred_r) / r.page * r.page : 0U)));
    std::optional<AutoTunePlan> best;
    uint32_t evaluated = 0;
    for (uint32_t chunk : chunks) {
        const uint32_t reserve = r.reserve.value_or(std::max(r.minimum_reserve, chunk));
        if (chunk > reserve || reserve >= r.context) continue;
        const uint32_t capacity = std::min(r.context, r.maximum_device_tokens / r.lanes);
        if (reserve >= capacity) continue;
        const uint32_t upper = std::min(target, (capacity - reserve) / r.page * r.page);
        if (r.budget && upper != *r.budget) continue;
        const uint64_t append_floor = reserve < r.minimum_window ? r.minimum_window - reserve : 0U;
        const uint64_t minimum64 = std::max(uint64_t(r.minimum_budget),
            (append_floor + r.page - 1) / r.page * r.page);
        if (minimum64 > upper) continue;
        const uint32_t minimum = static_cast<uint32_t>(minimum64);
        if (r.budget && *r.budget < minimum) continue;
        uint32_t lo = (r.budget ? *r.budget : minimum) / r.page;
        uint32_t hi = upper / r.page;
        while (lo <= hi) {
            const uint32_t mid = lo + (hi - lo) / 2;
            AutoTuneCandidate candidate{mid * r.page, reserve, chunk};
            ++evaluated;
            AutoTuneCost cost = price(candidate);
            if (!cost.host_page_bytes || cost.device_bytes.size() != r.device_free_bytes.size())
                throw std::logic_error("KVMem auto adapter returned an invalid memory price");
            bool fits = true;
            for (size_t i = 0; i < cost.device_bytes.size(); ++i) {
                const auto free = r.device_free_bytes[i];
                const auto bytes = cost.device_bytes[i];
                // Keep room for shape-dependent graphs and allocator growth in
                // addition to the fixed spare VRAM. Subtractions avoid overflow.
                const auto growth = bytes / 10 + (bytes % 10 != 0);
                if (free < r.device_reserve_bytes || bytes > free - r.device_reserve_bytes ||
                    growth > free - r.device_reserve_bytes - bytes)
                    fits = false;
            }
            const uint64_t overhead = tune_add(r.host_reserve_bytes, cost.host_overhead_bytes);
            const uint64_t spare = r.host_available_bytes > overhead ? r.host_available_bytes - overhead : 0;
            // Leave half the unreserved RAM for long-request growth and the host.
            uint64_t host = r.host_bytes.value_or(std::min(uint64_t(16) << 30, spare / 2));
            host = r.host_bytes ? host : host / cost.host_page_bytes * cost.host_page_bytes;
            // Automatic H must support the declared context for every active
            // lane, not just one sparse transfer window. Explicit H remains an
            // intentional history bound, checked again on request admission.
            const uint64_t history_tokens = r.host_bytes ? uint64_t(candidate.budget) + reserve : r.context;
            const uint64_t window = tune_mul((history_tokens + r.page - 1) / r.page, r.lanes);
            const uint64_t minimum_host = tune_mul(window, cost.host_page_bytes);
            if (host < minimum_host || host > spare) fits = false;
            if (fits) {
                AutoTunePlan plan;
                static_cast<AutoTuneCandidate &>(plan) = candidate;
                plan.host_bytes = host;
                plan.cost = std::move(cost);
                if (!best || plan.budget > best->budget ||
                    (plan.budget == best->budget && plan.chunk > best->chunk)) best = std::move(plan);
                if (mid == hi) break;
                lo = mid + 1;
            } else {
                if (!mid) break;
                hi = mid - 1;
            }
        }
    }
    if (!best) throw std::invalid_argument(
        "KVMem auto: no B/R/prefill plan fits available VRAM/RAM and explicit settings; "
        "free resources, reduce concurrency or adjust explicit budgets/reserves");
    best->candidates = evaluated;
    return *best;
}

} // namespace kvmem
