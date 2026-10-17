#pragma once

#include "kvmem/retrieval_options.hpp"
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>

namespace kvmem {

// Version 1 of the public KVMem CLI contract. Backend-native flags do not
// belong here. Adapters validate their capabilities after resolving this object.
struct CliOptions {
    static constexpr uint32_t default_budget = 32768;
    static constexpr uint32_t default_reserve = 1024;
    static constexpr uint64_t default_host_bytes = uint64_t(16384) << 20;
    bool enabled = false, lrfu = false;
    int enabled_override = -1;
    uint32_t budget = 0, gen_reserve = 0, sink_tokens = 0, recent_tokens = 64, sessions = 4;
    uint64_t payload_host_bytes = 0, payload_disk_bytes = 0;
    std::string payload_disk_dir;
    double lrfu_lambda = 0.5, lrfu_top_fraction = 0.9;
    int score_agg = 0;
    bool budget_set = false, sessions_set = false, auxiliary_set = false;

    static uint32_t integer(const std::string & name, const char * value, uint32_t minimum) {
        const std::string text(value);
        uint64_t number = 0;
        if (text.empty()) throw std::invalid_argument(name + " requires an integer");
        for (const char c : text) {
            if (c < '0' || c > '9') throw std::invalid_argument(name + " requires an unsigned decimal integer");
            number = number * 10 + uint32_t(c - '0');
            if (number > uint64_t(std::numeric_limits<int>::max()))
                throw std::invalid_argument(name + " exceeds 2147483647");
        }
        if (number < minimum) throw std::invalid_argument(name + " must be positive");
        return uint32_t(number);
    }

    template<class Need> bool parse(const std::string & arg, const Need & need) {
        if (arg == "--kvmem" || arg == "--no-kvmem") {
            enabled_override = arg == "--kvmem" ? 1 : 0;
            return true;
        }
        if (arg == "--kvmem-budget") {
            budget = integer(arg, need(arg.c_str()), 1);
            budget_set = true;
            return true;
        }
        if (arg == "--kvmem-gen-reserve") gen_reserve = integer(arg, need(arg.c_str()), 1);
        else if (arg == "--kvmem-sink-tokens") sink_tokens = integer(arg, need(arg.c_str()), 0);
        else if (arg == "--kvmem-recent-tokens") recent_tokens = integer(arg, need(arg.c_str()), 0);
        else if (arg == "--kvmem-sessions") {
            sessions = integer(arg, need(arg.c_str()), 1);
            sessions_set = true;
        } else if (arg == "--kvmem-host-mib") payload_host_bytes = uint64_t(integer(arg, need(arg.c_str()), 1)) << 20;
        else if (arg == "--kvmem-disk-mib") {
            payload_disk_bytes = uint64_t(integer(arg, need(arg.c_str()), 0)) << 20;
            return true;
        }
        else if (arg == "--kvmem-disk-path") {
            payload_disk_dir = need(arg.c_str());
            if (payload_disk_dir.empty()) throw std::invalid_argument(arg + " must not be empty");
        } else if (arg == "--kvmem-lrfu" || arg == "--no-kvmem-lrfu") {
            lrfu = arg == "--kvmem-lrfu";
            return true;
        }
        else if (arg == "--kvmem-lrfu-lambda") lrfu_lambda = parse_lrfu_real(need(arg.c_str()), true);
        else if (arg == "--kvmem-lrfu-top-frac") lrfu_top_fraction = parse_lrfu_real(need(arg.c_str()), false);
        else if (arg == "--kvmem-score-agg") score_agg = parse_score_aggregation(need(arg.c_str()));
        else return false;
        auxiliary_set = true;
        return true;
    }

    void resolve() {
        // Positive B keeps existing explicit-budget launch commands working.
        // Omission is disabled on both EXEs; --kvmem selects shared defaults.
        enabled = enabled_override < 0 ? budget_set : enabled_override != 0;
        if (!enabled && (budget_set || auxiliary_set || payload_disk_bytes || lrfu))
            throw std::invalid_argument("KVMem settings require --kvmem (or a positive --kvmem-budget); remove --no-kvmem");
        if (payload_disk_dir.empty() != (payload_disk_bytes == 0))
            throw std::invalid_argument("KVMem SSD requires a positive --kvmem-disk-mib and --kvmem-disk-path together; omit the path when disabled");
        if (!enabled) return;
        if (!budget) budget = default_budget;
        if (!gen_reserve) gen_reserve = default_reserve;
        if (!payload_host_bytes) payload_host_bytes = default_host_bytes;
    }
};

} // namespace kvmem
