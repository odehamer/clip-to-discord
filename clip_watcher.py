"""Watch NVIDIA Instant Replay and Medal saves, trim, re-encode and post them to a Discord webhook.

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
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
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
_logging_ready = False


def set_paths(base_dir: Path) -> None:
    """Move the tmp folder and log file under base_dir (used by the GUI app)."""
    global TMP_DIR, LOG_PATH
    base_dir = Path(base_dir)
    TMP_DIR = base_dir / "tmp"
    LOG_PATH = base_dir / "clip_watcher.log"


class ClipError(Exception):
    """A clip could not be processed; the message is the reason."""


class TooLargeError(Exception):
    """Discord rejected the upload as too large."""


@dataclass
class Config:
    webhook_url: str = ""          # old single-webhook key; see get_destinations
    destinations: list | None = None   # None = key absent; a list (even empty) is authoritative
    player_name: str = ""
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
    nvidia_enabled: bool = True
    medal_enabled: bool = False
    medal_dir: str = ""
    medal_delete_original: bool = False


@dataclass(frozen=True)
class Source:
    """One place clips are saved to: a folder, the file suffix to look for, a deletion rule."""
    name: str
    dir: Path
    suffix: str
    delete_original: bool


@dataclass(frozen=True)
class Destination:
    """One Discord channel clips are posted to: a name, its webhook URL and an on/off state."""
    name: str
    url: str
    enabled: bool = True


@dataclass(frozen=True)
class PostResult:
    """What happened when posting one clip to one destination."""
    name: str
    ok: bool
    reason: str = ""


class UploadError(ClipError):
    """A post failed; `reason` is a short, URL-free explanation for the user."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# --------------------------------------------------------------------------
# Pure logic
# --------------------------------------------------------------------------

def webhook_is_valid(url: str) -> bool:
    return bool(url) and PLACEHOLDER_MARKER not in url and url.startswith(WEBHOOK_PREFIXES)


PLAYER_NAME_MAX = 32
_BANNED_NAME_PARTS = ("discord", "clyde")


def validate_player_name(name: str) -> str | None:
    """A short user-facing error, or None when the player name is acceptable (empty is fine)."""
    name = (name or "").strip()
    if len(name) > PLAYER_NAME_MAX:
        return f"Keep your name to {PLAYER_NAME_MAX} characters or fewer."
    lowered = name.lower()
    if any(part in lowered for part in _BANNED_NAME_PARTS):
        return 'Discord does not allow names that contain "discord" or "clyde".'
    return None


def effective_player_name(cfg: "Config") -> str:
    """The player name to send, or "" when empty or one Discord would reject."""
    name = str(cfg.player_name or "").strip()
    if name and validate_player_name(name):
        log.warning("player_name is not allowed by Discord; posting under the webhook's name")
        return ""
    return name


def build_payload(content: str, player_name: str = "") -> dict:
    """The Discord JSON payload: never pings anyone; `username` only when a name is set."""
    payload: dict = {"content": content, "allowed_mentions": {"parse": []}}
    name = (player_name or "").strip()
    if name:
        payload["username"] = name
    return payload


def get_destinations(cfg: "Config | dict") -> list[Destination]:
    """Destinations from a Config (or raw config dict).

    A `destinations` list is authoritative (even when empty); entries with an invalid URL are
    dropped. Without the key, a valid old-style `webhook_url` becomes one "My server" destination.
    """
    if isinstance(cfg, dict):
        raw, legacy = cfg.get("destinations"), cfg.get("webhook_url", "")
    else:
        raw, legacy = cfg.destinations, cfg.webhook_url
    if isinstance(raw, list):
        out: list[Destination] = []
        for i, item in enumerate(raw, 1):
            if not isinstance(item, dict):
                log.warning("Ignoring destination %d: not a valid entry", i)
                continue
            name = str(item.get("name") or "").strip() or f"Destination {i}"
            url = str(item.get("url") or "").strip()
            if not webhook_is_valid(url):
                log.warning("Ignoring destination %r: it has no valid Discord webhook URL", name)
                continue
            out.append(Destination(name, url, bool(item.get("enabled", True))))
        return out
    legacy = str(legacy or "").strip()
    return [Destination("My server", legacy, True)] if webhook_is_valid(legacy) else []


def enabled_destinations(cfg: "Config | dict") -> list[Destination]:
    return [d for d in get_destinations(cfg) if d.enabled]


def all_posted(results: list[PostResult]) -> bool:
    """True only when there was something to post to and every destination succeeded."""
    return bool(results) and all(r.ok for r in results)


def failure_notice(results: list[PostResult]) -> tuple[str, str] | None:
    """(title, message) for the one notification about failed destinations, or None."""
    failed = [r for r in results if not r.ok]
    if not failed:
        return None
    parts = [f"Not posted to {r.name}: {(r.reason or 'unknown problem').rstrip('. ')}."
             for r in failed]
    return "Clip not posted everywhere", " ".join(parts)


def compute_video_kbps(length: float, max_upload_mb: float, audio_kbps: int) -> int:
    budget_bytes = max_upload_mb * 1024 * 1024 * 0.92
    total_kbps = budget_bytes * 8 / max(length, 0.1) / 1000
    return int(max(300, total_kbps - audio_kbps))


def trim_window(duration: float, clip_seconds: float) -> tuple[float, float]:
    return max(0.0, duration - clip_seconds), min(clip_seconds, duration)


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "clip"


_MEDAL_PREFIX_RE = re.compile(r"^medaltv", re.IGNORECASE)
_MEDAL_TRAIL_RE = re.compile(r"[\d\s\-_.]+$")
_MEDAL_LEAD_RE = re.compile(r"^[\s\-_.]+")
_DVR_TAIL_RE = re.compile(r"\.dvr$", re.IGNORECASE)


def game_from_medal_name(stem: str) -> str | None:
    """Best-effort game name from a Medal-style file name (without extension), or None."""
    name = _MEDAL_PREFIX_RE.sub("", stem.strip())
    name = _MEDAL_LEAD_RE.sub("", name)
    name = _MEDAL_TRAIL_RE.sub("", name)
    if sum(ch.isalpha() for ch in name) >= 2:
        return name
    return None


def derive_game_name(path: Path, base_dir: Path) -> str:
    parent = path.parent
    try:
        same = parent.resolve() == base_dir.resolve()
    except OSError:
        same = parent == base_dir
    if not same and parent.name:
        return parent.name
    m = DATE_RE.search(path.name)
    if m:
        text = path.name[: m.start()].strip(" -_.")
        if text:
            return text
    else:
        guess = game_from_medal_name(_DVR_TAIL_RE.sub("", path.stem))
        if guess:
            return guess
    return "Clip"


def upload_filename(game: str, path: Path) -> str:
    m = DATE_RE.search(path.name)
    if m:
        stamp = f"{m[1]}-{m[2]}-{m[3]} {m[4]}.{m[5]}"
    else:
        try:
            stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H.%M")
        except (OSError, ValueError, OverflowError):
            stamp = ""
    return sanitize_filename(f"{game} {stamp}".strip()) + ".mp4"


def detect_medal_dir(candidates: list[Path] | tuple[Path, ...] | None = None) -> str | None:
    """First existing Medal clips folder among the usual places, else None."""
    if candidates is None:
        home = Path.home()
        candidates = [Path(r"C:\Medal\Clips"), home / "Videos" / "Medal" / "Clips",
                      home / "Medal" / "Clips"]
    for c in candidates:
        try:
            if Path(c).is_dir():
                return str(c)
        except OSError:
            continue
    return None


def get_sources(cfg: Config) -> list[Source]:
    sources: list[Source] = []
    if cfg.nvidia_enabled:
        sources.append(Source("NVIDIA", Path(cfg.watch_dir), cfg.filename_suffix,
                              cfg.delete_original_after_upload))
    if cfg.medal_enabled:
        medal = cfg.medal_dir or detect_medal_dir()
        if medal:
            sources.append(Source("Medal", Path(medal), ".mp4", cfg.medal_delete_original))
        else:
            log.warning("Medal is enabled but its clips folder could not be found; "
                        "set medal_dir to use it")
    return sources


def should_delete_original(cfg: Config, source: Source | None) -> bool:
    return cfg.delete_original_after_upload if source is None else source.delete_original


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
    global _logging_ready
    if _logging_ready:
        return
    _logging_ready = True
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
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


def find_ffmpeg(configured: str | None,
                extra_dirs: list[Path] | tuple[Path, ...] = ()) -> tuple[Path, Path] | None:
    candidates: list[Path] = []
    if configured:
        p = Path(configured)
        candidates.append(p / "ffmpeg.exe" if p.is_dir() else p)
    for d in extra_dirs:
        candidates.append(Path(d) / "ffmpeg.exe")
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

def open_url(req: urllib.request.Request, timeout: float):
    """Open a request. The GUI app replaces this to add a fallback certificate store."""
    return urllib.request.urlopen(req, timeout=timeout)


def post_text(webhook_url: str, text: str, username: str = "") -> None:
    """Post a plain text message. Raises ClipError (never containing the URL) on failure."""
    body = json.dumps(build_payload(text, username)).encode("utf-8")
    req = urllib.request.Request(
        webhook_url + "?wait=true", data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "clip-to-discord/1.0"})
    try:
        with open_url(req, 20) as resp:
            resp.read()
        return
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8", "replace"))
            detail = data.get("message", "") if isinstance(data, dict) else ""
        except ValueError:
            detail = ""
        raise ClipError(f"Discord said no (HTTP {e.code}). {detail}".strip()) from None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise ClipError(f"Could not reach Discord ({type(e).__name__}). Check your internet connection.") from None


def _post_once(url: str, body: bytes, content_type: str) -> tuple[int, dict, dict]:
    req = urllib.request.Request(
        url + "?wait=true", data=body, method="POST",
        headers={"Content-Type": content_type, "User-Agent": "clip-to-discord/1.0"})
    try:
        with open_url(req, 120) as resp:
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
            reason = ("Could not reach Discord" if status == 0
                      else f"Discord kept failing (HTTP {status})")
            raise UploadError(f"upload failed after retries ({err})", reason)
        detail = data.get("message", "")
        raise UploadError(f"upload rejected: HTTP {status} {detail}".strip(),
                          f"Discord said no (HTTP {status})")


# --------------------------------------------------------------------------
# Clip pipeline
# --------------------------------------------------------------------------

def remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        log.warning("could not delete %s: %s", path, e)


def process_clip(cfg: Config, tools: tuple[Path, Path], src: Path, dry_run: bool,
                 source: Source | None = None) -> bool:
    t0 = time.time()
    base_dir = Path(cfg.watch_dir) if source is None else source.dir
    source_name = "NVIDIA" if source is None else source.name
    game = derive_game_name(src, base_dir)
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
        log.info("Processing %s clip %s (%.1fs, game=%s)", source_name, src.name, duration, game)

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

        dests = enabled_destinations(cfg)
        if not dests:
            raise ClipError("no destination is switched on")
        fname = upload_filename(game, src)
        payload = json.dumps(build_payload(f"**{game}**", effective_player_name(cfg)))
        results: list[PostResult] = []
        shrunk = False  # at most one re-encode per clip; later destinations get the smaller file
        for dest in dests:
            reason = ""
            while True:
                body, ctype = build_multipart(payload, fname, out.read_bytes())
                try:
                    upload(dest.url, body, ctype)
                    break
                except TooLargeError:
                    if shrunk:
                        reason = "Discord said the clip is too large"
                        break
                    shrunk = True
                    log.warning("Discord says too large; re-encoding at 0.7x bitrate")
                    encoder = encode(tools, src, out, start, length, height, cfg,
                                     int(kbps * 0.7), encoder)
                except ClipError as e:
                    reason = getattr(e, "reason", "") or str(e) or "unknown problem"
                    break
            results.append(PostResult(dest.name, not reason, reason))
            log.info("Post to %s: %s", dest.name, "ok" if not reason else f"failed ({reason})")

        src_size, up_size = src.stat().st_size, out.stat().st_size
        remove_quietly(out)
        if not all_posted(results):
            log.error("Clip %s was not posted everywhere; the original is kept", src.name)
            notify(*failure_notice(results))
            return False
        if should_delete_original(cfg, source):
            os.remove(src)
        log.info("Uploaded %s game=%s source=%.1fMB upload=%.1fMB destinations=%d took=%.0fs",
                 source_name, game, src_size / 1048576, up_size / 1048576, len(results),
                 time.time() - t0)
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


def watch(cfg: Config, tools: tuple[Path, Path],
          stop: threading.Event | None = None,
          paused: threading.Event | None = None) -> None:
    sources = get_sources(cfg)
    seen: set[str] = set()
    ready: set[str] = set()      # sources whose folder has been scanned once (baseline taken)
    missing_logged: set[str] = set()

    def poll_source(src: Source) -> list[tuple[Source, str]]:
        """New files of one source, in order. Takes the baseline on the first good scan."""
        if not src.dir.is_dir():
            if src.name not in missing_logged:
                missing_logged.add(src.name)
                log.warning("%s folder not found: %s (will keep checking)", src.name, src.dir)
            return []
        current = scan(str(src.dir), src.suffix)
        if src.name not in ready:
            ready.add(src.name)
            seen.update(current)
            log.info("Watching %s %s (%d existing clips ignored)", src.name, src.dir, len(current))
            return []
        new = sorted(current - seen)
        seen.update(new)  # the first source to list a path owns it
        return [(src, p) for p in new]

    for src in sources:
        poll_source(src)
    if not sources:
        log.warning("No clip source is enabled; nothing to watch")
    while not (stop and stop.is_set()):
        try:
            found: list[tuple[Source, str]] = []
            for src in sources:
                found.extend(poll_source(src))
            # clips saved while paused are already in seen and are skipped for good
            for src, path in found:
                if stop and stop.is_set():
                    return
                if paused is not None and paused.is_set():
                    continue
                if not wait_until_stable(path):
                    log.error("Timed out waiting for %s to finish writing", path)
                    notify("Clip not processed", f"Timed out waiting for {Path(path).name}")
                    continue
                process_clip(cfg, tools, Path(path), dry_run=False, source=src)
        except Exception:  # noqa: BLE001 - never kill the loop
            log.exception("Error in watch loop")
        if stop is not None:
            if stop.wait(cfg.poll_seconds):
                return
        else:
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
    if not args.dry_run and not enabled_destinations(cfg):
        return fail("no destination is switched on: add an enabled entry with a Discord webhook "
                    "URL to destinations (or set webhook_url) in the config file")
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
    sources = get_sources(cfg)
    if not sources:
        return fail("no clip source is enabled (check nvidia_enabled, medal_enabled and medal_dir)")
    if not any(s.dir.is_dir() for s in sources):
        return fail("no clips folder exists: " + ", ".join(f"{s.name}: {s.dir}" for s in sources))
    watch(cfg, tools)
    return 0


if __name__ == "__main__":
    sys.exit(main())
