"""Verify a frozen bundle starts offline without fetching or loading a model."""

from __future__ import annotations

import argparse
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
        runtime = args.executable.resolve().parent / "_internal/llama"
        manifest = json.loads((runtime / ".runtime-manifest.json").read_text())
        subprocess.run([str(runtime / manifest["executable"]), "--version"], check=True, timeout=30)
        command.append("--server-child")
    command += ["--port", str(port)]
    environment = dict(os.environ)
    environment["HF_HUB_OFFLINE"] = "1"
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
                "/install.user.js",
                "/licenses/plamo-community-license-ja.txt",
                "/licenses/plamo-community-license-en.txt",
            ]:
                with opener.open(base + path, timeout=3) as response:
                    if response.status != 200 or not response.read():
                        raise RuntimeError(f"Missing bundled resource: {path}")
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
