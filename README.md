# clip-to-discord

Press your NVIDIA Instant Replay hotkey in any game, and the last 30 seconds land in a Discord channel a few seconds later.

A small background script watches the folder where NVIDIA saves Instant Replay clips. For each new clip it:

1. trims it to the last 30 seconds,
2. re-encodes it on the GPU to fit Discord's upload limit (10 MB by default),
3. posts it to a channel through a webhook, with the game name as the message,
4. deletes the original clip once Discord confirms the upload (optional).

Manual recordings and screenshots are never posted, only Instant Replay saves.

## Requirements

- Windows 10 or 11
- An NVIDIA GPU with the NVIDIA app or GeForce Experience (for Instant Replay)
- [Python 3.11+](https://www.python.org/downloads/) (no extra packages needed)
- [ffmpeg](https://ffmpeg.org/)
- A Discord server where you can create webhooks

## Setup

### 1. Install ffmpeg

```
winget install Gyan.FFmpeg
```

The script finds a winget-installed ffmpeg on its own. If you installed it another way and it isn't on your `PATH`, set `ffmpeg_path` in the config.

### 2. Get the code

```
git clone https://github.com/YOUR_USERNAME/clip-to-discord.git
cd clip-to-discord
```

### 3. Create a Discord webhook

In Discord: open the channel's settings, then **Integrations → Webhooks → New Webhook → Copy Webhook URL**.

Treat the URL like a password. Anyone who has it can post to your channel.

### 4. Create your config

Copy `config.example.json` to `config.json` and paste your webhook URL into `webhook_url`. `config.json` is in `.gitignore`, so it won't be committed.

### 5. Turn on Instant Replay

Open the NVIDIA overlay (**Alt+Z**), switch **Instant Replay** on, and set its length to at least 30 seconds. In the overlay's settings you can also change the save hotkey (default **Alt+F10**).

### 6. Check the encode works (optional)

This processes one existing clip without uploading or deleting anything:

```
python clip_watcher.py --once "C:\path\to\some clip.DVR.mp4" --dry-run
```

The result is written to the `tmp` folder.

### 7. Start the watcher

```
pythonw clip_watcher.py
```

`pythonw` runs it with no console window. Now save an Instant Replay clip in a game and check your channel.

### 8. Start it with Windows (optional)

Press **Win+R**, enter `shell:startup`, and create a shortcut in that folder with this target (adjust the path):

```
pythonw.exe "C:\path\to\clip-to-discord\clip_watcher.py"
```

## Configuration

All settings live in `config.json`.

| Key | Default | Meaning |
|---|---|---|
| `webhook_url` | | Your Discord webhook URL. Required. |
| `watch_dir` | `""` | Folder NVIDIA saves clips to. Empty means your `Videos` folder, which is NVIDIA's default. |
| `clip_seconds` | `30` | How many seconds from the end of each save to post. |
| `max_upload_mb` | `10` | Upload size target. Use `50` for a level 2 boosted server, `100` for level 3. |
| `height` | `720` | Output height in pixels. Clips are never upscaled. |
| `fps` | `60` | Output frame rate. |
| `audio_kbps` | `96` | Audio bitrate. |
| `delete_original_after_upload` | `true` | Permanently delete NVIDIA's original after a confirmed upload. Set to `false` to keep it. |
| `poll_seconds` | `2` | How often the folder is checked for new clips. |
| `filename_suffix` | `".DVR.mp4"` | What an Instant Replay filename ends with. |
| `ffmpeg_path` | `null` | Folder or full path of ffmpeg, if it can't be found automatically. |

Restart the watcher after changing the config.

## Good to know

- **Originals are deleted by default.** With `delete_original_after_upload` on, the compressed Discord copy becomes the only copy of each clip. A clip whose upload fails is always kept.
- **Microphone.** Only the first audio track is posted. If NVIDIA is set to mix your mic into the same track as the game, your voice is in the clip. Choose separate tracks, or turn the mic off, in the overlay's audio settings.
- **Quality.** 30 seconds in 10 MB is about 2.5 Mbps, so fast motion looks soft. A boosted server with a higher `max_upload_mb` looks much better.
- **Clips saved before the watcher started are ignored**, so nothing old is posted when it launches.

## Troubleshooting

Everything is logged to `clip_watcher.log` next to the script. A failed upload also shows a Windows notification.

| Problem | Likely cause |
|---|---|
| Nothing is posted | The watcher isn't running, Instant Replay is off, or your clips don't end in `.DVR.mp4` (check a real filename and adjust `filename_suffix`). |
| "webhook_url is missing…" | `config.json` doesn't exist or still has the placeholder. |
| "ffmpeg/ffprobe not found" | Install ffmpeg or set `ffmpeg_path`. |
| HTTP 401 or 404 in the log | The webhook was deleted or the URL is wrong. |
| Game stutters after a clip | The GPU encoder failed and the CPU fallback was used; the log says so. Update your NVIDIA driver. |

To stop the watcher, end the `pythonw.exe` process in Task Manager.
