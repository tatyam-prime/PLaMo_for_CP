#pragma once
#include "../runtime.h"

namespace fake {
struct State {
    int models = 0, contexts = 0, decodes = 0, synchronizes = 0, removals = 0;
    bool fail_gpu_context = false, fail_cpu_context = false;
    size_t free_memory = 1024;
    llama_context_params parameters{};
};
State &state();
}
