#pragma once

#include "vendor/llama.h"

#include <memory>
#include <string>
#include <vector>

namespace cp_bridge {
constexpr const char *upstream_commit = "6f1e6543704bc5b080c7a98e7b6f707b31f6ed51";

#define CP_LLAMA_SYMBOLS(X) \
    X(llama_model_default_params) X(llama_context_default_params) \
    X(llama_backend_init) X(llama_model_load_from_file) X(llama_model_free) \
    X(llama_init_from_model) X(llama_free) X(llama_model_get_vocab) \
    X(llama_vocab_n_tokens) X(llama_tokenize) X(llama_token_to_piece) \
    X(llama_vocab_is_eog) X(llama_decode) X(llama_synchronize) \
    X(llama_get_logits_ith) X(llama_get_memory) X(llama_memory_seq_rm) \
    X(llama_n_ctx) X(llama_n_ctx_seq) X(llama_log_set) \
    X(ggml_backend_load) X(ggml_backend_reg_count) X(ggml_backend_dev_count) X(ggml_backend_dev_get) \
    X(ggml_backend_dev_type) X(ggml_backend_dev_name) \
    X(ggml_backend_dev_description) X(ggml_backend_dev_memory) X(ggml_log_set)

struct Api {
#define CP_DECLARE(name) decltype(&::name) name = nullptr;
    CP_LLAMA_SYMBOLS(CP_DECLARE)
#undef CP_DECLARE
};

struct Runtime {
    Api api;
    std::string directory;
    std::vector<std::string> warnings;
    // Immutable discovery snapshot: required-size/output calls must not differ
    // just because another process changed the reported free GPU memory.
    std::string device_snapshot;
};

/* A runtime is retained for the owned worker's lifetime. Upstream explicitly
 * warns that backend libraries cannot safely be unloaded while their internal
 * threads may still exist. Contexts/models are freed normally on every close.
 * Never load two runtime versions or directories in the same worker. */
std::shared_ptr<Runtime> load_runtime(const char *directory);
bool valid_utf8(const uint8_t *bytes, size_t size);
std::string json_quote(const std::string &value);
std::string devices_json(const Runtime &runtime);
} // namespace cp_bridge
