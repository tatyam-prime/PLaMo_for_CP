"""Verify a frozen bundle starts offline without fetching or loading a model."""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def smoke_frozen_native_worker(executable: Path, environment: dict, *, run=None) -> None:
    """Check windowed EXE pipe framing without loading a DLL or a model."""
    from plamo_cp.native_ipc import read_frame, write_frame

    request = io.BytesIO()
    # A deliberately unsupported first frame must produce one bounded fatal
    # response. This exercises console=False inherited stdin/stdout handles.
    write_frame(request, {"type": "smoke-invalid-init"})
    result = (run or subprocess.run)(
        [str(executable.resolve()), "--native-worker"],
        input=request.getvalue(),
        capture_output=True,
        env=environment,
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = io.BytesIO(result.stdout)
    frame = read_frame(output)
    if result.returncode != 1 or frame is None or frame.get("type") != "fatal" or output.read():
        raise RuntimeError("The frozen native worker did not preserve its isolated IPC protocol.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--windows", action="store_true")
    parser.add_argument("--port", type=int, default=0, help="0 selects an unused local port")
    args = parser.parse_args()
    # Do not mistake an existing developer/benchmark server for this child.
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", args.port))
        port = reservation.getsockname()[1]
    logs = Path(".local/distribution-smoke")
    logs.mkdir(parents=True, exist_ok=True)
    command = [str(args.executable.resolve())]
    if args.windows:
        from plamo_cp.llama_backend import _runtime_executable

        runtime = args.executable.resolve().parent / "_internal/llama"
        verified_server = _runtime_executable(runtime, verify=True)
        if verified_server is None:
            raise RuntimeError("The bundled CPU fallback failed its fixed-archive verification.")
        subprocess.run([str(verified_server), "--version"], check=True, timeout=30)
        command.append("--server-child")
    command += ["--port", str(port)]
    environment = dict(os.environ)
    environment["HF_HUB_OFFLINE"] = "1"
    if args.windows and (args.executable.resolve().parent / "_internal/native").is_dir():
        smoke_frozen_native_worker(args.executable, environment)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f"http://127.0.0.1:{port}"
    with (logs / "server.log").open("wb") as output:
        child = subprocess.Popen(command, cwd=logs, env=environment, stdout=output, stderr=output)
        try:
            health = None
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError(f"Frozen server exited: {child.returncode}")
                try:
                    with opener.open(base + "/api/health", timeout=1) as response:
                        health = json.loads(response.read())
                    break
                except (OSError, ValueError):
                    time.sleep(0.2)
            if health is None or health.get("app") != "plamo-for-cp":
                raise RuntimeError("The bundle did not start the expected application.")
            if health["engine"]["loaded_model"] is not None:
                raise RuntimeError("A model loaded without an explicit user action.")
            for path in [
                "/",
                "/api/setup",
                "/api/models",
                "/api/models/storage",
                "/api/cache",
                "/text",
                "/text.js",
                "/translation-jobs.js",
                "/userscript-version.js",
                "/install.user.js",
                "/licenses/plamo-community-license-ja.txt",
                "/licenses/plamo-community-license-en.txt",
            ]:
                with opener.open(base + path, timeout=3) as response:
                    body = response.read()
                    if response.status != 200 or not body:
                        raise RuntimeError(f"Missing bundled resource: {path}")
                    if args.windows and path == "/api/setup":
                        setup = json.loads(body)
                        if (
                            setup.get("supported_batch_sizes") != list(range(1, 33))
                            or setup.get("supported_translation_jobs") is not True
                            or setup.get("supported_translation_previews") is not True
                        ):
                            raise RuntimeError(
                                "The Windows bundle is missing its streaming job capabilities."
                            )
            try:
                opener.open(base + "/extension.zip", timeout=3)
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
            else:
                raise RuntimeError("The obsolete Chrome extension must not be distributed.")
            with opener.open(base + "/pdf", timeout=3) as response:
                if response.geturl() != base + "/text":
                    raise RuntimeError(
                        "The old entry point did not redirect to the text workspace."
                    )
            for path in ["/pdf.js", "/api/pdf/extract", "/api/pdf/fetch"]:
                try:
                    opener.open(base + path, timeout=3)
                except urllib.error.HTTPError as error:
                    if error.code != 404:
                        raise
                else:
                    raise RuntimeError(f"Removed workflow is still present: {path}")
            print(json.dumps({"status": "ok", "health": health}, ensure_ascii=False))
        finally:
            if child.poll() is None:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], check=False)
                else:
                    child.terminate()
                try:
                    child.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            windowed_log = Path.home() / ".cache/plamo-cp/logs/desktop.log"
            if args.windows and windowed_log.is_file():
                shutil.copyfile(windowed_log, logs / "windowed-server.log")


if __name__ == "__main__":
    main()
