#include "kvmem-server-metrics.h"
#include <cmath>
#include <cstdlib>
#include <iostream>

static void check(bool value) { if (!value) std::abort(); }
static double sample(const std::string & text, const std::string & name) {
    const auto pos = text.find("\n" + name + " ");
    check(pos != std::string::npos);
    return std::stod(text.substr(pos + name.size() + 2));
}
int main() {
    kvmem_request_metrics r;
    r.start(1000);
    r.prefilled(100, 20, 11000);
    r.token(1, 12000);
    auto one = r.timings();
    check(one["predicted_n"] == 1 && one["predicted_per_second"] == 0);
    r.token(5, 52000);
    auto timing = r.timings();
    check(timing["cache_n"] == 20 && timing["prompt_n"] == 80);
    check(timing["prompt_ms"] == 11 && timing["predicted_ms"] == 40);
    check(timing["predicted_per_second"] == 100 && timing["predicted_per_token_ms"] == 10);
    check(!timing.contains("draft_n"));
    r.native.n_draft_tokens = 6;
    r.native.n_draft_accepted = 3;
    r.native.n_draft_verif_steps = 2;
    r.accepted_per_position = {2, 1};
    r.decode_calls = 1;
    check(r.timings()["draft_n_accepted"] == 3);
    kvmem_server_metrics aggregate;
    aggregate.queued(); aggregate.queued(); aggregate.admitted();
    auto active = aggregate.render();
    check(sample(active, "llamacpp:requests_processing") == 1);
    check(sample(active, "llamacpp:requests_deferred") == 1);
    aggregate.finished(r, true);
    aggregate.finished(r, true); // streaming release must not double count
    auto text = aggregate.render(true);
    check(sample(text, "llamacpp:tokens_predicted_total") == 5);
    check(sample(text, "llamacpp:predicted_tokens_seconds") == 100);
    check(sample(text, "llamacpp:spec_decode_num_drafts_total") == 2);
    check(text.find("llamacpp:spec_decode_num_accepted_tokens_per_pos_total{position=\"1\"} 1") != std::string::npos);
    auto reset = aggregate.render();
    check(sample(reset, "llamacpp:tokens_predicted_total") == 5);
    check(sample(reset, "llamacpp:predicted_tokens_seconds") == 0);
    // A cancelled waiter cannot leak a deferred request or fabricate tokens.
    kvmem_request_metrics cancelled;
    aggregate.finished(cancelled, false);
    check(sample(aggregate.render(), "llamacpp:requests_deferred") == 0);
    kvmem_request_metrics partial;
    partial.start(1000); partial.prefilled(32, 0, 2000);
    aggregate.queued(); aggregate.admitted(); aggregate.finished(partial, true);
    check(sample(aggregate.render(), "llamacpp:prompt_tokens_total") == 112);
    for (int i = 0; i < 150; ++i) {
        kvmem_request_metrics cached;
        cached.start(1); cached.prefilled(10, 10, 2);
        check(cached.timings()["prompt_per_second"] == 0);
        aggregate.queued(); aggregate.admitted(); aggregate.finished(cached, true);
    }
    check(sample(aggregate.render(), "llamacpp:prompt_tokens_cached_total") == 1520);
    check(sample(aggregate.render(), "llamacpp:requests_processing") == 0);
    std::cout << "native timing, counters, reset, cancellation and lifetime checks passed\n";
}
