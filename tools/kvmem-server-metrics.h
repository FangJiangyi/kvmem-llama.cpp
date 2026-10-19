#pragma once
#include "nlohmann/json.hpp"
#include <algorithm>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

// Plain transport state keeps upstream's common_json alias out of the adapter.
// Serialization and derived rates use the pinned server_slot_stats implementation.
struct kvmem_slot_stats {
    uint64_t n_prompt_cached = 0, n_prompt_processed = 0, n_gen = 0;
    uint64_t n_draft_tokens = 0, n_draft_accepted = 0, n_draft_verif_steps = 0;
    int64_t t_start = 0, t_prompt_last = 0, t_gen_last = 0;
};

struct kvmem_request_metrics {
    kvmem_slot_stats native;
    uint64_t decode_calls = 0;
    std::vector<uint64_t> accepted_per_position;
    nlohmann::json speculative = nullptr;
    bool first_token = false, accounted = false;
    void start(int64_t now_us) { native.t_start = now_us; }
    void prefilled(uint64_t prompt, uint64_t cached, int64_t now_us) {
        native.n_prompt_cached = std::min(prompt, cached);
        native.n_prompt_processed = prompt - native.n_prompt_cached;
        native.t_prompt_last = now_us;
    }
    void token(uint64_t count, int64_t sampled_us) {
        if (!first_token) { first_token = true; native.t_prompt_last = sampled_us; }
        native.n_gen = count;
        native.t_gen_last = sampled_us;
    }
    nlohmann::json timings() const;
};

class kvmem_server_metrics {
    struct impl;
    std::unique_ptr<impl> impl_;
public:
    kvmem_server_metrics();
    ~kvmem_server_metrics();
    void queued();
    void admitted();
    void finished(kvmem_request_metrics & request, bool admitted);
    std::string render(bool reset_bucket = false);
};

struct kvmem_metrics_lease {
    kvmem_server_metrics & owner;
    kvmem_request_metrics stats;
    bool registered = false, running = false;
    explicit kvmem_metrics_lease(kvmem_server_metrics & metrics) : owner(metrics) {}
    void queue() { owner.queued(); registered = true; }
    void admit() { owner.admitted(); running = true; }
    void finish() { if (registered) owner.finished(stats, running); }
    ~kvmem_metrics_lease() { finish(); }
};
