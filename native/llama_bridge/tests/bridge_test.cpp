#include "../bridge.h"
#include "fake_runtime.h"
#include <cassert>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

int main(int argc, char **argv) {
    assert(argc == 2);
    const auto model = std::filesystem::u8path(argv[1]) / std::filesystem::u8path("モデル-日本語.gguf");
    std::ofstream(model).put('x'); // Only a test path: no GGUF reader or model exists.
    const auto path = model.u8string();
    assert(cp_llama_abi_version() == 1);
    void *handle = reinterpret_cast<void *>(99);
    assert(cp_llama_open(path.c_str(), "test-runtime", -1, 32768, 33, 256, 2, -1, &handle) == -1 && handle == nullptr);

    // A GPU allocation failure frees its model before the bounded CPU fallback.
    fake::state().fail_gpu_context = true;
    assert(cp_llama_open(path.c_str(), "test-runtime", -1, 32768, 32, 256, 2, -1, &handle) == 0);
    assert(fake::state().models == 1 && fake::state().contexts == 1);
    const auto &parameters = fake::state().parameters;
    assert(parameters.kv_unified && parameters.swa_full && parameters.n_batch == 256 && parameters.n_ubatch == 256 && parameters.n_seq_max == 32);
    assert(!parameters.offload_kqv && !parameters.op_offload);
    const auto info_size = cp_llama_info(handle, nullptr, 0);
    std::vector<uint8_t> info(info_size);
    assert(cp_llama_info(handle, info.data(), info_size) == info_size);
    const std::string info_text(info.begin(), info.end());
    assert(info_text.find("GPU context allocation failed") != std::string::npos);
    assert(info_text.find("Test GPU 日本語") != std::string::npos);
    assert(cp_llama_context_size(handle) == 32768);
    const auto devices_size = cp_llama_devices("test-runtime", nullptr, 0);
    fake::state().free_memory = 1000000000;
    std::vector<uint8_t> devices(devices_size);
    assert(cp_llama_devices("test-runtime", devices.data(), devices_size) == devices_size);
    assert(std::string(devices.begin(), devices.end()).find("\"free_bytes\":1024") != std::string::npos);

    int32_t token_output[] = {77, 77, 77};
    const uint8_t input[] = {'a', 'b', 'c'};
    assert(cp_llama_tokenize(handle, input, 3, token_output, 2, 0, 1) == 3 && token_output[0] == 77 && token_output[1] == 77);
    assert(cp_llama_tokenize(handle, input, 3, token_output, 3, 0, 1) == 3 && token_output[2] == 2);
    const uint8_t invalid_utf8[] = {0xc0, 0x80};
    assert(cp_llama_tokenize(handle, invalid_utf8, 2, token_output, 3, 0, 1) == -1);
    std::vector<uint8_t> piece(9, 0xab);
    assert(cp_llama_token_piece(handle, 2, piece.data(), 1) == 9 && piece[0] == 0xab);
    assert(cp_llama_token_piece(handle, 2, piece.data(), 9) == 9 && std::string(piece.begin(), piece.end()) == "日本語");
    assert(cp_llama_is_eog(handle, 7) == 1 && cp_llama_is_eog(handle, 2) == 0);
    assert(cp_llama_is_eog(handle, 8) == -1);

    const int32_t tokens[] = {1, 2}, positions[] = {0, 0}, sequences[] = {0, 1};
    const uint8_t flags[] = {0, 1};
    assert(cp_llama_decode(handle, tokens, positions, sequences, flags, 2) == 0);
    assert(cp_llama_sample_greedy(handle, 0) == -1); // No logits at this index.
    assert(cp_llama_sample_greedy(handle, 1) == 2); // Stable first maximum; native sync.
    const auto syncs = fake::state().synchronizes;
    assert(cp_llama_decode(handle, tokens, positions, sequences, flags, 2) == 0);
    assert(cp_llama_synchronize(handle) == 0 && fake::state().synchronizes == syncs + 1);
    assert(cp_llama_sample_greedy(handle, 1) == 2 && fake::state().synchronizes == syncs + 1);
    assert(cp_llama_decode(handle, tokens, positions, sequences, flags, 2) == 0);
    assert(cp_llama_seq_remove(handle, 1, 0, -1) == 0 && fake::state().removals == 1);
    assert(cp_llama_sample_greedy(handle, 1) == -1); // KV changes invalidate logits.
    const auto decodes = fake::state().decodes;
    const int32_t duplicates[] = {0, 0};
    assert(cp_llama_decode(handle, tokens, positions, duplicates, flags, 2) == -1 && fake::state().decodes == decodes);
    cp_llama_close(handle);
    assert(fake::state().models == 0 && fake::state().contexts == 0);
    assert(cp_llama_context_size(handle) == -1);

    // Explicit GPU requests do not silently fall back, and free failed models.
    assert(cp_llama_open(path.c_str(), "test-runtime", -1, 32768, 32, 256, 2, 1, &handle) == -1 && handle == nullptr);
    assert(fake::state().models == 0 && fake::state().contexts == 0);
    const auto error_size = cp_llama_last_error(nullptr, 0);
    std::vector<uint8_t> error(error_size);
    assert(cp_llama_last_error(error.data(), error_size) == error_size);
    assert(std::string(error.begin(), error.end()).find("GPU context allocation failed") != std::string::npos);
    fake::state().fail_cpu_context = true;
    assert(cp_llama_open(path.c_str(), "test-runtime", -1, 32768, 32, 256, 2, -1, &handle) == -1 && handle == nullptr);
    assert(fake::state().models == 0 && fake::state().contexts == 0);
    fake::state().fail_gpu_context = fake::state().fail_cpu_context = false;
    assert(cp_llama_open(path.c_str(), "test-runtime", -1, 32768, 32, 256, 2, -1, &handle) == 0);
    assert(fake::state().parameters.offload_kqv && fake::state().parameters.op_offload);
    std::vector<uint8_t> gpu_info(cp_llama_info(handle, nullptr, 0));
    assert(cp_llama_info(handle, gpu_info.data(), static_cast<int32_t>(gpu_info.size())) == static_cast<int32_t>(gpu_info.size()));
    assert(std::string(gpu_info.begin(), gpu_info.end()).find("\"device_index\":1") != std::string::npos);
    cp_llama_close(handle);
    assert(fake::state().models == 0 && fake::state().contexts == 0);
    std::filesystem::remove(model);
    return 0;
}
