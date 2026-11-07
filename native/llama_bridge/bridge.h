#ifndef CP_LLAMA_BRIDGE_H
#define CP_LLAMA_BRIDGE_H

#include <stdint.h>

#if defined(_WIN32)
#define CP_LLAMA_API __declspec(dllexport)
#else
#define CP_LLAMA_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* ABI 1: cdecl; only opaque handles, fixed-width integers, and caller-owned
 * arrays cross this boundary. No llama.cpp structs, C++ objects, FILEs, or
 * allocated strings cross CRT boundaries. All functions catch C++ exceptions.
 * A buffer function returns the required count (excluding a terminating NUL),
 * or -1 on error. It writes nothing when capacity is insufficient. Output
 * buffers need not have room for a NUL. All paths are absolute, UTF-8, and NUL
 * terminated. One inference owner calls a context; different calls serialize.
 * last_error is thread-local and must be read on the calling thread.
 */
CP_LLAMA_API uint32_t cp_llama_abi_version(void);
CP_LLAMA_API int32_t cp_llama_last_error(uint8_t *output, int32_t capacity);

/* device_index: -1 selects an available GPU, falling back to CPU on failure;
 * -2 forces CPU; nonnegative indices refer to cp_llama_devices. Explicit device
 * requests fail instead of silently selecting another device. n_gpu_layers=-1
 * offloads all layers. Context is unified across <=32 sequences, <=32768 total
 * KV positions, and <=256 tokens per decode/microbatch. */
CP_LLAMA_API int32_t cp_llama_open(const char *model_path, const char *dll_directory,
    int32_t n_gpu_layers, int32_t n_context, int32_t max_sequences,
    int32_t batch_size, int32_t threads, int32_t device_index, void **output);
CP_LLAMA_API void cp_llama_close(void *handle);
CP_LLAMA_API int32_t cp_llama_tokenize(void *handle, const uint8_t *text,
    int32_t text_length, int32_t *output, int32_t capacity,
    uint8_t add_special, uint8_t parse_special);
CP_LLAMA_API int32_t cp_llama_decode(void *handle, const int32_t *tokens,
    const int32_t *positions, const int32_t *sequence_ids,
    const uint8_t *logits_flags, int32_t count);
CP_LLAMA_API int32_t cp_llama_synchronize(void *handle);
/* Only indices flagged for logits in the latest successful decode may be used.
 * Decode/sample synchronize earlier work when required. An explicit synchronize
 * lets the owner measure GPU completion before applying its duty wait.
 * Greedy selection stays native; -1 means error. */
CP_LLAMA_API int32_t cp_llama_sample_greedy(void *handle, int32_t batch_index);
CP_LLAMA_API int32_t cp_llama_token_piece(void *handle, int32_t token,
    uint8_t *output, int32_t capacity);
CP_LLAMA_API int32_t cp_llama_is_eog(void *handle, int32_t token);
/* Synchronizes before changing KV memory. p0=0,p1=-1 removes an entire sequence. */
CP_LLAMA_API int32_t cp_llama_seq_remove(void *handle, int32_t sequence_id,
    int32_t position_start, int32_t position_end);
CP_LLAMA_API int32_t cp_llama_context_size(void *handle);
/* UTF-8 JSON, using the same buffer convention as token_piece. Device names
 * and memory figures are a startup snapshot, stable across the two buffer
 * calls. Automatic GPU choice separately probes current free device memory. */
CP_LLAMA_API int32_t cp_llama_info(void *handle, uint8_t *output, int32_t capacity);
CP_LLAMA_API int32_t cp_llama_devices(const char *dll_directory,
    uint8_t *output, int32_t capacity);

#ifdef __cplusplus
}
#endif
#endif
