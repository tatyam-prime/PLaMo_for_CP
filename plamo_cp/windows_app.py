"""Small Windows launcher; bundled Tk and Python are supplied by the build."""

from __future__ import annotations

import http.client
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import suppress
from pathlib import Path

from .bounded_log import drain_stream
from .desktop import (
    APPLICATION_ID,
    DEFAULT_PORT,
    HEALTH_MAX_BYTES,
    HEALTH_TIMEOUT,
    log_directory,
    service_state,
)
from .http_cancel import CancellableSocket

SETUP_URL = f"http://127.0.0.1:{DEFAULT_PORT}/"
APP_USER_MODEL_ID = "PLaMoForCP.Desktop"
QUIET_PERCENTAGES = (100, 80, 64, 50, 40, 32, 25, 20)
MODEL_LABELS = {"0.8b": "800M（0.8B）", "2b": "2B", "8b": "8B"}


def settings_summary(health: dict, setup: dict) -> dict[str, str]:
    """Present server-owned settings without selecting or loading a model."""
    engine = health.get("engine")
    engine = engine if isinstance(engine, dict) else {}
    loaded = engine.get("loaded_model")
    if loaded is None:
        model = "なし"
    elif isinstance(loaded, str):
        model = MODEL_LABELS.get(loaded, "—")
    else:
        model = "—"
    level = setup.get("quiet_level")
    if type(level) is int and 0 <= level < len(QUIET_PERCENTAGES):
        utilization = f"{QUIET_PERCENTAGES[level]}%"
    elif type(setup.get("quiet_mode")) is bool:
        utilization = "50%" if setup["quiet_mode"] else "100%"
    else:
        utilization = "—"
    return {
        "model": model,
        "utilization": utilization,
    }


class SettingsSummaryPoller:
    """One cancellable worker; the Tk thread only consumes completed snapshots."""

    def __init__(self, *, port: int = DEFAULT_PORT):
        self.port = port
        self.results = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._running = False
        self._next_refresh = 0.0
        self._connection = None
        self._active_socket = None

    def _read_json(self, path: str) -> dict:
        if self._closed.is_set():
            raise OSError("Summary polling stopped")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=HEALTH_TIMEOUT)
        with self._lock:
            self._connection = connection
            self._active_socket = None
        expired = threading.Event()
        active_socket = None
        response = None

        def abort():
            expired.set()
            if active_socket is not None:
                try:
                    active_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        timer = threading.Timer(HEALTH_TIMEOUT, abort)
        timer.daemon = True
        timer.start()
        try:
            connection.connect()
            connection.sock = CancellableSocket(
                connection.sock, lambda: self._closed.is_set() or expired.is_set()
            )
            active_socket = connection.sock
            with self._lock:
                if self._connection is connection:
                    # getresponse() may clear connection.sock for a
                    # Connection:close response while its body is still read.
                    self._active_socket = active_socket
            if self._closed.is_set() or expired.is_set():
                raise OSError("Summary polling stopped")
            connection.request("GET", path, headers={"Connection": "close"})
            response = connection.getresponse()
            if response.status != 200 or (
                response.length is not None and response.length > HEALTH_MAX_BYTES
            ):
                raise ValueError("Invalid settings response")
            body = response.read(HEALTH_MAX_BYTES + 1)
            if (
                self._closed.is_set()
                or expired.is_set()
                or len(body) > HEALTH_MAX_BYTES
                or response.length not in (None, 0)
            ):
                raise ValueError("Incomplete settings response")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise TypeError("Invalid settings response")
            return value
        finally:
            timer.cancel()
            with suppress(OSError):
                if response is not None:
                    response.close()
            with suppress(OSError):
                connection.close()
            with self._lock:
                if self._connection is connection:
                    self._connection = None
                    self._active_socket = None

    def _load(self):
        connected = False
        summary = None
        try:
            health = self._read_json("/api/health")
            if health.get("app") != APPLICATION_ID:
                raise ValueError("Unrelated local service")
            connected = True
            setup = self._read_json("/api/setup")
            if setup.get("application") != APPLICATION_ID:
                connected = False
                raise ValueError("Unrelated settings response")
            summary = settings_summary(health, setup)
        except (
            OSError,
            http.client.HTTPException,
            ValueError,
            UnicodeError,
            TypeError,
            AttributeError,
        ):
            pass
        finally:
            with self._lock:
                self._running = False
                self._next_refresh = time.monotonic() + 2
            if not self._closed.is_set():
                self.results.put((summary, connected))

    def refresh(self) -> bool:
        with self._lock:
            if self._closed.is_set() or self._running or time.monotonic() < self._next_refresh:
                return False
            self._running = True
        threading.Thread(target=self._load, daemon=True).start()
        return True

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            active_socket = self._active_socket
        if active_socket is not None:
            try:
                active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def application_icon() -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    base = Path(bundled) if bundled else Path(__file__).resolve().parents[1] / "packaging"
    return base / "icons" / "app-icon.ico"


def backend_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--server-child"]
    return [sys.executable, "-m", "plamo_cp.desktop"]


def stop_owned_process(child: subprocess.Popen | None) -> None:
    """Only stop this launcher's child tree, including an owned llama-server."""
    if child is None or child.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(child.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
    else:
        child.terminate()


def main(*, protocol_launch: bool = False) -> None:
    import tkinter as tk
    from tkinter import ttk

    if sys.platform == "win32":
        import ctypes

        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    root.title("PLaMo for 競プロ")
    icon = application_icon()
    if sys.platform == "win32" and icon.is_file():
        try:
            root.iconbitmap(default=str(icon))
        except tk.TclError:
            pass
    root.geometry("420x180")
    root.resizable(False, False)
    if protocol_launch:
        root.deiconify()
        root.lift()
        root.after_idle(root.focus_force)
    content = ttk.Frame(root, padding=16)
    content.pack(fill="both", expand=True)
    ttk.Label(content, text="PLaMo for 競プロ", font=("Segoe UI", 16, "bold")).pack(anchor="w")
    summary_values = {name: tk.StringVar(value="—") for name in ("model", "utilization")}
    summary_rows = ttk.Frame(content)
    summary_rows.pack(fill="x", pady=(12, 8))
    summary_rows.columnconfigure(0, minsize=96)
    summary_rows.columnconfigure(1, weight=1)
    for row, (name, label) in enumerate((("model", "読込中のモデル"), ("utilization", "稼働率"))):
        ttk.Label(summary_rows, text=label).grid(row=row, column=0, sticky="w", pady=4)
        value_view = ttk.Label(summary_rows, textvariable=summary_values[name])
        value_view.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=4)
    status = tk.StringVar(value="起動しています…")
    status_view = ttk.Label(content, textvariable=status, wraplength=388)
    status_view.pack(anchor="w")
    buttons = ttk.Frame(content)
    buttons.pack(anchor="w", pady=(12, 0))
    child: subprocess.Popen | None = None
    log_thread = None
    closing = False
    ready = False
    summary_started = False
    summary_poller = SettingsSummaryPoller()
    log_path = log_directory() / "desktop.log"
    initial_probe = queue.SimpleQueue()

    def resize_window():
        root.update_idletasks()
        # Updating only the dimensions preserves the window's top-left position.
        root.geometry(f"{max(420, root.winfo_reqwidth())}x{max(160, root.winfo_reqheight())}")

    def show_status(message):
        if status.get() == message and status_view.winfo_manager():
            return
        status.set(message)
        status_view.pack(anchor="w", before=buttons)
        resize_window()

    def open_setup():
        if ready:
            webbrowser.open(SETUP_URL)

    def open_text():
        if ready:
            webbrowser.open(SETUP_URL + "text")

    def show_logs():
        log_path.touch(exist_ok=True)
        os.startfile(str(log_path))

    def close():
        nonlocal closing
        closing = True
        summary_poller.close()
        stop_owned_process(child)
        if log_thread is not None:
            log_thread.join(timeout=1)
        root.destroy()

    def mark_ready(existing=False):
        nonlocal ready, summary_started
        ready = True
        summary_started = True
        show_status("設定を確認しています…")
        open_button.configure(state="normal")
        text_button.configure(state="normal")
        summary_poller.refresh()
        if not protocol_launch:
            open_setup()

    def poll(attempt=0):
        if closing:
            return
        threading.Thread(
            target=lambda: initial_probe.put(("startup", attempt, service_state(DEFAULT_PORT))),
            daemon=True,
        ).start()

    def startup_probe_result(attempt, state):
        if state == "ours":
            mark_ready()
        elif child is not None and child.poll() is not None:
            show_status("起動できませんでした。［ログを表示］で詳しい状態を確認できます。")
        elif attempt >= 40:
            show_status("起動に時間がかかっています。［ログを表示］で状態を確認してください。")
        else:
            root.after(500, poll, attempt + 1)

    def begin(existing_state):
        nonlocal child, log_thread
        if closing:
            return
        if existing_state == "ours":
            mark_ready(existing=True)
            return
        if existing_state == "other":
            show_status(
                "別のアプリが通信先を使用しています。ほかのPLaMo for 競プロを終了して、もう一度開いてください。"
            )
            return
        try:
            child = subprocess.Popen(
                backend_command(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=Path.home(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            log_thread = threading.Thread(
                target=drain_stream, args=(child.stdout, log_path), daemon=True
            )
            log_thread.start()
            root.after(100, poll)
        except OSError:
            show_status("翻訳アプリを起動できませんでした。アプリを展開し直して試してください。")

    def check_existing():
        initial_probe.put(("initial", 0, service_state(DEFAULT_PORT)))

    def receive_initial_probe():
        if closing:
            return
        try:
            kind, attempt, state = initial_probe.get_nowait()
        except queue.Empty:
            pass
        else:
            if kind == "initial":
                begin(state)
            else:
                startup_probe_result(attempt, state)
        root.after(50, receive_initial_probe)

    def update_summary():
        nonlocal ready
        if closing:
            return
        try:
            values, connected = summary_poller.results.get_nowait()
        except queue.Empty:
            pass
        else:
            ready = connected
            open_button.configure(state="normal" if connected else "disabled")
            text_button.configure(state="normal" if connected else "disabled")
            if values is not None:
                for name, variable in summary_values.items():
                    variable.set(values[name])
                if status_view.winfo_manager():
                    status.set("")
                    status_view.pack_forget()
                    resize_window()
            else:
                show_status(
                    "設定を取得できません。管理画面で確認してください。"
                    if connected
                    else "アプリに接続できません。"
                )
        if summary_started and root.state() == "normal" and root.winfo_viewable():
            summary_poller.refresh()
        root.after(200, update_summary)

    open_button = ttk.Button(buttons, text="管理画面を開く", command=open_setup, state="disabled")
    open_button.pack(side="left")
    text_button = ttk.Button(buttons, text="テキストを翻訳", command=open_text, state="disabled")
    text_button.pack(side="left", padx=(6, 0))
    ttk.Button(buttons, text="ログを表示", command=show_logs).pack(side="left", padx=6)
    ttk.Button(buttons, text="終了", command=close).pack(side="left")
    root.protocol("WM_DELETE_WINDOW", close)
    resize_window()
    threading.Thread(target=check_existing, daemon=True).start()
    root.after(50, receive_initial_probe)
    root.after(200, update_summary)
    root.mainloop()


if __name__ == "__main__":
    main()
