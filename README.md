# Clip to Discord

Save a clip in any game with NVIDIA Instant Replay or Medal, and the last 30 seconds land in a Discord channel a few seconds later.

A small app sits near the clock and watches the folders where NVIDIA Instant Replay and Medal save clips. For each new clip it:

1. trims it to the last 30 seconds,
2. shrinks it to fit Discord's upload limit (10 MB by default),
3. posts it to your channel, with the game name as the message,
4. for NVIDIA clips, deletes the original once Discord confirms the upload (you can turn this off). Medal clips are always kept.

NVIDIA manual recordings and screenshots are never posted, only Instant Replay saves.

## Install

You need Windows 10 or 11 and at least one of these to record clips:

- an NVIDIA graphics card with the NVIDIA app or GeForce Experience (for Instant Replay), or
- [Medal](https://medal.tv/).

1. **Download** `ClipToDiscord.exe` from the [latest release](https://github.com/odehamer/clip-to-discord/releases/latest).
2. **Run it.** Windows may show a blue "Windows protected your PC" notice, because the app isn't code-signed. Click **More info**, then **Run anyway**.
3. **Make a webhook in Discord.** Open your channel's settings, then **Integrations → Webhooks → New Webhook → Copy Webhook URL**.
4. **Paste the URL** into the window and click **Send test message** to check it.
5. **Tick where your clips come from:** NVIDIA Instant Replay, Medal, or both. The app pre-ticks whichever it finds on your PC.
6. Click **Save**. The first time, the app downloads the video tools it needs (about 110 MB).

Then save a clip in a game and check your channel:

- **NVIDIA:** press **Alt+Z** in a game, switch Instant Replay on, then use your save hotkey (**Alt+F10** by default).
- **Medal:** use your Medal clip hotkey as usual.

After the first run you can delete the downloaded file. The app keeps its own copy and starts with Windows.

### Good to know

- **Treat the webhook URL like a password.** Anyone who has it can post to your channel.
- **NVIDIA originals are deleted by default.** With that box ticked, the Discord copy becomes the only copy of each NVIDIA clip. Untick it in Settings to keep your originals. A clip that fails to upload is always kept, and Medal clips are never deleted.
- **Every new clip is posted.** That means every Instant Replay save and every new video in Medal's `Clips` folder, including Medal's automatic clips. Use **Pause posting** in the tray menu when you want to save a clip privately.
- **Medal's folder.** The app looks for `C:\Medal\Clips`. If you moved Medal's capture folder (Medal: Settings → PC Storage), use **Browse…** in the setup window to pick the `Clips` folder inside it.
- **Microphone.** Only the first audio track is posted. If NVIDIA is set to mix your mic into the same track as the game, your voice is in the clip. Choose separate tracks, or turn the mic off, in the overlay's audio settings.
- **Quality.** 30 seconds in 10 MB looks soft in fast motion. Boosted servers allow bigger uploads; see `max_upload_mb` below.

### The tray icon

Right-click the icon near the clock:

| Menu item | What it does |
|---|---|
| Pause posting | Stops posting until you untick it. Clips saved while paused are never posted. |
| Settings… | Reopens the setup window. |
| Open log | Shows what the app has done, and any errors. |
| Update available… | Appears when a newer version exists; opens the download page. |
| Remove from this PC… | Uninstalls the app, its settings and its video tools. Your clips are not touched. |
| Quit | Closes the app until you start it again. |

### Updating

Download the new `ClipToDiscord.exe` and run it. It replaces the installed copy and keeps your settings.

## Troubleshooting

| Problem | What to check |
|---|---|
| Nothing is posted | Is the icon near the clock, and not paused? Is the right capture app ticked in Settings? For NVIDIA, is Instant Replay on in the overlay? For Medal, is the clips folder in Settings the one Medal uses? Open the log from the tray menu. |
| "Discord said no (HTTP 401 or 404)" | The webhook was deleted or the URL is wrong. Make a new one and paste it in Settings. |
| The video tools download fails | Check your internet connection and click Retry. |
| The game stutters after a clip | The graphics-card encoder wasn't available and the slower fallback was used; the log says so. Update your NVIDIA driver. |
| Antivirus flags the file | Single-file Python apps are sometimes flagged by mistake. Every release is built by GitHub from this source, and the release notes list the file's SHA-256. |

## Advanced settings

The setup window covers the usual options. Others live in `%LOCALAPPDATA%\ClipToDiscord\config.json`; quit the app, edit the file, and start it again.

| Key | Default | Meaning |
|---|---|---|
| `clip_seconds` | `30` | How many seconds from the end of each save to post. |
| `max_upload_mb` | `10` | Upload size target. Use `50` for a level 2 boosted server, `100` for level 3. |
| `height` | `720` | Output height in pixels. Clips are never upscaled. |
| `fps` | `60` | Output frame rate. |
| `audio_kbps` | `96` | Audio bitrate. |
| `watch_dir` | `""` | Folder NVIDIA saves clips to. Empty means your `Videos` folder, NVIDIA's default. |
| `filename_suffix` | `".DVR.mp4"` | What an Instant Replay filename ends with. |
| `ffmpeg_path` | `null` | Use your own ffmpeg instead of the downloaded one. |
| `nvidia_enabled` | `true` | Watch for NVIDIA Instant Replay saves. Set in the setup window. |
| `medal_enabled` | `false` | Watch Medal's clips folder. Set in the setup window. |
| `medal_dir` | `""` | Medal's `Clips` folder. Empty means look in the usual places. |
| `medal_delete_original` | `false` | Delete Medal's original after a confirmed upload. This leaves a missing clip inside Medal's library. |

## Running from source

The app is plain Python. `clip_watcher.py` is the engine and has no dependencies beyond Python 3.11+ and ffmpeg; `app.py` adds the window and tray icon.

Script only, no window:

```
copy config.example.json config.json
pythonw clip_watcher.py
```

Put your webhook URL in `config.json` first. `python clip_watcher.py --once "clip.DVR.mp4" --dry-run` processes one file without uploading or deleting anything.

The full app, and building the `.exe`:

```
pip install -r requirements.txt
python app.py
build.bat
```

Tests: `python -m unittest discover -s tests`.

Releases are built by GitHub Actions: pushing a tag such as `v1.0.1` (matching `__version__` in `app.py`) builds `ClipToDiscord.exe` and attaches it to a release.
