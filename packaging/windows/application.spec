from pathlib import Path
import os
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH).resolve().parent.parent
staged = os.environ.get("PLAMO_STAGED_PACKAGE_ROOT")
source_root = Path(staged) if staged else root
native = os.environ.get("PLAMO_NATIVE_RUNTIME_DIR")
if bool(native) != bool(staged):
    raise RuntimeError("Native DLLs and their generated Python pins must be built together.")
datas = collect_data_files("plamo_cp")
icon = root / "packaging/icons/app-icon.ico"
if not icon.is_file():
    raise RuntimeError("The committed Windows application icon is missing.")
datas += [(str(icon), "icons")]
userscript = root / "dist/plamo-for-cp.user.js"
if userscript.is_file():
    datas += [(str(userscript), "plamo_cp/assets")]
runtime = Path(os.environ.get("PLAMO_WINDOWS_RUNTIME_DIR", root / ".local/windows-runtime/llama"))
if not (runtime / ".runtime-manifest.json").is_file():
    raise RuntimeError("Prepare the pinned Windows llama.cpp runtime before building.")
datas += [(str(runtime), "llama")]
if native:
    datas += [(native, "native")]
from plamo_cp.llama_backend import _runtime_archive
runtime_archive = _runtime_archive(runtime)
if runtime_archive is None:
    raise RuntimeError("The pinned runtime ZIP is required as its verification trust anchor.")
if runtime_archive.parent != runtime:
    datas += [(str(runtime_archive), "llama")]
for distribution in ("huggingface-hub", "httpx", "fastapi", "uvicorn"):
    datas += copy_metadata(distribution)
analysis = Analysis(
    [str(root / "packaging/windows/entry.py")], pathex=[str(source_root), str(root)], datas=datas,
    hiddenimports=["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.h11_impl",
                   "uvicorn.lifespan.on", "tkinter", "tkinter.ttk"],
    excludes=["mlx", "mlx_lm", "torch", "torchvision", "tensorflow", "jax", "pytest", "pypdf"],
)
if native:
    from scripts.prepare_native_runtime import verify_analyzed_pins
    verify_analyzed_pins(analysis, source_root / "plamo_cp/_native_runtime_pins.py")
from scripts.collect_bundle_licenses import collect_bundle_licenses
analysis.datas += collect_bundle_licenses(
    analysis, Path(workpath) / "licenses", supplemental_dir=root / "packaging/licenses"
)
archive = PYZ(analysis.pure)
executable = EXE(archive, analysis.scripts, [], exclude_binaries=True,
                 name="PLaMo for 競プロ", console=False, icon=str(icon), upx=False, strip=False)
collection = COLLECT(executable, analysis.binaries, analysis.datas,
                     name="PLaMo for 競プロ", upx=False, strip=False)
