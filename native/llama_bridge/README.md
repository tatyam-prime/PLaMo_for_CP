# Windows inference bridge

The bridge keeps llama.cpp structs and logits inside C++, exposing only the
versioned C ABI in `bridge.h`. It is loaded in the application's owned inference
worker, not its HTTP server. The Python owner controls admission, cancellation,
synchronization and duty waits. No weights or inference runtime are downloaded
by this directory or the build helper.

The public headers are from official llama.cpp release **b12119**, commit
`6f1e6543704bc5b080c7a98e7b6f707b31f6ed51`. `vendor/provenance.json` records each
source URL and SHA-256, and `vendor/LICENSE` preserves the full upstream MIT
license. `scripts/build_llama_bridge.py` verifies those headers before building.

```powershell
python -m scripts.build_llama_bridge --output .local/windows-native-runtime
```

Building requires x64 Windows, CMake and Visual Studio C++ tools. The resulting
`cp_llama_bridge.dll`, `bridge-build.json` and `bridge-licenses/` belong next to
the hash-verified, same-release llama/ggml DLLs prepared by the packaging code.
Compilation is serial. There is no import-library dependency.

The loader uses absolute UTF-8 paths converted to Windows wide paths, restricted
`LoadLibraryExW` flags, and explicit CPU/Vulkan/CUDA plugin filenames inside that
verified directory. It does not call bulk backend discovery, inspect PATH/CWD,
or honor `GGML_BACKEND_PATH`. Compatible CPU variants are selected by the
upstream score function. The model loader's upstream `ggml_fopen` handles UTF-8
model paths through `_wfopen`.

Models, contexts and KV memory are released on close or failed GPU allocation.
Automatic GPU selection can fall back once to CPU; its reason is included in
`cp_llama_info`. Explicit device requests fail instead. Loaded backend modules
remain until the worker exits: upstream warns that internal backend threads
make unloading a registered plugin unsafe. No llama or backend DLL is loaded
in the model-free portable test.

```sh
cmake -S native/llama_bridge -B .local/bridge-test -DCP_LLAMA_BRIDGE_TESTS=ON
cmake --build .local/bridge-test --parallel 1
ctest --test-dir .local/bridge-test --output-on-failure
```

The portable test uses an in-memory fake backend to cover buffer sizing,
UTF-8, bounded batched decode, greedy sampling, synchronization before KV
release, stale handles, and cleanup after GPU/CPU allocation failures. It does
not establish Windows DLL compatibility, driver compatibility or inference
quality. Those require separate Windows build and device checks.
