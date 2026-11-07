// Windows-only loader. No PATH/CWD/backend-environment search is used.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include "runtime.h"

#include <algorithm>
#include <cstdio>
#include <filesystem>
#include <mutex>
#include <stdexcept>

namespace cp_bridge {
namespace {
std::mutex runtime_mutex;

std::wstring wide(const char *text) {
    if (!text || !*text) throw std::runtime_error("A runtime directory is required");
    const auto count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text, -1, nullptr, 0);
    if (!count) throw std::runtime_error("Runtime directory is not UTF-8");
    std::wstring result(static_cast<size_t>(count), L'\0');
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text, -1, result.data(), count)) throw std::runtime_error("Runtime directory conversion failed");
    result.pop_back();
    return result;
}

std::filesystem::path checked_directory(const char *directory) {
    const std::filesystem::path input(wide(directory));
    if (!input.is_absolute() || !std::filesystem::is_directory(input)) throw std::runtime_error("Runtime directory must be an absolute existing directory");
    const auto attributes = GetFileAttributesW(input.c_str());
    if (attributes == INVALID_FILE_ATTRIBUTES || attributes & FILE_ATTRIBUTE_REPARSE_POINT) throw std::runtime_error("Runtime directory cannot be a reparse point");
    return std::filesystem::canonical(input);
}

std::filesystem::path checked_file(const std::filesystem::path &root, const std::wstring &name) {
    const auto path = root / name;
    const auto attributes = GetFileAttributesW(path.c_str());
    if (attributes == INVALID_FILE_ATTRIBUTES || attributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY)) throw std::runtime_error("Runtime DLL is missing or is not a regular file");
    if (std::filesystem::canonical(path).parent_path() != root) throw std::runtime_error("Runtime DLL escapes its verified directory");
    return path;
}

class Module {
public:
    HMODULE handle = nullptr;
    explicit Module(const std::filesystem::path &path) {
        handle = LoadLibraryExW(path.c_str(), nullptr, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
        if (!handle) throw std::runtime_error("Cannot load a verified runtime DLL (Windows error " + std::to_string(GetLastError()) + ")");
        try {
            std::wstring loaded(32768, L'\0');
            const auto length = GetModuleFileNameW(handle, loaded.data(), static_cast<DWORD>(loaded.size()));
            if (!length || length >= loaded.size()) throw std::runtime_error("Cannot verify the loaded DLL path");
            loaded.resize(length);
            if (std::filesystem::canonical(loaded) != std::filesystem::canonical(path)) throw std::runtime_error("A DLL with this name is already loaded from another directory");
        } catch (...) { FreeLibrary(handle); handle = nullptr; throw; }
    }
    Module(const Module &) = delete;
    Module &operator=(const Module &) = delete;
    ~Module() { if (handle) FreeLibrary(handle); }
    void retain() { handle = nullptr; }
};

void stderr_log(enum ggml_log_level, const char *text, void *) {
    if (!text) return;
    // stdout belongs exclusively to the owner's framed IPC protocol.
    std::fputs(text, stderr);
    std::fflush(stderr);
}

bool cpu_filename(const std::wstring &name) {
    if (name == L"ggml-cpu.dll") return true;
    if (name.size() <= 13 || name.compare(0, 9, L"ggml-cpu-") || name.compare(name.size() - 4, 4, L".dll")) return false;
    return std::all_of(name.begin() + 9, name.end() - 4, [](wchar_t character) { return (character >= L'a' && character <= L'z') || (character >= L'0' && character <= L'9') || character == L'-' || character == L'_'; });
}

FARPROC symbol(const std::vector<std::unique_ptr<Module>> &modules, const char *name) {
    for (auto it = modules.rbegin(); it != modules.rend(); ++it) if (const auto address = GetProcAddress((*it)->handle, name)) return address;
    throw std::runtime_error(std::string("Pinned runtime export is missing: ") + name);
}

void register_plugin(Runtime &runtime, const std::filesystem::path &root,
    const std::wstring &name, std::vector<std::unique_ptr<Module>> &modules) {
    const auto path = checked_file(root, name);
    // Securely preload all dependencies before upstream's loader obtains an
    // additional reference to this exact path. The bulk discovery functions
    // are deliberately not called: they also honor GGML_BACKEND_PATH.
    auto module = std::make_unique<Module>(path);
    if (!runtime.api.ggml_backend_load(path.u8string().c_str())) throw std::runtime_error("The pinned backend could not initialize");
    modules.push_back(std::move(module));
}
} // namespace

std::shared_ptr<Runtime> load_runtime(const char *directory) {
    std::lock_guard<std::mutex> lock(runtime_mutex);
    const auto root = checked_directory(directory);
    // Deliberately process-lifetime, matching upstream's backend thread and
    // registry lifetime. This is used only in our isolated inference worker.
    static auto *retained = new std::shared_ptr<Runtime>();
    if (*retained) {
        if ((*retained)->directory != root.u8string()) throw std::runtime_error("A worker cannot mix different runtime directories");
        return *retained;
    }
    auto runtime = std::make_shared<Runtime>(); runtime->directory = root.u8string();
    std::vector<std::unique_ptr<Module>> modules;
    for (const auto *name : { L"ggml-base.dll", L"ggml.dll", L"llama.dll" }) modules.push_back(std::make_unique<Module>(checked_file(root, name)));
#define CP_RESOLVE(name) runtime->api.name = reinterpret_cast<decltype(runtime->api.name)>(symbol(modules, #name));
    CP_LLAMA_SYMBOLS(CP_RESOLVE)
#undef CP_RESOLVE
    runtime->api.ggml_log_set(stderr_log, nullptr);
    runtime->api.llama_log_set(stderr_log, nullptr);

    std::vector<std::wstring> candidates;
    for (const auto &entry : std::filesystem::directory_iterator(root)) {
        auto name = entry.path().filename().wstring();
        std::transform(name.begin(), name.end(), name.begin(), [](wchar_t value) { return value >= L'A' && value <= L'Z' ? value + (L'a' - L'A') : value; });
        if (cpu_filename(name)) candidates.push_back(entry.path().filename().wstring());
    }
    std::sort(candidates.begin(), candidates.end());
    int best_score = 0; std::wstring best;
    for (const auto &name : candidates) {
        // The score entry point checks CPU feature compatibility; loading every
        // variant as an active backend could execute unsupported instructions.
        Module candidate(checked_file(root, name));
        using Score = int (__cdecl *)(void);
        const auto score_function = reinterpret_cast<Score>(GetProcAddress(candidate.handle, "ggml_backend_score"));
        const auto score = score_function ? score_function() : name == L"ggml-cpu.dll" ? 1 : 0;
        if (score > best_score) { best_score = score; best = name; }
    }
    if (best.empty()) throw std::runtime_error("No CPU backend supports this processor");
    register_plugin(*runtime, root, best, modules);
    bool has_cpu = false;
    for (size_t index = 0; index < runtime->api.ggml_backend_dev_count(); index++) if (runtime->api.ggml_backend_dev_type(runtime->api.ggml_backend_dev_get(index)) == GGML_BACKEND_DEVICE_TYPE_CPU) has_cpu = true;
    // llama_backend_init itself invokes bulk discovery when the registry is
    // empty. Refuse that path rather than permitting environment/CWD search.
    if (!runtime->api.ggml_backend_reg_count() || !has_cpu) throw std::runtime_error("The explicit CPU backend did not register a CPU device");

    // Once a backend is registered, upstream may keep worker threads alive.
    // Retain modules even if a later optional backend fails. No models or KV
    // allocations exist yet; per-model failures release those independently.
    for (auto &module : modules) module->retain();
    modules.clear();
    for (const auto *name : { L"ggml-vulkan.dll", L"ggml-cuda.dll" }) {
        if (!std::filesystem::exists(root / name)) continue;
        try { register_plugin(*runtime, root, name, modules); }
        catch (const std::exception &error) { runtime->warnings.push_back(std::filesystem::path(name).u8string() + ": " + error.what()); }
        for (auto &module : modules) module->retain();
        modules.clear();
    }
    runtime->api.llama_backend_init();
    runtime->device_snapshot = devices_json(*runtime);
    *retained = runtime;
    return runtime;
}
} // namespace cp_bridge
