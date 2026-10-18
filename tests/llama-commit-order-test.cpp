#include "llama.h"
#include "llama-kvmem-hooks.h"
#include "ggml-backend.h"
#include <algorithm>
#include <cstdlib>
#include <cstdio>
#include <memory>
#include <stdexcept>
#include <vector>

static void require(bool ok, const char * message) {
    if (!ok) throw std::runtime_error(message);
}

static void synchronous(bool enabled) {
#ifdef _WIN32
    _putenv_s("KVMEM_GDN_FOLD_SYNC", enabled ? "1" : "0");
#else
    setenv("KVMEM_GDN_FOLD_SYNC", enabled ? "1" : "0", 1);
#endif
}

struct Result {
    std::vector<std::vector<uint8_t>> checkpoints;
    std::vector<float> logits;
};

static Result run(llama_model * model, bool sync) {
    synchronous(sync);
    llama_kvmem_params memory{};
    memory.enabled = true;
    memory.budget = memory.gen_reserve = 512;
    memory.block_tokens = 128;
    memory.method = 1;
    memory.mtp_state = 2;
    memory.query_begin = memory.query_end = memory.force_pos = -1;
    llama_kvmem_set_params(&memory);
    auto params = llama_context_default_params();
    params.n_ctx = 1024;
    params.n_batch = params.n_ubatch = 128;
    params.n_seq_max = 1;
    params.n_rs_seq = 3;
    params.n_outputs_max = params.n_outputs_max_per_seq = 4;
    params.type_k = params.type_v = GGML_TYPE_Q8_0;
    params.flash_attn_type = LLAMA_FLASH_ATTN_TYPE_ENABLED;
    std::unique_ptr<llama_context, decltype(&llama_free)> context(llama_init_from_model(model, params), llama_free);
    require(bool(context) && llama_kvmem_gdn_replay_enabled(), "replay context initialization failed");
    std::vector<llama_token> prompt(32);
    for (size_t i = 0; i < prompt.size(); ++i) prompt[i] = 100 + i;
    require(llama_decode(context.get(), llama_batch_get_one(prompt.data(), prompt.size())) == 0, "prefill failed");
    llama_kvmem_end_prefill_capture();
    int past = prompt.size();
    Result result;
    for (int keep : {0, 1, 3, 4, 0, 4}) {
        llama_token ids[] = {200, 201, 202, 203};
        llama_pos positions[] = {past, past + 1, past + 2, past + 3};
        int8_t outputs[] = {1, 1, 1, 1};
        auto batch = llama_batch_get_one(ids, 4);
        batch.pos = batch.logical_pos = positions;
        batch.logits = outputs;
        require(llama_kvmem_gdn_replay_begin(past, 4), "replay begin failed");
        llama_kvmem_begin_speculative_evaluation();
        require(llama_decode(context.get(), batch) == 0, "verification failed");
        require(llama_kvmem_commit_speculative(context.get(), keep), "accepted prefix commit failed");

        // This public read must drain Fold without an explicit caller fence.
        const auto flags = LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY;
        std::vector<uint8_t> state(llama_state_seq_get_size_ext(context.get(), 0, flags));
        require(llama_state_seq_get_data_ext(context.get(), state.data(), state.size(), 0, flags) == state.size(),
                "immediate checkpoint read failed");
        require(llama_state_seq_set_data_ext(context.get(), state.data(), state.size(), 0, flags) == state.size(),
                "checkpoint restore failed");
        result.checkpoints.push_back(std::move(state));
        past += keep;
        require(llama_kvmem_remove_logical(context.get(), past, -1), "rejected tail removal failed");
        llama_kvmem_truncate_cached(past);
        const auto * logits = llama_get_logits_ith(context.get(), 0);
        const int count = llama_vocab_n_tokens(llama_model_get_vocab(model));
        require(logits != nullptr, "logits missing after checkpoint");
        result.logits.insert(result.logits.end(), logits, logits + count);
    }
    return result;
}

int main(int argc, char ** argv) {
    if (argc != 2) return argc == 1 ? 77 : 1;
    try {
        ggml_backend_load_all();
        auto params = llama_model_default_params();
        params.n_gpu_layers = 99;
        std::unique_ptr<llama_model, decltype(&llama_model_free)> model(
                llama_model_load_from_file(argv[1], params), llama_model_free);
        require(bool(model), "model load failed");
        const auto before = run(model.get(), true);
        const auto after = run(model.get(), false);
        require(before.checkpoints == after.checkpoints, "queued Fold changed checkpoint bytes");
        require(before.logits == after.logits, "queued Fold changed logits");
        std::puts("PASS zero/partial/full acceptance, immediate checkpoint/restore, continued verification: sync == queued");
        return 0;
    } catch (const std::exception & error) {
        std::fprintf(stderr, "%s\n", error.what());
        return 1;
    }
}
