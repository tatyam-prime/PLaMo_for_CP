// An in-memory test double, not a model/runtime shim used in production.
#include "fake_runtime.h"
#include <algorithm>
#include <cassert>
#include <cstring>

struct llama_vocab {};
struct llama_model { bool gpu; };
struct llama_context { uint32_t size; bool pending = false; };
struct ggml_backend_device { enum ggml_backend_dev_type type; const char *name; };

namespace {
llama_vocab vocabulary;
ggml_backend_device cpu{GGML_BACKEND_DEVICE_TYPE_CPU, "Test CPU"};
ggml_backend_device gpu{GGML_BACKEND_DEVICE_TYPE_GPU, "Test GPU 日本語"};
float logits[] = {0.0f, 1.0f, 9.0f, 9.0f, -1.0f, 2.0f, 3.0f, 0.0f};
}

namespace fake { State &state() { static State result; return result; } }

namespace cp_bridge {
std::shared_ptr<Runtime> load_runtime(const char *directory) {
    assert(directory && std::strcmp(directory, "test-runtime") == 0);
    static auto runtime = [] {
        auto result = std::make_shared<Runtime>(); result->directory = "test-runtime";
        auto &api = result->api;
        api.llama_model_default_params = +[]() { return llama_model_params{}; };
        api.llama_context_default_params = +[]() { return llama_context_params{}; };
        api.llama_model_load_from_file = +[](const char *, llama_model_params parameters) -> llama_model * {
            fake::state().models++;
            return new llama_model{parameters.n_gpu_layers != 0 && parameters.devices[0] != nullptr};
        };
        api.llama_model_free = +[](llama_model *model) { fake::state().models--; delete model; };
        api.llama_init_from_model = +[](llama_model *model, llama_context_params parameters) -> llama_context * {
            fake::state().parameters = parameters;
            if (model->gpu ? fake::state().fail_gpu_context : fake::state().fail_cpu_context) return nullptr;
            fake::state().contexts++; return new llama_context{parameters.n_ctx};
        };
        api.llama_free = +[](llama_context *context) { fake::state().contexts--; delete context; };
        api.llama_model_get_vocab = +[](const llama_model *) -> const llama_vocab * { return &vocabulary; };
        api.llama_vocab_n_tokens = +[](const llama_vocab *) -> int32_t { return 8; };
        api.llama_tokenize = +[](const llama_vocab *, const char *, int32_t length, llama_token *tokens, int32_t capacity, bool, bool) -> int32_t {
            if (capacity < length) return -length;
            for (int32_t index = 0; index < length; index++) tokens[index] = index % 8;
            return length;
        };
        api.llama_token_to_piece = +[](const llama_vocab *, llama_token, char *output, int32_t capacity, int32_t, bool) -> int32_t {
            constexpr char piece[] = "日本語";
            constexpr int32_t size = sizeof(piece) - 1;
            if (capacity < size) return -size;
            std::memcpy(output, piece, size); return size;
        };
        api.llama_vocab_is_eog = +[](const llama_vocab *, llama_token token) { return token == 7; };
        api.llama_decode = +[](llama_context *context, llama_batch batch) -> int32_t {
            assert(!context->pending && batch.n_tokens > 0 && batch.n_tokens <= 256);
            for (int32_t index = 0; index < batch.n_tokens; index++) assert(batch.n_seq_id[index] == 1 && *batch.seq_id[index] >= 0);
            context->pending = true; fake::state().decodes++; return 0;
        };
        api.llama_synchronize = +[](llama_context *context) { context->pending = false; fake::state().synchronizes++; };
        api.llama_get_logits_ith = +[](llama_context *context, int32_t) -> float * { assert(!context->pending); return logits; };
        api.llama_get_memory = +[](const llama_context *context) -> llama_memory_t { return reinterpret_cast<llama_memory_t>(const_cast<llama_context *>(context)); };
        api.llama_memory_seq_rm = +[](llama_memory_t memory, llama_seq_id, llama_pos, llama_pos) {
            assert(!reinterpret_cast<llama_context *>(memory)->pending); fake::state().removals++; return true;
        };
        api.llama_n_ctx = +[](const llama_context *context) -> uint32_t { return context->size; };
        api.llama_n_ctx_seq = api.llama_n_ctx;
        api.ggml_backend_dev_count = +[]() -> size_t { return 2; };
        api.ggml_backend_dev_get = +[](size_t index) -> ggml_backend_dev_t { return index == 0 ? &cpu : &gpu; };
        api.ggml_backend_dev_type = +[](ggml_backend_dev_t device) { return device->type; };
        api.ggml_backend_dev_name = +[](ggml_backend_dev_t device) { return device->name; };
        api.ggml_backend_dev_description = api.ggml_backend_dev_name;
        api.ggml_backend_dev_memory = +[](ggml_backend_dev_t, size_t *free, size_t *total) { *free = fake::state().free_memory; *total = 2048; };
        result->device_snapshot = devices_json(*result);
        return result;
    }();
    return runtime;
}
}
