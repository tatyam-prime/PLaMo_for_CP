"""Small Windows launcher; bundled Tk and Python are supplied by the build."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from .bounded_log import drain_stream
from .desktop import DEFAULT_PORT, log_directory, service_state

SETUP_URL = f"http://127.0.0.1:{DEFAULT_PORT}/"
APP_USER_MODEL_ID = "PLaMoForCP.Desktop"


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


def main() -> None:
    import tkinter as tk
    from tkinter import ttk

    if sys.platform == "win32":
        import ctypes

        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    root.title("PLaMo for CP")
    icon = application_icon()
    if sys.platform == "win32" and icon.is_file():
        try:
            root.iconbitmap(default=str(icon))
        except tk.TclError:
            pass
    root.geometry("480x340")
    root.resizable(False, False)
    content = ttk.Frame(root, padding=24)
    content.pack(fill="both", expand=True)
    ttk.Label(content, text="PLaMo for CP", font=("Segoe UI", 20, "bold")).pack(anchor="w")
    ttk.Label(
        content,
        text="翻訳を、このパソコンで実行します。\n標準は2B。モデルや保存先は管理画面で変更できます。",
    ).pack(anchor="w", pady=(12, 16))
    status = tk.StringVar(value="起動しています…")
    ttk.Label(content, textvariable=status, wraplength=420).pack(anchor="w")
    buttons = ttk.Frame(content)
    buttons.pack(anchor="w", pady=(20, 14))
    child: subprocess.Popen | None = None
    log_thread = None
    closing = False
    ready = False
    log_path = log_directory() / "desktop.log"
    initial_probe = queue.SimpleQueue()

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
        stop_owned_process(child)
        if log_thread is not None:
            log_thread.join(timeout=1)
        root.destroy()

    def mark_ready(existing=False):
        nonlocal ready
        ready = True
        status.set(
            "起動済みのPLaMo for CPに接続しました。"
            if existing
            else "起動しました。管理画面でモデルや保存先を確認できます。"
        )
        open_button.configure(state="normal")
        text_button.configure(state="normal")
        open_setup()

    def poll(attempt=0):
        if closing:
            return
        state = service_state(DEFAULT_PORT)
        if state == "ours":
            mark_ready()
        elif child is not None and child.poll() is not None:
            status.set("起動できませんでした。［ログを表示］で詳しい状態を確認できます。")
        elif attempt >= 40:
            status.set("起動に時間がかかっています。［ログを表示］で状態を確認してください。")
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
            status.set(
                "別のアプリが通信先を使用しています。ほかのPLaMo for CPを終了して、もう一度開いてください。"
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
            status.set("翻訳アプリを起動できませんでした。アプリを展開し直して試してください。")

    def check_existing():
        initial_probe.put(service_state(DEFAULT_PORT))

    def receive_initial_probe():
        if closing:
            return
        try:
            state = initial_probe.get_nowait()
        except queue.Empty:
            root.after(50, receive_initial_probe)
        else:
            begin(state)

    open_button = ttk.Button(buttons, text="管理画面を開く", command=open_setup, state="disabled")
    open_button.pack(side="left")
    text_button = ttk.Button(buttons, text="テキストを翻訳", command=open_text, state="disabled")
    text_button.pack(side="left", padx=(8, 0))
    ttk.Button(buttons, text="ログを表示", command=show_logs).pack(side="left", padx=8)
    ttk.Button(buttons, text="終了", command=close).pack(side="left")
    ttk.Label(
        content,
        text="翻訳中はこのアプリを起動しておいてください。\n終了すると、このアプリが開始した翻訳処理を停止します。",
        foreground="#666666",
    ).pack(anchor="w")
    root.protocol("WM_DELETE_WINDOW", close)
    threading.Thread(target=check_existing, daemon=True).start()
    root.after(50, receive_initial_probe)
    root.mainloop()


if __name__ == "__main__":
    main()
