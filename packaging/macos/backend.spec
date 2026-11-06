# Run with the repository root as the working directory.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata

root = Path(SPECPATH).resolve().parent.parent
datas = collect_data_files("plamo_cp")
userscript = root / "dist/plamo-for-cp.user.js"
if userscript.is_file():
    datas += [(str(userscript), "plamo_cp/assets")]
binaries = []
hiddenimports = [
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.lifespan.on", "transformers.tokenization_utils",
    "transformers.dynamic_module_utils", "transformers.models.auto.tokenization_auto",
    "transformers.generation.configuration_utils", "numba.typed", "numba.core.types",
]
for package in ("mlx", "mlx_lm", "numba", "llvmlite"):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports
datas += collect_data_files("transformers", include_py_files=True)
for distribution in ("mlx", "mlx-metal", "mlx-lm", "transformers", "numba", "llvmlite",
                     "numpy", "huggingface-hub", "tokenizers", "safetensors", "regex"):
    datas += copy_metadata(distribution)

analysis = Analysis(
    [str(root / "packaging/macos/backend_entry.py")], pathex=[str(root)],
    binaries=binaries, datas=datas, hiddenimports=hiddenimports,
    excludes=["torch", "torchvision", "tensorflow", "jax", "matplotlib", "pytest", "tkinter", "pypdf"],
    noarchive=False,
)
from scripts.collect_bundle_licenses import collect_bundle_licenses
analysis.datas += collect_bundle_licenses(
    analysis, Path(workpath) / "licenses", supplemental_dir=root / "packaging/licenses"
)
archive = PYZ(analysis.pure)
executable = EXE(archive, analysis.scripts, [], exclude_binaries=True,
                 name="plamo-cp-backend", console=True, target_arch="arm64")
collection = COLLECT(executable, analysis.binaries, analysis.datas,
                     name="plamo-cp-backend")
