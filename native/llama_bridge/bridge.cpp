#include "bridge.h"
#include "runtime.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <unordered_map>
#include <unordered_set>

namespace {
using cp_bridge::Runtime;
thread_local std::array<uint8_t, 64000> last_error{};
thread_local int32_t last_error_size = 0;
constexpr int32_t maximum_text_bytes = 1024 * 1024;

void fail(const std::string &message) { throw std::runtime_error(message); }
template<class Function> int32_t guarded(Function function) noexcept {
    last_error_size = 0;
    const auto remember = [](const char *text) noexcept {
        size_t size = 0;
        while (size < last_error.size() && text[size]) size++;
        std::memcpy(last_error.data(), text, size);
        last_error_size = static_cast<int32_t>(size);
    };
    try { return function(); }
    catch (const std::exception &error) { remember(error.what()); }
    catch (...) { remember("Unknown native inference error"); }
    return -1;
}

int32_t copy_bytes(const std::string &value, uint8_t *output, int32_t capacity) {
    if (capacity < 0 || (capacity && !output)) fail("Invalid output buffer");
    if (value.size() > static_cast<size_t>(INT32_MAX)) fail("Output is too large");
    const auto count = static_cast<int32_t>(value.size());
    if (capacity >= count && count) std::memcpy(output, value.data(), count);
    return count;
}

struct Context {
    std::shared_ptr<Runtime> runtime;
    llama_model *model = nullptr;
    llama_context *context = nullptr;
    const llama_vocab *vocabulary = nullptr;
    int32_t vocabulary_size = 0, max_sequences = 0, batch_size = 0;
    int32_t selected_device = -2;
    std::string fallback_reason;
    std::mutex mutex;
    bool pending = false;
    std::vector<llama_token> tokens;
    std::vector<llama_pos> positions;
    std::vector<llama_seq_id> sequences;
    std::vector<llama_seq_id *> sequence_pointers;
    std::vector<int32_t> sequence_counts;
    std::vector<int8_t> logits;
    std::array<ggml_backend_dev_t, 2> devices{};

    std::unique_lock<std::mutex> lock() {
        std::unique_lock<std::mutex> lock(mutex);
        if (!context || !model) fail("Native context is closed");
        return lock;
    }

    void synchronize() {
        runtime->api.llama_synchronize(context);
        pending = false;
    }
    void release() noexcept {
        if (context) {
            try { runtime->api.llama_synchronize(context); } catch (...) {}
            try { runtime->api.llama_free(context); } catch (...) {}
            context = nullptr;
        }
        if (model) { try { runtime->api.llama_model_free(model); } catch (...) {} model = nullptr; }
        vocabulary = nullptr; pending = false;
    }
    ~Context() { release(); }
};

std::mutex handles_mutex;
std::unordered_map<void *, std::shared_ptr<Context>> handles;
uintptr_t next_handle = 1;

std::shared_ptr<Context> get(void *handle) {
    std::lock_guard<std::mutex> lock(handles_mutex);
    const auto found = handles.find(handle);
    if (found == handles.end()) fail("Invalid or closed native context");
    return found->second;
}

void valid_token(const Context &context, int32_t token) {
    if (token < 0 || token >= context.vocabulary_size) fail("Token is outside the vocabulary");
}

std::string absolute_model_path(const char *value) {
    if (!value || !*value) fail("A model path is required");
    const std::string path(value);
    if (!cp_bridge::valid_utf8(reinterpret_cast<const uint8_t *>(path.data()), path.size())) fail("Model path is not UTF-8");
    const auto file = std::filesystem::u8path(path);
    if (!file.is_absolute() || !std::filesystem::is_regular_file(file)) fail("Model path must be an absolute regular file");
    return std::filesystem::canonical(file).u8string();
}

int32_t choose_gpu(const Runtime &runtime) {
    auto &api = runtime.api;
    int32_t best = -2;
    int best_kind = -1;
    size_t most_free = 0;
    const auto count = api.ggml_backend_dev_count();
    if (count > INT32_MAX) fail("Too many inference devices");
    for (size_t index = 0; index < count; index++) {
        const auto device = api.ggml_backend_dev_get(index);
        const auto type = api.ggml_backend_dev_type(device);
        const int kind = type == GGML_BACKEND_DEVICE_TYPE_GPU ? 2 : type == GGML_BACKEND_DEVICE_TYPE_IGPU ? 1 : 0;
        if (!kind) continue;
        size_t free = 0, total = 0;
        api.ggml_backend_dev_memory(device, &free, &total);
        if (kind > best_kind || (kind == best_kind && free > most_free)) {
            best = static_cast<int32_t>(index); best_kind = kind; most_free = free;
        }
    }
    return best;
}

void initialize(Context &context, const std::string &path, int32_t gpu_layers,
    int32_t n_context, int32_t max_sequences, int32_t batch_size, int32_t threads,
    int32_t device_index) {
    auto &api = context.runtime->api;
    context.devices.fill(nullptr);
    const bool gpu = device_index >= 0 && api.ggml_backend_dev_type(api.ggml_backend_dev_get(device_index)) != GGML_BACKEND_DEVICE_TYPE_CPU;
    if (gpu) context.devices[0] = api.ggml_backend_dev_get(device_index);
    auto model_parameters = api.llama_model_default_params();
    model_parameters.devices = context.devices.data();
    model_parameters.n_gpu_layers = gpu ? gpu_layers : 0;
    model_parameters.split_mode = LLAMA_SPLIT_MODE_NONE;
    model_parameters.main_gpu = 0;
    context.model = api.llama_model_load_from_file(path.c_str(), model_parameters);
    if (!context.model) fail(gpu ? "GPU model loading failed" : "CPU model loading failed");

    auto parameters = api.llama_context_default_params();
    parameters.n_ctx = static_cast<uint32_t>(n_context);
    parameters.n_seq_max = static_cast<uint32_t>(max_sequences);
    parameters.n_batch = static_cast<uint32_t>(batch_size);
    parameters.n_ubatch = static_cast<uint32_t>(batch_size);
    parameters.n_threads = threads;
    parameters.n_threads_batch = threads;
    parameters.kv_unified = true;
    parameters.swa_full = true;
    parameters.offload_kqv = gpu;
    parameters.op_offload = gpu;
    parameters.flash_attn_type = LLAMA_FLASH_ATTN_TYPE_AUTO;
    parameters.no_perf = true;
    context.context = api.llama_init_from_model(context.model, parameters);
    if (!context.context) fail(gpu ? "GPU context allocation failed" : "CPU context allocation failed");
    const auto actual_context = api.llama_n_ctx(context.context);
    if (!actual_context || actual_context > 32768) fail("The runtime did not honor the bounded context size");
    context.vocabulary = api.llama_model_get_vocab(context.model);
    if (!context.vocabulary) fail("Model vocabulary is unavailable");
    context.vocabulary_size = api.llama_vocab_n_tokens(context.vocabulary);
    if (context.vocabulary_size <= 0) fail("Model vocabulary is empty");
    context.max_sequences = max_sequences; context.batch_size = batch_size;
    context.selected_device = gpu ? device_index : -2;
}
} // namespace

namespace cp_bridge {
bool valid_utf8(const uint8_t *bytes, size_t size) {
    for (size_t index = 0; index < size;) {
        const auto first = bytes[index++];
        if (first <= 0x7f) continue;
        int remaining;
        uint32_t code, minimum;
        if (first >= 0xc2 && first <= 0xdf) { remaining = 1; code = first & 0x1f; minimum = 0x80; }
        else if (first >= 0xe0 && first <= 0xef) { remaining = 2; code = first & 0x0f; minimum = 0x800; }
        else if (first >= 0xf0 && first <= 0xf4) { remaining = 3; code = first & 7; minimum = 0x10000; }
        else return false;
        if (size - index < static_cast<size_t>(remaining)) return false;
        while (remaining--) { const auto next = bytes[index++]; if ((next & 0xc0) != 0x80) return false; code = (code << 6) | (next & 0x3f); }
        if (code < minimum || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff)) return false;
    }
    return true;
}

std::string json_quote(const std::string &value) {
    std::string result = "\"";
    const char *hex = "0123456789abcdef";
    for (const auto character : value) {
        const auto byte = static_cast<uint8_t>(character);
        if (byte == '"' || byte == '\\') { result += '\\'; result += character; }
        else if (byte < 0x20) { result += "\\u00"; result += hex[byte >> 4]; result += hex[byte & 15]; }
        else result += character;
    }
    return result + '"';
}

std::string devices_json(const Runtime &runtime) {
    if (!runtime.device_snapshot.empty()) return runtime.device_snapshot;
    auto &api = runtime.api;
    std::string result = "[";
    const auto count = api.ggml_backend_dev_count();
    if (count > INT32_MAX) fail("Too many inference devices");
    for (size_t index = 0; index < count; index++) {
        const auto device = api.ggml_backend_dev_get(index);
        size_t free = 0, total = 0;
        api.ggml_backend_dev_memory(device, &free, &total);
        const auto type = api.ggml_backend_dev_type(device);
        const char *name = api.ggml_backend_dev_name(device), *description = api.ggml_backend_dev_description(device);
        const char *kind = type == GGML_BACKEND_DEVICE_TYPE_CPU ? "cpu" : type == GGML_BACKEND_DEVICE_TYPE_GPU ? "gpu" : type == GGML_BACKEND_DEVICE_TYPE_IGPU ? "integrated_gpu" : "accelerator";
        if (index) result += ',';
        result += "{\"index\":" + std::to_string(index) + ",\"type\":" + json_quote(kind) + ",\"name\":" + json_quote(name ? name : "") + ",\"description\":" + json_quote(description ? description : "") + ",\"free_bytes\":" + std::to_string(free) + ",\"total_bytes\":" + std::to_string(total) + '}';
    }
    return result + ']';
}
} // namespace cp_bridge

uint32_t cp_llama_abi_version(void) { return 1; }
int32_t cp_llama_last_error(uint8_t *output, int32_t capacity) {
    if (capacity < 0 || (capacity && !output)) return -1;
    if (capacity >= last_error_size && last_error_size) std::memcpy(output, last_error.data(), last_error_size);
    return last_error_size;
}

int32_t cp_llama_open(const char *model_path, const char *directory,
    int32_t gpu_layers, int32_t n_context, int32_t max_sequences,
    int32_t batch_size, int32_t threads, int32_t device_index, void **output) {
    if (output) *output = nullptr;
    return guarded([&]() -> int32_t {
        if (!output || n_context < 32 || n_context > 32768 || max_sequences < 1 || max_sequences > 32
            || batch_size < 1 || batch_size > 256 || threads < 1 || threads > 256 || gpu_layers < -1 || gpu_layers > 10000 || device_index < -2) fail("Invalid native context parameters");
        const auto path = absolute_model_path(model_path);
        auto context = std::make_shared<Context>();
        context->runtime = cp_bridge::load_runtime(directory);
        auto &api = context->runtime->api;
        if (device_index >= 0 && static_cast<size_t>(device_index) >= api.ggml_backend_dev_count()) fail("Selected inference device does not exist");
        if (device_index >= 0) {
            const auto type = api.ggml_backend_dev_type(api.ggml_backend_dev_get(device_index));
            if (type != GGML_BACKEND_DEVICE_TYPE_CPU && type != GGML_BACKEND_DEVICE_TYPE_GPU && type != GGML_BACKEND_DEVICE_TYPE_IGPU) fail("Selected inference device is not supported");
        }
        const auto selected = gpu_layers == 0 || device_index == -2 ? -2 : device_index == -1 ? choose_gpu(*context->runtime) : device_index;
        try { initialize(*context, path, gpu_layers, n_context, max_sequences, batch_size, threads, selected); }
        catch (const std::exception &error) {
            if (device_index != -1 || selected < 0) throw;
            context->fallback_reason = error.what();
            context->release();
            initialize(*context, path, 0, n_context, max_sequences, batch_size, threads, -2);
        }
        std::lock_guard<std::mutex> lock(handles_mutex);
        if (next_handle == std::numeric_limits<uintptr_t>::max()) fail("Native handle space exhausted");
        void *handle = reinterpret_cast<void *>(next_handle++);
        handles.emplace(handle, context);
        *output = handle;
        return 0;
    });
}

void cp_llama_close(void *handle) {
    guarded([&]() -> int32_t {
        std::shared_ptr<Context> context;
        { std::lock_guard<std::mutex> lock(handles_mutex); const auto found = handles.find(handle); if (found == handles.end()) fail("Invalid or closed native context"); context = found->second; handles.erase(found); }
        auto lock = context->lock();
        context->release();
        return 0;
    });
}

int32_t cp_llama_tokenize(void *handle, const uint8_t *text, int32_t length,
    int32_t *output, int32_t capacity, uint8_t add_special, uint8_t parse_special) {
    return guarded([&]() -> int32_t {
        if (length < 0 || length > maximum_text_bytes || (length && !text) || capacity < 0 || capacity > maximum_text_bytes || (capacity && !output)
            || add_special > 1 || parse_special > 1 || !cp_bridge::valid_utf8(text, length)) fail("Invalid UTF-8 tokenization input or output buffer");
        const auto context = get(handle); auto lock = context->lock();
        auto &api = context->runtime->api;
        const char *input = length ? reinterpret_cast<const char *>(text) : "";
        const auto probe = api.llama_tokenize(context->vocabulary, input, length, nullptr, 0, add_special != 0, parse_special != 0);
        if (probe == INT32_MIN) fail("Tokenization overflow");
        const auto count = probe < 0 ? -probe : probe;
        if (count > maximum_text_bytes) fail("Tokenization output exceeds its bound");
        if (count && capacity >= count) {
            std::vector<int32_t> tokens(count);
            const auto actual = api.llama_tokenize(context->vocabulary, input, length, tokens.data(), count, add_special != 0, parse_special != 0);
            if (actual != count) fail("Tokenization changed between size and output calls");
            std::memcpy(output, tokens.data(), static_cast<size_t>(count) * sizeof(int32_t));
        }
        return count;
    });
}

int32_t cp_llama_decode(void *handle, const int32_t *tokens, const int32_t *positions,
    const int32_t *sequence_ids, const uint8_t *logits_flags, int32_t count) {
    return guarded([&]() -> int32_t {
        const auto context = get(handle); auto lock = context->lock();
        if (context->pending) context->synchronize();
        if (count < 1 || count > context->batch_size || !tokens || !positions || !sequence_ids || !logits_flags) fail("Invalid decode batch");
        std::unordered_set<uint64_t> seen;
        const auto limit = context->runtime->api.llama_n_ctx(context->context);
        for (int32_t index = 0; index < count; index++) {
            valid_token(*context, tokens[index]);
            if (positions[index] < 0 || static_cast<uint32_t>(positions[index]) >= limit || sequence_ids[index] < 0 || sequence_ids[index] >= context->max_sequences || logits_flags[index] > 1) fail("Invalid decode position, sequence or logits flag");
            const auto key = (static_cast<uint64_t>(sequence_ids[index]) << 32) | static_cast<uint32_t>(positions[index]);
            if (!seen.insert(key).second) fail("Duplicate position in a decode sequence");
        }
        context->tokens.assign(tokens, tokens + count); context->positions.assign(positions, positions + count);
        context->sequences.assign(sequence_ids, sequence_ids + count); context->logits.assign(logits_flags, logits_flags + count);
        context->sequence_counts.assign(count, 1); context->sequence_pointers.resize(count);
        for (int32_t index = 0; index < count; index++) context->sequence_pointers[index] = &context->sequences[index];
        llama_batch batch{};
        batch.n_tokens = count; batch.token = context->tokens.data(); batch.pos = context->positions.data();
        batch.n_seq_id = context->sequence_counts.data(); batch.seq_id = context->sequence_pointers.data(); batch.logits = context->logits.data();
        const auto result = context->runtime->api.llama_decode(context->context, batch);
        context->pending = true;
        if (result != 0) { context->synchronize(); context->logits.clear(); fail("llama_decode failed with status " + std::to_string(result)); }
        return 0;
    });
}

int32_t cp_llama_synchronize(void *handle) {
    return guarded([&]() -> int32_t { const auto context = get(handle); auto lock = context->lock(); context->synchronize(); return 0; });
}

int32_t cp_llama_sample_greedy(void *handle, int32_t index) {
    return guarded([&]() -> int32_t {
        const auto context = get(handle); auto lock = context->lock();
        if (context->pending) context->synchronize();
        if (index < 0 || static_cast<size_t>(index) >= context->logits.size() || !context->logits[index]) fail("No logits were requested at this batch index");
        const auto values = context->runtime->api.llama_get_logits_ith(context->context, index);
        if (!values) fail("Requested logits are unavailable");
        int32_t best = -1; float largest = -std::numeric_limits<float>::infinity();
        for (int32_t token = 0; token < context->vocabulary_size; token++) if (!std::isnan(values[token]) && values[token] > largest) { largest = values[token]; best = token; }
        if (best < 0) fail("No valid token logits are available");
        return best;
    });
}

int32_t cp_llama_token_piece(void *handle, int32_t token, uint8_t *output, int32_t capacity) {
    return guarded([&]() -> int32_t {
        if (capacity < 0 || capacity > maximum_text_bytes || (capacity && !output)) fail("Invalid token-piece buffer");
        const auto context = get(handle); auto lock = context->lock(); valid_token(*context, token);
        auto &api = context->runtime->api;
        const auto probe = api.llama_token_to_piece(context->vocabulary, token, nullptr, 0, 0, true);
        if (probe == INT32_MIN) fail("Token-piece overflow");
        const auto count = probe < 0 ? -probe : probe;
        if (count > maximum_text_bytes) fail("Token piece exceeds its bound");
        if (count && capacity >= count) {
            std::vector<char> bytes(count);
            if (api.llama_token_to_piece(context->vocabulary, token, bytes.data(), count, 0, true) != count) fail("Token piece changed between size and output calls");
            std::memcpy(output, bytes.data(), count);
        }
        return count;
    });
}

int32_t cp_llama_is_eog(void *handle, int32_t token) {
    return guarded([&]() -> int32_t { const auto context = get(handle); auto lock = context->lock(); valid_token(*context, token); return context->runtime->api.llama_vocab_is_eog(context->vocabulary, token) ? 1 : 0; });
}

int32_t cp_llama_seq_remove(void *handle, int32_t sequence, int32_t start, int32_t end) {
    return guarded([&]() -> int32_t {
        const auto context = get(handle); auto lock = context->lock();
        if (sequence < 0 || sequence >= context->max_sequences || start < -1 || end < -1 || (end >= 0 && start >= 0 && end < start)) fail("Invalid sequence removal range");
        context->synchronize();
        const auto memory = context->runtime->api.llama_get_memory(context->context);
        if (!memory || !context->runtime->api.llama_memory_seq_rm(memory, sequence, start, end)) fail("Sequence removal failed");
        context->logits.clear();
        return 0;
    });
}

int32_t cp_llama_context_size(void *handle) {
    return guarded([&]() -> int32_t { const auto context = get(handle); auto lock = context->lock(); return static_cast<int32_t>(context->runtime->api.llama_n_ctx(context->context)); });
}

int32_t cp_llama_info(void *handle, uint8_t *output, int32_t capacity) {
    return guarded([&]() -> int32_t {
        const auto context = get(handle); auto lock = context->lock();
        const auto &runtime = *context->runtime;
        std::string value = "{\"abi\":1,\"llama_commit\":" + cp_bridge::json_quote(cp_bridge::upstream_commit)
            + ",\"device_index\":" + std::to_string(context->selected_device)
            + ",\"context_size\":" + std::to_string(runtime.api.llama_n_ctx(context->context))
            + ",\"sequence_context_size\":" + std::to_string(runtime.api.llama_n_ctx_seq(context->context))
            + ",\"max_sequences\":" + std::to_string(context->max_sequences)
            + ",\"batch_size\":" + std::to_string(context->batch_size)
            + ",\"fallback_reason\":" + cp_bridge::json_quote(context->fallback_reason)
            + ",\"devices\":" + cp_bridge::devices_json(runtime) + ",\"warnings\":[";
        for (size_t index = 0; index < runtime.warnings.size(); index++) { if (index) value += ','; value += cp_bridge::json_quote(runtime.warnings[index]); }
        return copy_bytes(value + "]}", output, capacity);
    });
}

int32_t cp_llama_devices(const char *directory, uint8_t *output, int32_t capacity) {
    return guarded([&]() -> int32_t { return copy_bytes(cp_bridge::devices_json(*cp_bridge::load_runtime(directory)), output, capacity); });
}
