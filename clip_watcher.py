"""Watch NVIDIA Instant Replay saves, trim, re-encode and post them to a Discord webhook.

Standard library only. Run with pythonw.exe for a background (no console) watcher.

Usage:
    clip_watcher.py                      watch mode
    clip_watcher.py --once FILE          process one file and exit 0/1
    clip_watcher.py --dry-run ...        encode only: no upload, no deletion
    clip_watcher.py --config PATH        alternative config file
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import logging
import logging.handlers
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TMP_DIR = SCRIPT_DIR / "tmp"
LOG_PATH = SCRIPT_DIR / "clip_watcher.log"
LOCK_PORT = 47653
WEBHOOK_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
)
PLACEHOLDER_MARKER = "PASTE_YOUR"
DATE_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2}) - (\d{2})\.(\d{2})\.(\d{2})")
CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
RETRY_WAITS = (5, 15, 30)

log = logging.getLogger("clip_watcher")


class ClipError(Exception):
    """A clip could not be processed; the message is the reason."""


class TooLargeError(Exception):
    """Discord rejected the upload as too large."""


@dataclass
class Config:
    webhook_url: str = ""
    watch_dir: str = ""
    clip_seconds: float = 30
    max_upload_mb: float = 10
    height: int = 720
    fps: int = 60
    audio_kbps: int = 96
    delete_original_after_upload: bool = True
    poll_seconds: float = 2
    filename_suffix: str = ".DVR.mp4"
    ffmpeg_path: str | None = None


# --------------------------------------------------------------------------
# Pure logic
# --------------------------------------------------------------------------

def webhook_is_valid(url: str) -> bool:
    return bool(url) and PLACEHOLDER_MARKER not in url and url.startswith(WEBHOOK_PREFIXES)


def compute_video_kbps(length: float, max_upload_mb: float, audio_kbps: int) -> int:
    budget_bytes = max_upload_mb * 1024 * 1024 * 0.92
    total_kbps = budget_bytes * 8 / max(length, 0.1) / 1000
    return int(max(300, total_kbps - audio_kbps))


def trim_window(duration: float, clip_seconds: float) -> tuple[float, float]:
    return max(0.0, duration - clip_seconds), min(clip_seconds, duration)


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "clip"


def derive_game_name(path: Path, watch_dir: Path) -> str:
    parent = path.parent
    try:
        same = parent.resolve() == watch_dir.resolve()
    except OSError:
        same = parent == watch_dir
    if not same and parent.name:
        return parent.name
    m = DATE_RE.search(path.name)
    if m:
        text = path.name[: m.start()].strip(" -_.")
        if text:
            return text
    return "Clip"


def upload_filename(game: str, path: Path) -> str:
    m = DATE_RE.search(path.name)
    stamp = f"{m[1]}-{m[2]}-{m[3]} {m[4]}.{m[5]}" if m else ""
    return sanitize_filename(f"{game} {stamp}".strip()) + ".mp4"


def build_multipart(payload_json: str, filename: str, file_bytes: bytes,
                    boundary: str | None = None) -> tuple[bytes, str]:
    boundary = boundary or uuid.uuid4().hex
    safe_name = filename.replace('"', "_").encode("utf-8", "replace")
    crlf = b"\r\n"
    parts = [
        b"--" + boundary.encode() + crlf
        + b'Content-Disposition: form-data; name="payload_json"' + crlf
        + b"Content-Type: application/json" + crlf + crlf
        + payload_json.encode("utf-8") + crlf,
        b"--" + boundary.encode() + crlf
        + b'Content-Disposition: form-data; name="files[0]"; filename="' + safe_name + b'"' + crlf
        + b"Content-Type: video/mp4" + crlf + crlf
        + file_bytes + crlf,
        b"--" + boundary.encode() + b"--" + crlf,
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# --------------------------------------------------------------------------
# Config / logging / notifications
# --------------------------------------------------------------------------

def setup_logging() -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stderr is not None and getattr(sys.stderr, "isatty", lambda: False)():
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        log.addHandler(sh)


_NOTIFY_PS = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.BalloonTipTitle = $env:CW_TITLE
$n.BalloonTipText = $env:CW_MSG
$n.Visible = $true
$n.ShowBalloonTip(8000)
Start-Sleep -Seconds 9
$n.Dispose()
"""


def notify(title: str, message: str) -> None:
    try:
        encoded = base64.b64encode(_NOTIFY_PS.encode("utf-16-le")).decode("ascii")
        env = dict(os.environ, CW_TITLE=title[:60], CW_MSG=message[:250])
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-EncodedCommand", encoded],
            env=env, creationflags=CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 - notifying must never raise
        pass


def load_config(path: Path | None, dry_run: bool) -> Config:
    candidates = [path] if path else [SCRIPT_DIR / "config.json"]
    if dry_run and not (path or candidates[0]).exists():
        candidates = [SCRIPT_DIR / "config.example.json"]
    cfg_path = candidates[0]
    if not cfg_path.exists():
        raise ClipError(f"config file not found: {cfg_path}")
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise ClipError(f"cannot read config {cfg_path}: {e}") from e
    known = Config.__dataclass_fields__
    cfg = Config(**{k: v for k, v in raw.items() if k in known})
    if not cfg.watch_dir:
        # NVIDIA's default save location is the user's Videos folder.
        cfg.watch_dir = str(Path.home() / "Videos")
    return cfg


# --------------------------------------------------------------------------
# ffmpeg
# --------------------------------------------------------------------------

def _exe_beside(ffmpeg: Path, name: str) -> Path:
    return ffmpeg.with_name(name + ffmpeg.suffix)


def find_ffmpeg(configured: str | None) -> tuple[Path, Path] | None:
    candidates: list[Path] = []
    if configured:
        p = Path(configured)
        candidates.append(p / "ffmpeg.exe" if p.is_dir() else p)
    which = shutil.which("ffmpeg")
    if which:
        candidates.append(Path(which))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe")
        pattern = os.path.join(local, "Microsoft", "WinGet", "Packages",
                               "Gyan.FFmpeg*", "**", "bin", "ffmpeg.exe")
        candidates.extend(Path(m) for m in sorted(glob.glob(pattern, recursive=True)))
    for ff in candidates:
        probe = _exe_beside(ff, "ffprobe")
        if ff.is_file() and probe.is_file():
            return ff, probe
    return None


def run_tool(cmd: list[str], low_priority: bool = False,
             timeout: float | None = None) -> subprocess.CompletedProcess:
    flags = CREATE_NO_WINDOW | (BELOW_NORMAL_PRIORITY_CLASS if low_priority else 0)
    return subprocess.run(cmd, capture_output=True, creationflags=flags, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def probe_duration(ffprobe: Path, src: Path) -> float:
    r = run_tool([str(ffprobe), "-v", "error", "-show_entries", "format=duration",
                  "-of", "default=nw=1:nk=1", str(src)], timeout=60)
    try:
        return float(r.stdout.decode().strip())
    except ValueError as e:
        raise ClipError(f"ffprobe could not read duration: {r.stderr.decode(errors='replace')[-300:]}") from e


def probe_height(ffprobe: Path, src: Path) -> int | None:
    r = run_tool([str(ffprobe), "-v", "error", "-select_streams", "v:0",
                  "-show_entries", "stream=height", "-of", "default=nw=1:nk=1", str(src)],
                 timeout=60)
    try:
        return int(r.stdout.decode().split()[0])
    except (ValueError, IndexError):
        return None


def build_encode_cmd(ffmpeg: Path, src: Path, out: Path, start: float, length: float,
                     height: int, fps: int, video_kbps: int, audio_kbps: int,
                     encoder: str) -> list[str]:
    if encoder == "h264_nvenc":
        venc = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr"]
    else:
        venc = ["-c:v", "libx264", "-preset", "veryfast"]
    return [
        str(ffmpeg), "-hide_banner", "-y", "-ss", f"{start:.3f}", "-i", str(src),
        "-t", f"{length:.3f}", "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", f"scale=-2:{height},fps={fps}",
        *venc, "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k",
        "-bufsize", f"{2 * video_kbps}k",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ac", "2",
        "-movflags", "+faststart", str(out),
    ]


def encode(tools: tuple[Path, Path], src: Path, out: Path, start: float, length: float,
           height: int, cfg: Config, video_kbps: int, encoder: str) -> str:
    """Encode; returns the encoder actually used (falls back from nvenc to x264 once)."""
    ffmpeg = tools[0]
    order = [encoder] if encoder != "h264_nvenc" else ["h264_nvenc", "libx264"]
    last_err = ""
    for enc in order:
        cmd = build_encode_cmd(ffmpeg, src, out, start, length, height, cfg.fps,
                               video_kbps, cfg.audio_kbps, enc)
        r = run_tool(cmd, low_priority=True)
        if r.returncode == 0 and out.exists():
            return enc
        last_err = r.stderr.decode(errors="replace")[-1500:]
        log.error("ffmpeg (%s) failed with exit %s:\n%s", enc, r.returncode, last_err)
        if enc == "h264_nvenc":
            log.warning("nvenc encode failed, falling back to libx264")
    raise ClipError("ffmpeg encode failed: " + last_err.strip().splitlines()[-1] if last_err.strip()
                    else "ffmpeg encode failed")


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------

def _post_once(url: str, body: bytes, content_type: str) -> tuple[int, dict, dict]:
    req = urllib.request.Request(
        url + "?wait=true", data=body, method="POST",
        headers={"Content-Type": content_type, "User-Agent": "clip-to-discord/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            status, raw, headers = resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        status, raw, headers = e.code, e.read(), dict(e.headers)
    try:
        data = json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except ValueError:
        data = {}
    return status, (data if isinstance(data, dict) else {}), headers


def upload(url: str, body: bytes, content_type: str) -> None:
    """Post the clip. Returns on success; raises TooLargeError or ClipError."""
    net_retries = 0
    rate_retries = 0
    while True:
        try:
            status, data, headers = _post_once(url, body, content_type)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            status, data, headers = 0, {}, {}
            err = f"network error: {type(e).__name__}"
        else:
            err = f"HTTP {status}"
        if status == 200 and data.get("id") and data.get("attachments"):
            return
        if status == 413 or data.get("code") == 40005:
            raise TooLargeError()
        if status == 429 and rate_retries < 5:
            rate_retries += 1
            try:
                wait = float(data.get("retry_after", headers.get("Retry-After", 5)))
            except (TypeError, ValueError):
                wait = 5.0
            log.warning("rate limited, sleeping %.1fs", wait)
            time.sleep(min(max(wait, 0.5), 300))
            continue
        if status == 0 or status >= 500 or status == 200:
            if net_retries < len(RETRY_WAITS):
                wait = RETRY_WAITS[net_retries]
                net_retries += 1
                log.warning("upload failed (%s), retry %d in %ds", err, net_retries, wait)
                time.sleep(wait)
                continue
            raise ClipError(f"upload failed after retries ({err})")
        detail = data.get("message", "")
        raise ClipError(f"upload rejected: HTTP {status} {detail}".strip())


# --------------------------------------------------------------------------
# Clip pipeline
# --------------------------------------------------------------------------

def remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        log.warning("could not delete %s: %s", path, e)


def process_clip(cfg: Config, tools: tuple[Path, Path], src: Path, dry_run: bool) -> bool:
    t0 = time.time()
    watch_dir = Path(cfg.watch_dir)
    game = derive_game_name(src, watch_dir)
    out = TMP_DIR / (src.stem + ".mp4")
    keep_out = dry_run
    try:
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        remove_quietly(out)
        duration = probe_duration(tools[1], src)
        start, length = trim_window(duration, cfg.clip_seconds)
        src_h = probe_height(tools[1], src)
        height = min(cfg.height, src_h) if src_h else cfg.height
        base_kbps = compute_video_kbps(length, cfg.max_upload_mb, cfg.audio_kbps)
        limit = cfg.max_upload_mb * 1024 * 1024
        log.info("Processing %s (%.1fs, game=%s)", src.name, duration, game)

        encoder = "h264_nvenc"
        kbps = base_kbps
        for attempt in range(3):
            encoder = encode(tools, src, out, start, length, height, cfg, int(kbps), encoder)
            if out.stat().st_size <= limit:
                break
            log.warning("output %.2f MB over limit (attempt %d), lowering bitrate",
                        out.stat().st_size / 1048576, attempt + 1)
            kbps *= 0.8
        else:
            raise ClipError("could not get the clip under the upload limit in 3 attempts")
        if encoder != "h264_nvenc":
            log.info("used libx264 fallback encoder")

        if dry_run:
            size_mb = out.stat().st_size / 1048576
            msg = f"DRY RUN output={out} size={size_mb:.2f}MB duration={length:.1f}s encoder={encoder}"
            log.info(msg)
            if sys.stdout is not None:
                print(msg)
            return True

        fname = upload_filename(game, src)
        payload = json.dumps({"content": f"**{game}**", "allowed_mentions": {"parse": []}})
        shrunk = False
        while True:
            body, ctype = build_multipart(payload, fname, out.read_bytes())
            try:
                upload(cfg.webhook_url, body, ctype)
                break
            except TooLargeError:
                if shrunk:
                    raise ClipError("Discord rejected the upload as too large (even after re-encode)")
                shrunk = True
                log.warning("Discord says too large; re-encoding at 0.7x bitrate")
                encoder = encode(tools, src, out, start, length, height, cfg,
                                 int(kbps * 0.7), encoder)

        src_size, up_size = src.stat().st_size, out.stat().st_size
        remove_quietly(out)
        if cfg.delete_original_after_upload:
            os.remove(src)
        log.info("Uploaded game=%s source=%.1fMB upload=%.1fMB took=%.0fs",
                 game, src_size / 1048576, up_size / 1048576, time.time() - t0)
        return True
    except Exception as e:  # noqa: BLE001 - per-clip isolation
        log.exception("Failed to process %s: %s", src, e)
        notify("Clip upload failed", f"{src.name}: {e}")
        return False
    finally:
        if not keep_out:
            remove_quietly(out)


# --------------------------------------------------------------------------
# Watching
# --------------------------------------------------------------------------

def scan(watch_dir: str, suffix: str) -> set[str]:
    suffix = suffix.lower()
    found: set[str] = set()

    def walk(d: str) -> None:
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            walk(entry.path)
                        elif entry.name.lower().endswith(suffix):
                            found.add(entry.path)
                    except OSError:
                        continue
        except OSError:
            return

    walk(watch_dir)
    return found


def wait_until_stable(path: str, timeout: float = 120) -> bool:
    deadline = time.time() + timeout
    last = -1
    stable = 0
    while time.time() < deadline:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = -1
        if size > 0 and size == last:
            stable += 1
        else:
            stable = 0
        last = size
        if stable >= 3:
            try:
                with open(path, "rb"):
                    return True
            except OSError:
                stable = 0
        time.sleep(1)
    return False


def acquire_lock() -> socket.socket | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        s.close()
        return None
    return s


def watch(cfg: Config, tools: tuple[Path, Path]) -> None:
    seen = scan(cfg.watch_dir, cfg.filename_suffix)
    log.info("Watching %s (%d existing clips ignored)", cfg.watch_dir, len(seen))
    while True:
        try:
            new = sorted(scan(cfg.watch_dir, cfg.filename_suffix) - seen)
            for path in new:
                seen.add(path)
                if not wait_until_stable(path):
                    log.error("Timed out waiting for %s to finish writing", path)
                    notify("Clip not processed", f"Timed out waiting for {Path(path).name}")
                    continue
                process_clip(cfg, tools, Path(path), dry_run=False)
        except Exception:  # noqa: BLE001 - never kill the loop
            log.exception("Error in watch loop")
        time.sleep(cfg.poll_seconds)


def fail(msg: str) -> int:
    log.error(msg)
    notify("Clip watcher", msg)
    if sys.stderr is not None:
        print(msg, file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", metavar="FILE", help="process one file and exit")
    ap.add_argument("--dry-run", action="store_true", help="no upload, no deletion")
    ap.add_argument("--config", metavar="PATH", help="alternative config file")
    args = ap.parse_args()

    setup_logging()
    try:
        cfg = load_config(Path(args.config) if args.config else None, args.dry_run)
    except ClipError as e:
        return fail(str(e))
    if not args.dry_run and not webhook_is_valid(cfg.webhook_url):
        return fail("webhook_url is missing, a placeholder, or not a Discord webhook URL")
    tools = find_ffmpeg(cfg.ffmpeg_path)
    if tools is None:
        return fail("ffmpeg/ffprobe not found (set ffmpeg_path in config.json)")

    if args.once:
        src = Path(args.once)
        if not src.is_file():
            return fail(f"file not found: {src}")
        return 0 if process_clip(cfg, tools, src, args.dry_run) else 1

    lock = acquire_lock()
    if lock is None:
        return 0
    if not Path(cfg.watch_dir).is_dir():
        return fail(f"watch_dir does not exist: {cfg.watch_dir}")
    watch(cfg, tools)
    return 0


if __name__ == "__main__":
    sys.exit(main())
