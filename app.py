"""Clip to Discord: a friendly Windows app around clip_watcher.py.

Setup window, tray icon, automatic download of the video tools, self-install,
update check and self-removal. Build with build.bat (PyInstaller, one file).
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

import clip_watcher

__version__ = "1.0.0"
APP_NAME = "Clip to Discord"
REPO = "odehamer/clip-to-discord"

HOME_ENV = "CLIPTODISCORD_HOME"
DIR_NAME = "ClipToDiscord"
# Same port as the script's lock, so the app and the script never run together.
# CLIPTODISCORD_PORT exists for testing a second copy beside a running one.
CONTROL_PORT = int(os.environ.get("CLIPTODISCORD_PORT") or clip_watcher.LOCK_PORT)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ClipToDiscord"
FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_SHA_URL = FFMPEG_ZIP_URL + ".sha256"
UPDATE_INTERVAL_S = 24 * 3600
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

ProgressCb = Callable[[int, int], None]
log = logging.getLogger("clip_watcher.app")


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Paths:
    app_dir: Path

    @property
    def config(self) -> Path:
        return self.app_dir / "config.json"

    @property
    def log(self) -> Path:
        return self.app_dir / "clip_watcher.log"

    @property
    def ffmpeg_dir(self) -> Path:
        return self.app_dir / "ffmpeg"

    @property
    def ffmpeg_bin(self) -> Path:
        return self.ffmpeg_dir / "bin"

    @property
    def exe(self) -> Path:
        return self.app_dir / "ClipToDiscord.exe"


def default_paths() -> Paths:
    override = os.environ.get(HOME_ENV)
    if override:
        return Paths(Path(override))
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Paths(Path(base) / DIR_NAME)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def show_error_box(title: str, message: str) -> None:
    """Message box that works without Tk (a windowed exe has no console)."""
    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:  # noqa: BLE001
        print(f"{title}: {message}", file=sys.stderr)


# --------------------------------------------------------------------------
# Pure logic
# --------------------------------------------------------------------------

def parse_version(text: str) -> tuple[int, ...] | None:
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", text or "")
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def is_newer(tag: str, current: str = __version__) -> bool:
    a, b = parse_version(tag), parse_version(current)
    if a is None or b is None:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def select_zip_members(names: list[str]) -> dict[str, str]:
    """Pick the single ffmpeg.exe and ffprobe.exe inside a '<dir>/bin/' folder.

    Returns {"ffmpeg.exe": member, "ffprobe.exe": member}; members with odd names
    (absolute, drive letters, '..') are never selected. Shortest path wins.
    """
    chosen: dict[str, str] = {}
    for name in names:
        norm = name.replace("\\", "/")
        parts = norm.split("/")
        if norm.startswith("/") or ":" in norm or ".." in parts or "" in parts[:-1]:
            continue
        for exe in ("ffmpeg.exe", "ffprobe.exe"):
            if norm.lower().endswith("/bin/" + exe):
                if exe not in chosen or len(norm) < len(chosen[exe].replace("\\", "/")):
                    chosen[exe] = name
    return chosen


def merge_config(existing: dict, webhook_url: str, delete_original: bool) -> dict:
    """Return existing config with the two app-controlled keys set; other keys are kept."""
    merged = dict(existing)
    merged["webhook_url"] = webhook_url
    merged["delete_original_after_upload"] = bool(delete_original)
    return merged


def parse_sha256(text: str) -> str:
    m = re.search(r"\b[0-9a-fA-F]{64}\b", text)
    if not m:
        raise ToolsError("The download could not be checked. Please try again.")
    return m.group(0).lower()


def safe_to_remove(target: Path, home_override: str | None = None) -> bool:
    """Only a folder named ClipToDiscord (or the explicit override) may be deleted."""
    try:
        resolved = Path(target).resolve()
    except OSError:
        return False
    if resolved.name == DIR_NAME:
        return True
    if home_override:
        try:
            return resolved == Path(home_override).resolve()
        except OSError:
            return False
    return False


# --------------------------------------------------------------------------
# Config files
# --------------------------------------------------------------------------

def load_raw_config(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(path: Path, webhook_url: str, delete_original: bool) -> dict:
    merged = merge_config(load_raw_config(path), webhook_url, delete_original)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return merged


def has_valid_config(path: Path) -> bool:
    return clip_watcher.webhook_is_valid(str(load_raw_config(path).get("webhook_url", "")))


# --------------------------------------------------------------------------
# Video tools (ffmpeg) download
# --------------------------------------------------------------------------

class ToolsError(Exception):
    """Getting the video tools failed; the message is shown to the user."""


_fallback_ssl: ssl.SSLContext | None = None


def open_url(req: urllib.request.Request, timeout: float):
    """urlopen using the Windows certificate store, retrying with the bundled certifi
    list when verification fails. Some PCs carry an expired certificate in the Windows
    store that makes Python reject sites whose certificates are in fact valid."""
    global _fallback_ssl
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            raise
    if _fallback_ssl is None:
        import certifi
        _fallback_ssl = ssl.create_default_context(cafile=certifi.where())
    log.info("certificate check failed with the Windows store; using the bundled list")
    return urllib.request.urlopen(req, timeout=timeout, context=_fallback_ssl)


def _request(url: str, timeout: float) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": f"clip-to-discord/{__version__}"})


def fetch_text(url: str, timeout: float = 30) -> str:
    try:
        with open_url(_request(url, timeout), timeout) as resp:
            return resp.read(65536).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log.error("fetch %s failed: %s", url, e)
        raise ToolsError("Could not download the video tools. "
                         "Check your internet connection and try again.") from None


def download_file(url: str, dest: Path, progress: ProgressCb | None = None,
                  timeout: float = 30) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open_url(_request(url, timeout), timeout) as resp, \
                open(dest, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log.error("download %s failed: %s", url, e)
        raise ToolsError("Could not download the video tools. "
                         "Check your internet connection and try again.") from None


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def verify_sha256(path: Path, expected: str) -> bool:
    return sha256_of(path) == expected.strip().lower()


def extract_tools(zip_path: Path, bin_dir: Path) -> None:
    """Extract only ffmpeg.exe and ffprobe.exe, flattened into bin_dir."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = select_zip_members(zf.namelist())
            if len(members) != 2:
                raise ToolsError("The downloaded file did not contain the video tools.")
            for exe, member in members.items():
                part = bin_dir / (exe + ".part")
                written.append(part)
                with zf.open(member) as src, open(part, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            for part in written:
                os.replace(part, bin_dir / part.name[: -len(".part")])
    except zipfile.BadZipFile:
        raise ToolsError("The downloaded file was damaged. Please try again.") from None
    except OSError as e:
        log.error("extract failed: %s", e)
        raise ToolsError("Could not save the video tools to this PC.") from None
    finally:
        for part in written:
            part.unlink(missing_ok=True)


def install_tools(zip_url: str, sha_url: str, ffmpeg_dir: Path,
                  progress: ProgressCb | None = None) -> None:
    """Download, verify and extract the video tools into ffmpeg_dir/bin. Cleans up on failure."""
    part = ffmpeg_dir / "download.zip.part"
    bin_dir = ffmpeg_dir / "bin"
    try:
        expected = parse_sha256(fetch_text(sha_url))
        download_file(zip_url, part, progress)
        if not verify_sha256(part, expected):
            log.error("sha256 mismatch for downloaded video tools")
            raise ToolsError("The download was damaged. Please try again.")
        extract_tools(part, bin_dir)
    except BaseException:
        for exe in ("ffmpeg.exe", "ffprobe.exe"):
            (bin_dir / exe).unlink(missing_ok=True)
        raise
    finally:
        part.unlink(missing_ok=True)


def find_tools(paths: Paths, configured: str | None) -> tuple[Path, Path] | None:
    return clip_watcher.find_ffmpeg(configured, [paths.ffmpeg_bin])


# --------------------------------------------------------------------------
# Autostart (registry)
# --------------------------------------------------------------------------

def set_autostart(enabled: bool, exe_path: Path, subkey: str = RUN_KEY,
                  require_frozen: bool = True) -> bool:
    """Add or remove the Run value. Returns False if skipped (running from source)."""
    import winreg
    if require_frozen and not is_frozen():
        log.info("autostart change skipped (running from source)")
        return False
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, subkey) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, f'"{exe_path}"')
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass
    return True


def autostart_enabled(subkey: str = RUN_KEY) -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
            return True
    except OSError:
        return False


def nvidia_detected() -> bool:
    import winreg
    pf = os.environ.get("ProgramFiles")
    if pf and (Path(pf) / "NVIDIA Corporation").exists():
        return True
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\NVIDIA Corporation\Global\ShadowPlay"):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# Update check
# --------------------------------------------------------------------------

def fetch_latest_tag(repo: str = REPO, timeout: float = 15) -> str:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"User-Agent": f"clip-to-discord/{__version__}",
                 "Accept": "application/vnd.github+json"})
    with open_url(req, timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return str(data.get("tag_name", ""))


# --------------------------------------------------------------------------
# Icon
# --------------------------------------------------------------------------

def draw_icon(size: int, paused: bool = False) -> Image.Image:
    """A rounded blurple square with a white play triangle (grey when paused)."""
    s = 4  # supersample for smooth edges
    big = size * s
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill = (125, 128, 133, 255) if paused else (88, 101, 242, 255)
    d.rounded_rectangle((0, 0, big - 1, big - 1), radius=int(big * 0.22), fill=fill)
    cx, cy, h = big * 0.52, big * 0.5, big * 0.46
    d.polygon([(cx - h * 0.42, cy - h / 2), (cx - h * 0.42, cy + h / 2), (cx + h * 0.55, cy)],
              fill=(255, 255, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


# --------------------------------------------------------------------------
# Single instance / control channel
# --------------------------------------------------------------------------

class ControlServer:
    """Tiny localhost server; one-line commands, replies 'ok'."""

    def __init__(self, handler: Callable[[str], None], port: int = CONTROL_PORT) -> None:
        self.handler = handler
        self.port = port
        self._sock: socket.socket | None = None
        self._closed = threading.Event()

    def start(self) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", self.port))
            s.listen(4)
        except OSError:
            s.close()
            return False
        s.settimeout(0.5)
        self.port = s.getsockname()[1]
        self._sock = s
        threading.Thread(target=self._serve, name="control", daemon=True).start()
        return True

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._closed.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    conn.settimeout(2)
                    line = conn.recv(64).decode("ascii", "ignore").strip().lower()
                    if line in ("show", "quit"):
                        conn.sendall(b"ok\n")
                        self.handler(line)
                except (OSError, UnicodeError):
                    pass

    def close(self) -> None:
        self._closed.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass


def send_command(cmd: str, port: int = CONTROL_PORT, timeout: float = 2) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as c:
            c.sendall(cmd.encode("ascii") + b"\n")
            return c.recv(16).strip() == b"ok"
    except OSError:
        return False


def wait_port_free(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while True:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            if time.time() >= deadline:
                return False
        finally:
            s.close()
        time.sleep(0.25)


# --------------------------------------------------------------------------
# Self-install / removal
# --------------------------------------------------------------------------

def _spawn_detached(args: list[str] | str, **kw) -> None:
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    subprocess.Popen(args, env=env, close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


def same_file(a: Path, b: Path) -> bool:
    """True when both files exist and have identical contents."""
    try:
        return a.stat().st_size == b.stat().st_size and sha256_of(a) == sha256_of(b)
    except OSError:
        return False


def install_self(paths: Paths, current_exe: Path, port: int = CONTROL_PORT) -> str | None:
    """Copy current_exe to the install location and start it. Returns an error text or None
    (None means the installed copy was started and the caller should exit)."""
    try:
        paths.app_dir.mkdir(parents=True, exist_ok=True)
        if send_command("quit", port):
            if not wait_port_free(port, 10):
                return "The running copy of the app did not close in time."
        tmp = paths.exe.with_name(paths.exe.name + ".new")
        last: Exception | None = None
        for _ in range(10):
            try:
                shutil.copy2(current_exe, tmp)
                os.replace(tmp, paths.exe)
                last = None
                break
            except OSError as e:
                last = e
                time.sleep(0.5)
        if last is not None:
            tmp.unlink(missing_ok=True)
            return f"Could not copy the app into place ({last.strerror or last})."
        _spawn_detached([str(paths.exe)], creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
        return None
    except Exception as e:  # noqa: BLE001
        log.exception("self-install failed")
        return str(e)


def remove_local_data(paths: Paths) -> None:
    """Delete settings and video tools only (used when running from source)."""
    if not safe_to_remove(paths.app_dir, os.environ.get(HOME_ENV)):
        raise ToolsError("Refusing to remove an unexpected folder.")
    paths.config.unlink(missing_ok=True)
    shutil.rmtree(paths.ffmpeg_dir, ignore_errors=True)


def schedule_dir_removal(app_dir: Path) -> None:
    """Start a hidden cmd that deletes app_dir after this process has exited."""
    if not safe_to_remove(app_dir, os.environ.get(HOME_ENV)):
        raise ToolsError("Refusing to remove an unexpected folder.")
    d = str(app_dir)
    wait = "ping -n 3 127.0.0.1 >nul"
    cmd = f'cmd /s /c "{wait} & rmdir /s /q "{d}" & {wait} & rmdir /s /q "{d}""'
    _spawn_detached(cmd, creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
                    cwd=os.environ.get("SystemRoot", "C:\\"))


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

DELETE_LABEL = "Delete the original clip after it's posted. The Discord copy will be the only copy."
NO_NVIDIA_WARNING = ("No NVIDIA software was found on this PC. Clips come from NVIDIA Instant "
                     "Replay, so nothing will be posted without it.")
INTRO = ("Press your NVIDIA Instant Replay hotkey in a game and the last 30 seconds are "
         "posted to your Discord channel.")
BEFORE_START = ("1. Press Alt+Z in a game to open the NVIDIA overlay.\n"
                "2. Turn Instant Replay on. Your save hotkey (Alt+F10 by default) then posts a clip.")
RUNNING_NOTE = "Clip to Discord is running. Look for the icon near the clock."
WIDTH = 480


class SettingsWindow:
    def __init__(self, app: "App") -> None:
        import tkinter as tk
        from tkinter import ttk
        self.app = app
        self.tk, self.ttk = tk, ttk
        self.top = tk.Toplevel(app.root)
        self.top.withdraw()
        self.top.title(APP_NAME)
        self.top.resizable(False, False)
        self.top.protocol("WM_DELETE_WINDOW", self.on_close)
        self._icon_ref = None
        try:
            from PIL import ImageTk
            self._icon_ref = ImageTk.PhotoImage(draw_icon(64))
            self.top.iconphoto(True, self._icon_ref)
        except Exception:  # noqa: BLE001
            pass

        raw = load_raw_config(app.paths.config)
        valid = clip_watcher.webhook_is_valid(str(raw.get("webhook_url", "")))
        self.webhook_var = tk.StringVar(value=str(raw.get("webhook_url", "")) if valid else "")
        self.show_var = tk.BooleanVar(value=False)
        self.autostart_var = tk.BooleanVar(value=autostart_enabled() if valid else True)
        self.delete_var = tk.BooleanVar(value=bool(raw.get("delete_original_after_upload", True)))
        self.msg_var = tk.StringVar()
        self.progress_text = tk.StringVar()
        self._build()
        self.fit()

    # -- layout ------------------------------------------------------------
    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        f = ttk.Frame(self.top, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=APP_NAME, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(f, text=INTRO, wraplength=WIDTH - 40).pack(anchor="w", pady=(4, 12))

        box = ttk.LabelFrame(f, text="Before you start", padding=10)
        box.pack(fill="x")
        ttk.Label(box, text=BEFORE_START, wraplength=WIDTH - 60, justify="left").pack(anchor="w")
        if not nvidia_detected():
            self.warning = ttk.Label(box, text=NO_NVIDIA_WARNING, wraplength=WIDTH - 60,
                                     foreground="#b45309", justify="left")
            self.warning.pack(anchor="w", pady=(8, 0))

        ttk.Label(f, text="Discord webhook URL").pack(anchor="w", pady=(14, 2))
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.entry = ttk.Entry(row, textvariable=self.webhook_var, show="\u2022")
        self.entry.pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(row, text="Show", variable=self.show_var,
                        command=self._toggle_show).pack(side="left", padx=(8, 0))
        ttk.Label(f, foreground="#666666", wraplength=WIDTH - 40,
                  text="In Discord: channel settings \u2192 Integrations \u2192 Webhooks \u2192 "
                       "New Webhook \u2192 Copy Webhook URL").pack(anchor="w", pady=(2, 6))
        self.test_btn = ttk.Button(f, text="Send test message", command=self.on_test)
        self.test_btn.pack(anchor="w")
        self.msg_label = tk.Label(f, textvariable=self.msg_var, wraplength=WIDTH - 40,
                                  justify="left", anchor="w", fg="#666666")
        self.msg_label.pack(anchor="w", fill="x", pady=(4, 0))

        self.autostart_cb = tk.Checkbutton(f, text="Start with Windows", variable=self.autostart_var,
                                           anchor="w")
        self.autostart_cb.pack(anchor="w", pady=(10, 0))
        self.delete_cb = tk.Checkbutton(f, text=DELETE_LABEL, variable=self.delete_var,
                                        anchor="w", justify="left", wraplength=WIDTH - 60)
        self.delete_cb.pack(anchor="w", fill="x")

        self.progress_frame = ttk.Frame(f)
        ttk.Label(self.progress_frame, textvariable=self.progress_text,
                  wraplength=WIDTH - 40).pack(anchor="w")
        self.bar = ttk.Progressbar(self.progress_frame, mode="determinate", maximum=100)
        self.bar.pack(fill="x", pady=(4, 0))
        self.retry_btn = ttk.Button(self.progress_frame, text="Retry", command=self.on_retry)
        # shown only when the download failed; progress_frame itself is packed on demand
        self.progress_frame_pack = dict(fill="x", pady=(12, 0))

        self.buttons = ttk.Frame(f)
        self.buttons.pack(fill="x", pady=(16, 0))
        self.close_btn = ttk.Button(self.buttons, text="Close", command=self.on_close)
        self.close_btn.pack(side="right")
        self.save_btn = ttk.Button(self.buttons, text="Save", command=self.on_save)
        self.save_btn.pack(side="right", padx=(0, 8))

    def fit(self) -> None:
        self.top.update_idletasks()
        self.top.geometry(f"{WIDTH}x{self.top.winfo_reqheight()}")

    def _toggle_show(self) -> None:
        self.entry.configure(show="" if self.show_var.get() else "\u2022")

    # -- helpers used by App ----------------------------------------------
    def set_message(self, text: str, kind: str = "info") -> None:
        self.msg_var.set(text)
        self.msg_label.configure(fg={"ok": "#15803d", "error": "#b91c1c"}.get(kind, "#666666"))
        self.fit()

    def show(self) -> None:
        self.top.deiconify()
        self.top.lift()
        self.top.attributes("-topmost", True)
        self.top.after(300, lambda: self.top.attributes("-topmost", False))
        self.entry.focus_set()
        self.fit()

    def hide(self) -> None:
        self.top.withdraw()

    def visible(self) -> bool:
        return self.top.state() == "normal"

    def set_busy(self, busy: bool) -> None:
        self.save_btn.configure(state="disabled" if busy else "normal")

    def show_progress(self, text: str, fraction: float | None = None, error: bool = False) -> None:
        if not self.progress_frame.winfo_ismapped():
            self.progress_frame.pack(before=self.buttons, **self.progress_frame_pack)
        self.progress_text.set(text)
        if fraction is None:
            self.bar.configure(mode="indeterminate")
            self.bar.start(15)
        else:
            self.bar.stop()
            self.bar.configure(mode="determinate", value=fraction * 100)
        if error:
            self.bar.pack_forget()
            self.retry_btn.pack(anchor="w", pady=(6, 0))
        else:
            self.retry_btn.pack_forget()
            if not self.bar.winfo_ismapped():
                self.bar.pack(fill="x", pady=(4, 0))
        self.fit()

    def hide_progress(self) -> None:
        self.bar.stop()
        self.progress_frame.pack_forget()
        self.fit()

    # -- actions -------------------------------------------------------------
    def _valid_url(self) -> str | None:
        url = self.webhook_var.get().strip()
        if clip_watcher.webhook_is_valid(url):
            return url
        self.set_message("That doesn't look like a Discord webhook URL. Copy it again from "
                         "Discord's webhook settings.", "error")
        return None

    def on_test(self) -> None:
        url = self._valid_url()
        if not url:
            return
        self.test_btn.configure(state="disabled")
        self.set_message("Sending\u2026")

        def work() -> None:
            try:
                clip_watcher.post_text(url, "Clip to Discord is connected.")
                result = ("Test message sent. Check your channel.", "ok")
            except clip_watcher.ClipError as e:
                result = (str(e), "error")
            except Exception as e:  # noqa: BLE001
                log.exception("test message failed")
                result = (f"Something went wrong ({type(e).__name__}).", "error")
            self.app.later(lambda: self._test_done(*result))

        threading.Thread(target=work, name="test-message", daemon=True).start()

    def _test_done(self, text: str, kind: str) -> None:
        self.test_btn.configure(state="normal")
        self.set_message(text, kind)

    def on_save(self) -> None:
        url = self._valid_url()
        if not url:
            return
        try:
            save_config(self.app.paths.config, url, self.delete_var.get())
        except OSError as e:
            self.set_message(f"Could not save your settings ({e.strerror or e}).", "error")
            return
        try:
            set_autostart(self.autostart_var.get(), self.app.paths.exe)
        except OSError as e:
            log.error("autostart change failed: %s", e)
        self.set_message("Saved.", "ok")
        self.app.prepare_and_start(hide_when_ready=True)

    def on_retry(self) -> None:
        self.app.prepare_and_start(hide_when_ready=True)

    def on_close(self) -> None:
        if has_valid_config(self.app.paths.config):
            self.hide()
        else:
            self.app.quit()


class App:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.root = None
        self.window: SettingsWindow | None = None
        self.icon = None
        self.paused = threading.Event()
        self.quit_evt = threading.Event()
        self.control: ControlServer | None = None
        self.watcher_stop: threading.Event | None = None
        self.watcher_thread: threading.Thread | None = None
        self.state = "setup"  # setup | downloading | running
        self.busy = False
        self.hide_when_ready = False
        self.update_tag = ""
        self._notified_tag = ""
        self._last_progress = 0.0
        self._calls: queue.Queue[Callable[[], None]] = queue.Queue()

    # -- Tk / tray ---------------------------------------------------------
    def create_root(self) -> None:
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.root.after(50, self._pump)

    def start_tray(self) -> None:
        import pystray
        menu = pystray.Menu(
            pystray.MenuItem(lambda item: self.status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Pause posting", lambda i, it: self.later(self.toggle_pause),
                             checked=lambda item: self.paused.is_set()),
            pystray.MenuItem("Settings\u2026", lambda i, it: self.later(self.show_settings),
                             default=True),
            pystray.MenuItem("Open log", lambda i, it: self.later(self.open_log)),
            pystray.MenuItem(lambda item: f"Update available ({self.update_tag})\u2026",
                             lambda i, it: self.later(self.open_release_page),
                             visible=lambda item: bool(self.update_tag)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Remove from this PC\u2026", lambda i, it: self.later(self.remove_all)),
            pystray.MenuItem("Quit", lambda i, it: self.later(self.quit)),
        )
        self.icon = pystray.Icon(APP_NAME, draw_icon(64), APP_NAME, menu)
        self.icon.run_detached()
        clip_watcher.notify = self.notify
        clip_watcher.open_url = open_url

    def later(self, fn: Callable[[], None]) -> None:
        """Run fn on the Tk thread. Safe to call from any thread: Tk itself is only ever
        touched by _pump, which runs on the Tk thread."""
        self._calls.put(fn)

    def _pump(self) -> None:
        while True:
            try:
                fn = self._calls.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:  # noqa: BLE001 - one failing callback must not stop the pump
                log.exception("UI callback failed")
        if self.root is not None and not self.quit_evt.is_set():
            self.root.after(50, self._pump)

    @property
    def status_text(self) -> str:
        if self.state == "downloading":
            return "Downloading video tools\u2026"
        if self.state == "setup":
            return "Needs setup"
        return "Paused" if self.paused.is_set() else "Running"

    def set_state(self, state: str) -> None:
        self.state = state
        self.refresh_tray()

    def refresh_tray(self) -> None:
        if self.icon is not None:
            try:
                self.icon.icon = draw_icon(64, self.paused.is_set())
                self.icon.update_menu()
            except Exception:  # noqa: BLE001
                log.debug("tray refresh failed", exc_info=True)

    def notify(self, title: str, message: str) -> None:
        try:
            if self.icon is not None:
                self.icon.notify(message, title)
        except Exception:  # noqa: BLE001 - notifying must never raise
            log.debug("tray notification failed", exc_info=True)

    # -- menu actions (all run on the Tk thread) ----------------------------
    def show_settings(self) -> None:
        if self.window is None:
            self.window = SettingsWindow(self)
        self.window.show()

    def toggle_pause(self) -> None:
        if self.paused.is_set():
            self.paused.clear()
        else:
            self.paused.set()
        self.refresh_tray()

    def open_log(self) -> None:
        try:
            self.paths.log.parent.mkdir(parents=True, exist_ok=True)
            self.paths.log.touch(exist_ok=True)
            os.startfile(str(self.paths.log))  # type: ignore[attr-defined]
        except OSError as e:
            log.error("could not open log: %s", e)

    def open_release_page(self) -> None:
        webbrowser.open(f"https://github.com/{REPO}/releases/latest")

    def handle_command(self, cmd: str) -> None:
        self.later(self.show_settings if cmd == "show" else self.quit)

    def quit(self) -> None:
        self.quit_evt.set()
        self.stop_watcher()
        if self.control:
            self.control.close()
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.root is not None:
            self.root.quit()

    def remove_all(self) -> None:
        from tkinter import messagebox
        if not messagebox.askyesno(
                APP_NAME, "Remove Clip to Discord from this PC? This deletes its settings and "
                          "video tools. Your clips and Discord posts are not touched."):
            return
        app_dir = self.paths.app_dir
        if not safe_to_remove(app_dir, os.environ.get(HOME_ENV)):
            messagebox.showerror(APP_NAME, "Refusing to remove an unexpected folder.")
            return
        self.stop_watcher()
        try:
            set_autostart(False, self.paths.exe)
        except OSError as e:
            log.error("could not remove autostart: %s", e)
        try:
            if is_frozen() and Path(sys.executable).resolve() == self.paths.exe.resolve():
                schedule_dir_removal(app_dir)
            else:
                remove_local_data(self.paths)
        except Exception as e:  # noqa: BLE001
            log.exception("removal failed")
            messagebox.showerror(APP_NAME, f"Could not remove everything: {e}")
        self.quit()

    # -- video tools + watcher ------------------------------------------------
    def prepare_and_start(self, hide_when_ready: bool = False) -> None:
        """Make sure the video tools exist (downloading if needed), then (re)start the watcher."""
        if self.busy:
            return
        self.hide_when_ready = hide_when_ready
        cfg = self._load_cfg()
        if cfg is None:
            self.set_state("setup")
            return
        if find_tools(self.paths, cfg.ffmpeg_path):
            self._tools_ready()
            return
        self.busy = True
        self.set_state("downloading")
        if self.window:
            self.window.set_busy(True)
            self.window.show_progress("Downloading video tools\u2026")

        def work() -> None:
            err = ""
            try:
                install_tools(FFMPEG_ZIP_URL, FFMPEG_SHA_URL, self.paths.ffmpeg_dir,
                              lambda d, t: self._progress(d, t))
            except ToolsError as e:
                err = str(e)
            except Exception as e:  # noqa: BLE001
                log.exception("video tools install failed")
                err = f"Something went wrong ({type(e).__name__})."
            self.later(lambda: self._download_done(err))

        threading.Thread(target=work, name="tools-download", daemon=True).start()

    def _progress(self, done: int, total: int) -> None:
        now = time.time()
        if now - self._last_progress < 0.15 and done != total:
            return
        self._last_progress = now
        self.later(lambda: self._show_progress(done, total))

    def _show_progress(self, done: int, total: int) -> None:
        if not self.window:
            return
        mb = done // (1024 * 1024)
        if total:
            text = f"Downloading video tools\u2026 {mb} of {total // (1024 * 1024)} MB"
            self.window.show_progress(text, done / total)
        else:
            self.window.show_progress(f"Downloading video tools\u2026 {mb} MB")

    def _download_done(self, err: str) -> None:
        self.busy = False
        if self.window:
            self.window.set_busy(False)
        if err:
            self.set_state("setup")
            self.notify(APP_NAME, err)
            self.show_settings()
            assert self.window is not None
            self.window.show_progress(err, 0.0, error=True)
            return
        if self.window:
            self.window.hide_progress()
        self._tools_ready()

    def _tools_ready(self) -> None:
        self.start_watcher()
        if self.hide_when_ready:
            self.hide_when_ready = False
            if self.window:
                self.window.hide()
            self.notify(APP_NAME, RUNNING_NOTE)

    def _load_cfg(self) -> clip_watcher.Config | None:
        try:
            cfg = clip_watcher.load_config(self.paths.config, False)
        except clip_watcher.ClipError:
            return None
        return cfg if clip_watcher.webhook_is_valid(cfg.webhook_url) else None

    def stop_watcher(self) -> None:
        if self.watcher_stop is not None:
            self.watcher_stop.set()
        self.watcher_stop = None
        self.watcher_thread = None

    def start_watcher(self) -> None:
        self.stop_watcher()
        cfg = self._load_cfg()
        tools = find_tools(self.paths, cfg.ffmpeg_path) if cfg else None
        if cfg is None or tools is None:
            self.set_state("setup")
            return
        if not Path(cfg.watch_dir).is_dir():
            self.set_state("setup")
            self.notify(APP_NAME, "The Videos folder was not found, so nothing can be watched. "
                                  "Set watch_dir in the settings file or create the folder.")
            log.error("watch_dir does not exist: %s", cfg.watch_dir)
            return
        stop = threading.Event()

        def run() -> None:
            try:
                clip_watcher.watch(cfg, tools, stop, self.paused)
            except Exception:  # noqa: BLE001
                log.exception("watcher crashed")

        self.watcher_stop = stop
        self.watcher_thread = threading.Thread(target=run, name="watcher", daemon=True)
        self.watcher_thread.start()
        self.set_state("running")

    # -- updates ----------------------------------------------------------------
    def update_loop(self) -> None:
        while not self.quit_evt.is_set():
            try:
                tag = fetch_latest_tag()
                if is_newer(tag):
                    self.update_tag = tag if tag.startswith("v") else "v" + tag
                    self.later(self.refresh_tray)
                    if self._notified_tag != self.update_tag:
                        self._notified_tag = self.update_tag
                        self.notify(APP_NAME, f"Version {self.update_tag} is available. "
                                              "Use the tray menu to get it.")
            except Exception as e:  # noqa: BLE001 - includes 404 when no release exists
                log.debug("update check failed: %s", e)
            if self.quit_evt.wait(UPDATE_INTERVAL_S):
                return

    # -- run -----------------------------------------------------------------
    def run(self) -> None:
        self.create_root()
        self.start_tray()
        threading.Thread(target=self.update_loop, name="updates", daemon=True).start()
        if has_valid_config(self.paths.config):
            self.prepare_and_start()
        else:
            self.show_settings()
        self.root.mainloop()
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--version", action="store_true", help="print the version and exit")
    args = ap.parse_args(argv)
    if args.version:
        if sys.stdout is not None:
            print(f"{APP_NAME} {__version__}")
        return 0

    paths = default_paths()
    try:
        paths.app_dir.mkdir(parents=True, exist_ok=True)
        clip_watcher.set_paths(paths.app_dir)
        clip_watcher.setup_logging()
        log.info("%s %s starting", APP_NAME, __version__)

        if is_frozen():
            try:
                same = Path(sys.executable).resolve() == paths.exe.resolve()
            except OSError:
                same = False
            if not same and same_file(Path(sys.executable), paths.exe):
                # The same version is already installed: open its Settings window
                # instead of replacing and silently restarting it.
                if not send_command("show"):
                    _spawn_detached([str(paths.exe)],
                                    creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
                return 0
            if not same:
                err = install_self(paths, Path(sys.executable))
                if err is None:
                    return 0
                log.error("self-install failed: %s", err)
                show_error_box(APP_NAME, f"Could not install the app.\n\n{err}\n\n"
                                         "It will keep running from where it is.")

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass

        app = App(paths)
        app.control = ControlServer(app.handle_command)
        if not app.control.start():
            if send_command("show"):
                return 0
            show_error_box(APP_NAME, "Clip to Discord (or the clip_watcher script) is already "
                                     "running on this PC. Close it first, then try again.")
            return 1
        app.run()
        return 0
    except Exception:  # noqa: BLE001
        log.error("startup failed:\n%s", traceback.format_exc())
        show_error_box(APP_NAME, "Clip to Discord could not start.\n\n"
                                 f"Details were saved to:\n{paths.log}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
