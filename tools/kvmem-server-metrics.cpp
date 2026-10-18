#include "server-common.h"
#include "kvmem-server-metrics.h"
#include <iomanip>
#include <locale>
#include <sstream>

static server_slot_stats to_native(const kvmem_slot_stats & s) {
    server_slot_stats result;
    result.n_prompt_cached = s.n_prompt_cached;
    result.n_prompt_processed = s.n_prompt_processed;
    result.n_gen = s.n_gen;
    result.n_draft_tokens = s.n_draft_tokens;
    result.n_draft_accepted = s.n_draft_accepted;
    result.n_draft_verif_steps = s.n_draft_verif_steps;
    result.t_start = s.t_start;
    result.t_prompt_last = s.t_prompt_last;
    result.t_gen_last = s.t_gen_last;
    return result;
}

nlohmann::json kvmem_request_metrics::timings() const {
    return nlohmann::json::parse(to_native(native).to_json().dump());
}

struct kvmem_server_metrics::impl {
    std::mutex mutex_;
    server_metrics totals_;
    uint64_t waiting_ = 0, processing_ = 0;
public:
    void queued() { std::lock_guard<std::mutex> lock(mutex_); ++waiting_; }
    void admitted() { std::lock_guard<std::mutex> lock(mutex_); --waiting_; ++processing_; }
    void finished(kvmem_request_metrics & request, bool admitted) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (request.accounted) return;
        request.accounted = true;
        if (admitted) --processing_; else --waiting_;
        const auto s = to_native(request.native);
        if (!s.is_set() || !s.t_prompt_last) return;
        totals_.add_prompt(s.n_prompt_processed,
                (uint64_t) std::max<int64_t>(0, s.t_prompt_last - s.t_start));
        totals_.add_prompt_cached(s.n_prompt_cached);
        totals_.predict.add(s.n_gen, s.n_gen_steps(), (uint64_t) s.t_gen_us());
        totals_.predict_bucket.add(s.n_gen, s.n_gen_steps(), (uint64_t) s.t_gen_us());
        totals_.n_tokens_max = std::max(totals_.n_tokens_max,
                s.n_prompt_processed + s.n_prompt_cached + s.n_gen);
        totals_.n_decode += request.decode_calls;
        totals_.n_busy_slots += request.decode_calls; // one sequence per independent context
        totals_.n_draft_tokens += s.n_draft_tokens;
        totals_.n_draft_accepted += s.n_draft_accepted;
        totals_.n_draft_verif_steps += s.n_draft_verif_steps;
        totals_.n_accepted_per_pos.resize(std::max(totals_.n_accepted_per_pos.size(),
                request.accepted_per_position.size()), 0);
        for (size_t i = 0; i < request.accepted_per_position.size(); ++i)
            totals_.n_accepted_per_pos[i] += request.accepted_per_position[i];
    }
    std::string render(bool reset_bucket = false) {
        std::lock_guard<std::mutex> lock(mutex_);
        std::ostringstream out;
        out.imbue(std::locale::classic());
        out << std::setprecision(17);
        auto emit = [&](const char * name, const char * type, const char * help, double value) {
            out << "# HELP llamacpp:" << name << ' ' << help << '\n'
                << "# TYPE llamacpp:" << name << ' ' << type << '\n'
                << "llamacpp:" << name << ' ' << value << '\n';
        };
        emit("prompt_tokens_total", "counter", "Number of prompt tokens processed, excluding cached tokens", (double) totals_.prompt.count);
        emit("prompt_tokens_cached_total", "counter", "Number of prompt tokens reused from the cache", (double) totals_.n_prompt_cached);
        emit("prompt_seconds_total", "counter", "Total time spent processing prompts", totals_.prompt.time / 1.e6);
        emit("tokens_predicted_total", "counter", "Number of generation tokens processed", (double) totals_.predict.count);
        emit("tokens_predicted_seconds_total", "counter", "Total time spent generating tokens", totals_.predict.time / 1.e6);
        emit("n_decode_total", "counter", "Total number of llama_decode() calls, excluding speculative decoding and multimodal decoding", (double) totals_.n_decode);
        emit("n_tokens_max", "counter", "Largest observed sequence length (prompt + generation)", (double) totals_.n_tokens_max);
        emit("spec_decode_num_draft_tokens_total", "counter", "Speculative: Total draft tokens generated", (double) totals_.n_draft_tokens);
        emit("spec_decode_num_accepted_tokens_total", "counter", "Speculative: Total draft tokens accepted by the target model", (double) totals_.n_draft_accepted);
        emit("spec_decode_num_drafts_total", "counter", "Speculative: Total speculative decoding verification steps", (double) totals_.n_draft_verif_steps);
        emit("prompt_tokens_seconds", "gauge", "Average prompt throughput in tokens/s", totals_.prompt_bucket.n_per_second());
        emit("predicted_tokens_seconds", "gauge", "Average generation throughput in tokens/s", totals_.predict_bucket.n_per_second());
        emit("requests_processing", "gauge", "Number of requests processing", (double) processing_);
        emit("requests_deferred", "gauge", "Number of requests deferred", (double) waiting_);
        emit("n_busy_slots_per_decode", "gauge", "Average number of busy slots per llama_decode() call",
                (double) totals_.n_busy_slots / std::max((double) totals_.n_decode, 1.0));
        if (!totals_.n_accepted_per_pos.empty()) {
            out << "# HELP llamacpp:spec_decode_num_accepted_tokens_per_pos_total Accepted tokens per draft position\n"
                << "# TYPE llamacpp:spec_decode_num_accepted_tokens_per_pos_total counter\n";
            for (size_t i = 0; i < totals_.n_accepted_per_pos.size(); ++i)
                out << "llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position=\"" << i << "\"} "
                    << totals_.n_accepted_per_pos[i] << '\n';
        }
        if (reset_bucket) totals_.reset_bucket();
        return out.str();
    }
};

kvmem_server_metrics::kvmem_server_metrics() : impl_(new impl) {}
kvmem_server_metrics::~kvmem_server_metrics() = default;
void kvmem_server_metrics::queued() { impl_->queued(); }
void kvmem_server_metrics::admitted() { impl_->admitted(); }
void kvmem_server_metrics::finished(kvmem_request_metrics & s, bool admitted) { impl_->finished(s, admitted); }
std::string kvmem_server_metrics::render(bool reset) { return impl_->render(reset); }
