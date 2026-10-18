// Manual, fixed-token comparison. Compile the same file against pristine
// upstream (without KVMEM_BENCH_ADAPTER) and against the prepared backend.
// No HTTP, chat template, prompt reuse, context fitting or tool grammar.
#include "common.h"
#include "sampling.h"
#include "speculative.h"
#include "json.h"
#ifdef KVMEM_BENCH_ADAPTER
#include "llama-kvmem-hooks.h"
#endif
#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <stdexcept>

static void require(bool ok, const char * what) {
    if (!ok) throw std::runtime_error(what);
}

int main(int argc, char ** argv) {
    try {
        require(argc == 8, "model tokens.json adapter:0|1 mtp:0|3 output.json repetitions warmups");
        const bool adapter = std::stoi(argv[3]) != 0;
        const int mtp = std::stoi(argv[4]);
        const int repetitions = std::stoi(argv[6]), warmups = std::stoi(argv[7]);
        constexpr int budget = 36864, reserve = 16384, outputs = 256, batch_size = 512;
        std::ifstream fixture(argv[2]);
        const std::string fixture_text((std::istreambuf_iterator<char>(fixture)), {});
        const auto input = common_json::parse(fixture_text);
        const auto prompt = input.get<llama_tokens>();
        require(prompt.size() == budget, "fixture must contain exactly B tokens");
        ggml_backend_load_all();
        common_init();
#ifdef KVMEM_BENCH_ADAPTER
        const bool replay = adapter && std::getenv("KVMEM_BENCH_REPLAY") &&
                            std::string(std::getenv("KVMEM_BENCH_REPLAY")) == "1";
        llama_kvmem_params kp{};
        kp.enabled = adapter;
        kp.block_tokens = replay ? 128 : 32;
        kp.mtp_state = replay ? 2 : 0;
        kp.budget = budget;
        kp.gen_reserve = reserve;
        kp.method = 1;
        kp.query_begin = kp.query_end = kp.force_pos = -1;
        llama_kvmem_set_params(&kp);
#else
        require(!adapter, "pristine build cannot enable KVMem");
#endif
        auto mp = llama_model_default_params();
        mp.n_gpu_layers = 99;
        mp.load_mtp = mtp > 0;
        auto * model = llama_model_load_from_file(argv[1], mp);
        require(model, "model load failed");
        auto cp = llama_context_default_params();
        cp.n_ctx = adapter ? 131072 : budget + reserve;
        cp.n_batch = cp.n_ubatch = batch_size;
        cp.n_seq_max = 1;
        cp.flash_attn_type = LLAMA_FLASH_ATTN_TYPE_ENABLED;
        cp.type_k = cp.type_v = GGML_TYPE_Q8_0;
        cp.no_perf = false;
        if (mtp) {
            cp.n_outputs_max = cp.n_outputs_max_per_seq = mtp + 1;
            cp.n_rs_seq = mtp;
        }
        auto * ctx = llama_init_from_model(model, cp);
        require(ctx, "context initialization failed");
        common_json report = {{"adapter", adapter}, {"mtp", mtp},
            {"budget", budget}, {"reserve", reserve}, {"n_ctx", llama_n_ctx(ctx)},
            {"batch", batch_size}, {"ubatch", batch_size}, {"rows", common_json::array()}};
#ifdef KVMEM_BENCH_ADAPTER
        report["mtp_state"] = replay ? "replay" : "snapshots";
        report["block_tokens"] = kp.block_tokens;
#endif
        for (int run = 0; run < warmups + repetitions; ++run) {
            llama_memory_clear(llama_get_memory(ctx), true);
            llama_perf_context_reset(ctx);
            common_speculative_init_result_ptr draft_init;
            common_speculative * spec = nullptr;
            llama_context * draft_ctx = nullptr;
            if (mtp) {
                common_params p;
                p.n_ctx = cp.n_ctx;
                p.n_batch = p.n_ubatch = batch_size;
                p.n_parallel = 1;
                p.n_gpu_layers = 99;
                p.flash_attn_type = cp.flash_attn_type;
                p.n_outputs_max = p.n_outputs_max_per_seq = mtp + 1;
                p.speculative.types = {COMMON_SPECULATIVE_TYPE_DRAFT_MTP};
                p.speculative.draft.n_max = mtp;
                p.speculative.draft.n_min = 0;
                p.speculative.draft.p_min = 0;
                p.speculative.draft.n_gpu_layers = 99;
                p.speculative.draft.cache_type_k = p.speculative.draft.cache_type_v = GGML_TYPE_F16;
                draft_init = common_speculative_init_from_params(common_base_params_to_speculative(p), model, ctx);
                draft_ctx = draft_init ? draft_init->context() : nullptr;
                require(draft_ctx, "draft initialization failed");
                p.speculative.draft.ctx_tgt = ctx;
                p.speculative.draft.ctx_dft = draft_ctx;
                spec = common_speculative_init(p.speculative, 1);
                bool state_ready = llama_n_rs_seq(ctx) >= (unsigned) mtp;
#ifdef KVMEM_BENCH_ADAPTER
                state_ready = state_ready || (replay && llama_kvmem_gdn_replay_enabled());
#endif
                require(spec && state_ready, "MTP needs rollback planes or replay state");
            }
            common_params_sampling sp;
            sp.temp = 0;
            sp.top_k = 0;
            sp.top_p = 1;
            sp.min_p = 0;
            sp.penalty_repeat = 1;
            sp.seed = 1234;
            sp.samplers = {COMMON_SAMPLER_TYPE_TEMPERATURE};
            // common_init_from_params normally installs this bias. This driver
            // loads the model directly and therefore resolves it explicitly.
            const auto * vocab = llama_model_get_vocab(model);
            for (int id = 0; id < llama_vocab_n_tokens(vocab); ++id) {
                if (llama_vocab_is_eog(vocab, id)) sp.logit_bias.push_back({id, -INFINITY});
            }
            common_sampler_ptr sampler(common_sampler_init(model, sp));
            require(bool(sampler), "sampler initialization failed");
            common_batch batch(ctx);
            auto add = [&](llama_token id, int pos, bool output) {
                const auto idx = batch.add(id, pos, 0, output);
#ifdef KVMEM_BENCH_ADAPTER
                if (adapter) batch.tokens[idx].logical_pos = pos;
#endif
            };
            auto decode = [&] {
                require(llama_process(ctx, LLAMA_PROCESS_TYPE_DECODE, batch.get()) == 0, "decode failed");
                if (spec) require(common_speculative_process(spec, batch), "draft catch-up failed");
            };
            auto remove = [&](llama_context * context, int from) {
#ifdef KVMEM_BENCH_ADAPTER
                if (adapter) {
                    require(llama_kvmem_remove_logical(context, from, -1), "logical tail removal failed");
                    return;
                }
#endif
                require(llama_memory_seq_rm(llama_get_memory(context), 0, from, -1), "native tail removal failed");
            };
#ifdef KVMEM_BENCH_ADAPTER
            if (adapter) llama_kvmem_begin_retrieval_round();
#endif
            const int64_t started = ggml_time_us();
            const int eval_end = budget - (mtp ? 1 : 0);
            for (int first = 0; first < eval_end; first += batch_size) {
                batch.clear();
                const int end = std::min(eval_end, first + batch_size);
                for (int i = first; i < end; ++i) add(prompt[i], i, !mtp && i == eval_end - 1);
                decode();
            }
#ifdef KVMEM_BENCH_ADAPTER
            if (adapter) {
                llama_kvmem_accept_retrieval_round();
                llama_kvmem_end_prefill_capture();
            }
#endif
            llama_tokens generated, history(prompt.begin(), prompt.begin() + eval_end);
            int64_t first_token = 0;
            int drafted = 0, accepted = 0;
            if (!mtp) {
                for (int i = 0; i < outputs; ++i) {
                    const auto id = common_sampler_sample(sampler.get(), ctx, -1);
                    common_sampler_accept(sampler.get(), id, true);
                    generated.push_back(id);
                    if (!first_token) first_token = ggml_time_us();
                    if (i + 1 == outputs) break;
                    batch.clear();
                    add(id, budget + i, true);
                    decode();
                }
            } else {
                common_speculative_begin(spec, 0, history);
                int past = eval_end;
                auto last = prompt.back();
                while (generated.size() < outputs) {
                    llama_tokens draft;
                    auto & dp = common_speculative_get_draft_params(spec, 0);
                    dp = {};
                    dp.drafting = true;
                    dp.n_max = std::min(mtp, outputs - (int) generated.size() - 1);
                    dp.pos0 = past;
                    dp.id_last = last;
                    dp.prompt = &history;
                    dp.result = &draft;
#ifdef KVMEM_BENCH_ADAPTER
                    if (adapter) dp.n_past_logical = past;
#endif
                    common_speculative_draft(spec);
                    remove(draft_ctx, past);
                    batch.clear();
                    add(last, past, true);
                    for (size_t i = 0; i < draft.size(); ++i) add(draft[i], past + 1 + (int) i, true);
#ifdef KVMEM_BENCH_ADAPTER
                    if (replay) require(llama_kvmem_gdn_replay_begin(past, batch.tokens.size()), "replay begin failed");
                    if (adapter) llama_kvmem_begin_speculative_evaluation();
#endif
                    decode();
                    const auto ids = common_sampler_sample_and_accept_n(sampler.get(), ctx, draft);
                    require(!ids.empty() && generated.size() + ids.size() <= outputs, "invalid accepted prefix");
#ifdef KVMEM_BENCH_ADAPTER
                    if (adapter) require(llama_kvmem_commit_speculative(ctx, ids.size()), "prefix commit failed");
#endif
                    common_speculative_accept(spec, 0, ids.size() - 1);
                    past += ids.size();
                    drafted += draft.size();
                    accepted += ids.size() - 1;
                    for (auto id : ids) {
                        history.push_back(last);
                        last = id;
                        generated.push_back(id);
                        if (!first_token) first_token = ggml_time_us();
                    }
                    remove(ctx, past);
                    remove(draft_ctx, past);
#ifdef KVMEM_BENCH_ADAPTER
                    if (adapter) llama_kvmem_truncate_cached(past);
#endif
                }
            }
            llama_synchronize(ctx);
            const int64_t ended = ggml_time_us();
            const double prefill_ms = (first_token - started) / 1000.0;
            const double generation_ms = (ended - first_token) / 1000.0;
            common_json row = {{"run", run}, {"warmup", run < warmups}, {"prompt_n", budget},
                {"predicted_n", generated.size()}, {"prefill_ms", prefill_ms}, {"generation_ms", generation_ms},
                {"prompt_tps", budget * 1000.0 / prefill_ms},
                {"generation_tps", (outputs - 1) * 1000.0 / generation_ms},
                {"drafted", drafted}, {"accepted", accepted}, {"tokens", generated}};
            report["rows"].push_back(row);
            std::ofstream(argv[5]) << report.dump(2);
            std::cout << row.dump() << std::endl;
            common_speculative_free(spec);
        }
        llama_free(ctx);
        llama_model_free(model);
        return 0;
    } catch (const std::exception & e) {
        std::cerr << "FAIL: " << e.what() << std::endl;
        return 1;
    }
}
