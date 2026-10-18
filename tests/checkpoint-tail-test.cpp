#include "llama.h"
#include "llama-kvmem-hooks.h"
#include "ggml-backend.h"

#include <cstdio>
#include <memory>
#include <stdexcept>
#include <vector>

static void require(bool ok, const char * message) {
    if (!ok) throw std::runtime_error(message);
}

static std::vector<uint8_t> state(llama_context * ctx, llama_state_seq_flags flags = 0) {
    std::vector<uint8_t> bytes(llama_state_seq_get_size_ext(ctx, 0, flags));
    require(llama_state_seq_get_data_ext(ctx, bytes.data(), bytes.size(), 0, flags) == bytes.size(),
            "cannot read native KV/recurrent state");
    return bytes;
}

static void check(llama_model * model, int rows) {
    llama_kvmem_params memory{};
    memory.enabled = true;
    memory.method = 1;
    memory.block_tokens = 32;
    memory.budget = 256;
    memory.gen_reserve = 128;
    memory.query_begin = memory.query_end = memory.force_pos = -1;
    llama_kvmem_set_params(&memory);
    auto params = llama_context_default_params();
    params.n_ctx = 1024;
    params.n_batch = params.n_ubatch = 128;
    params.n_seq_max = 1;
    params.type_k = params.type_v = GGML_TYPE_Q8_0;
    params.flash_attn_type = LLAMA_FLASH_ATTN_TYPE_ENABLED;
    std::unique_ptr<llama_context, decltype(&llama_free)> ctx(llama_init_from_model(model, params), llama_free);
    require(bool(ctx), "context initialization failed");
    std::vector<llama_token> tokens(rows + 32);
    for (int i = 0; i < (int) tokens.size(); ++i) tokens[i] = 100 + i;
    require(llama_decode(ctx.get(), llama_batch_get_one(tokens.data(), rows)) == 0, "prefill failed");
    llama_synchronize(ctx.get());
    const auto before = state(ctx.get());
    const auto checkpoint = state(ctx.get(), LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY);
    std::vector<llama_pos> positions(32);
    for (int i = 0; i < 32; ++i) positions[i] = rows + i;
    auto extra = llama_batch_get_one(tokens.data() + rows, 32);
    extra.pos = extra.logical_pos = positions.data();
    require(llama_decode(ctx.get(), extra) == 0, "extra suffix prefill failed");
    llama_synchronize(ctx.get());
    auto selection = llama_kvmem_preview_retrieval();
    require(selection.blocks.size() == 4, "fixture must have four blocks before rewind");
    selection.blocks = {0};
    llama_kvmem_apply_selection(selection);
    require(llama_kvmem_get_attention_view().blocks.size() == 1, "partial tail was not evicted");
    require(llama_state_seq_set_data_ext(ctx.get(), checkpoint.data(), checkpoint.size(), 0,
            LLAMA_STATE_SEQ_FLAGS_PARTIAL_ONLY) == checkpoint.size(), "recurrent checkpoint restore failed");
    require(llama_kvmem_remove_logical(ctx.get(), rows, -1), "suffix removal failed");
    llama_kvmem_truncate_cached(rows);
    require(llama_kvmem_restore_checkpoint_tail(), "cannot restore cold partial tail");
    // Normalize physical slot order before comparing the serialized bytes.
    llama_kvmem_apply_selection(llama_kvmem_preview_retrieval());
    require(llama_kvmem_get_attention_view().valid, "restored cells are incomplete");
    require(state(ctx.get()) == before, "restored native KV/recurrent bytes differ");

    const int gap = rows == 65 ? 0 : rows - 1;
    require(llama_kvmem_remove_logical(ctx.get(), gap, gap + 1), "cannot inject coverage gap");
    require(!llama_kvmem_restore_checkpoint_tail(), "resident coverage gap was hidden");
    require(!llama_kvmem_get_attention_view().valid, "broken view was published as reusable");
    llama_memory_clear(llama_get_memory(ctx.get()), true);
    require(llama_decode(ctx.get(), llama_batch_get_one(tokens.data(), rows)) == 0, "cold recovery failed");
    llama_synchronize(ctx.get());
    require(state(ctx.get()) == before, "cold recovery retained invalid state");
    std::printf("PASS checkpoint tail %d/32: exact native bytes, coverage rejection, cold recovery\n", rows % 32);
}

int main(int argc, char ** argv) {
    if (argc != 2) return argc == 1 ? 77 : 1;
    try {
        llama_backend_init();
        ggml_backend_load_all();
        auto params = llama_model_default_params();
        params.n_gpu_layers = 99;
        std::unique_ptr<llama_model, decltype(&llama_model_free)> model(llama_model_load_from_file(argv[1], params), llama_model_free);
        require(bool(model), "model loading failed");
        check(model.get(), 65);
        check(model.get(), 95);
        llama_kvmem_set_params(nullptr);
    } catch (const std::exception & error) {
        std::fprintf(stderr, "%s\n", error.what());
        return 1;
    }
    return 0;
}
