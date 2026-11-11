# Clip to Discord

Save a clip in any game with NVIDIA Instant Replay or Medal, and it lands in your Discord channels a few seconds later.

A small app sits near the clock and watches the folders where NVIDIA Instant Replay and Medal save clips. For each new clip it:

1. trims it to the last 30 seconds (you can choose 15 to 90),
2. shrinks it to fit Discord's upload limit for your server,
3. posts it to every channel you've switched on, with the game name as the message and your name as the sender,
4. for NVIDIA clips, deletes the original once every channel has it (you can turn this off). Medal clips are always kept.

NVIDIA manual recordings and screenshots are never posted, only Instant Replay saves.

## Install

You need Windows 10 or 11 and at least one of these to record clips:

- an NVIDIA graphics card with the NVIDIA app or GeForce Experience (for Instant Replay), or
- [Medal](https://medal.tv/).

1. **Download** `ClipToDiscord.exe` from the [latest release](https://github.com/odehamer/clip-to-discord/releases/latest).
2. **Run it.** Windows may show a blue "Windows protected your PC" notice, because the app isn't code-signed. Click **More info**, then **Run anyway**.
3. **Make a webhook in Discord.** Open your channel's settings, then **Integrations → Webhooks → New Webhook → Copy Webhook URL**.
4. **Add it in the app.** Click **Add destination…**, give it a name you'll recognise (like "Friends #clips"), paste the URL, and click **Test** to check it.
5. **Type your name** if you want your clips to show it as the sender.
6. **Tick where your clips come from:** NVIDIA Instant Replay, Medal, or both. The app pre-ticks whichever it finds on your PC.
7. Click **Save**. The first time, the app downloads the video tools it needs (about 110 MB).

Then save a clip in a game and check your channel:

- **NVIDIA:** press **Alt+Z** in a game, switch Instant Replay on, then use your save hotkey (**Alt+F10** by default).
- **Medal:** use your Medal clip hotkey as usual.

After the first run you can delete the downloaded file. The app keeps its own copy and starts with Windows.

## The Settings window

Open it from the tray icon (**Settings…**). Changes apply as soon as you click **Save**.

### Setup tab

- **Your name.** Shown as the sender of your clips, up to 32 characters. Leave it empty to use the webhook's own name. Discord doesn't allow names containing "discord" or "clyde".
- **Where clips are posted.** Each destination is one Discord channel. Add as many as you like, switch each on or off, and use **Test** to send a test message. Every clip goes to every destination that's switched on.
- **Where your clips come from.** NVIDIA Instant Replay, Medal, or both, plus Medal's clips folder.
- **Start with Windows** and **Delete the original clip after it's posted**.

### Clips tab

| Setting | Choices |
|---|---|
| Clip length | 15, 30, 60 or 90 seconds, taken from the end of each save |
| Server boost level | None (10 MB), Level 2 (50 MB) or Level 3 (100 MB) |
| Resolution | 720p or 1080p |
| Frame rate | 30 or 60 fps |

The tab warns you when a combination will look blocky, such as 90 seconds or 1080p on an unboosted server. If your destinations are on servers with different boost levels, choose the lowest one.

## Good to know

- **Treat webhook URLs like passwords.** Anyone who has one can post to that channel.
- **NVIDIA originals are deleted by default.** With that box ticked, the Discord copy becomes the only copy of each NVIDIA clip. A clip that didn't reach every destination is always kept, and Medal clips are never deleted.
- **Every new clip is posted.** That means every Instant Replay save and every new video in Medal's `Clips` folder, including Medal's automatic clips. Use **Pause posting** in the tray menu when you want to save a clip privately.
- **Clip length and Instant Replay.** An NVIDIA clip can't be longer than the Instant Replay length set in the NVIDIA overlay.
- **Medal's folder.** The app looks for `C:\Medal\Clips`. If you moved Medal's capture folder (Medal: Settings → PC Storage), use **Browse…** in the Settings window to pick the `Clips` folder inside it.
- **Want the Medal link instead of the video?** Clip to Discord always posts the video file, because Medal offers no way for other apps to look up a clip's link. Medal's own Discord bot, MedalBot, posts Medal links: in Medal, go to **Settings → Connected Accounts → Medal Discord Bot** ([Medal's guide](https://support.medal.tv/support/solutions/articles/48001147847-how-to-setup-medalbot-for-discord-clip-sharing)). Its automatic sharing only covers the server owner's clips; anyone else shares from Medal with **Share → Send to Discord**. If you use MedalBot, untick Medal in Clip to Discord so clips aren't posted twice.
- **Microphone.** Only the first audio track is posted. If NVIDIA is set to mix your mic into the same track as the game, your voice is in the clip. Choose separate tracks, or turn the mic off, in the overlay's audio settings.

## The tray icon

Right-click the icon near the clock:

| Menu item | What it does |
|---|---|
| Pause posting | Stops posting until you untick it. Clips saved while paused are never posted. |
| Post to | Switch each destination on or off. With all of them off, nothing is posted and clips saved meanwhile are skipped. |
| Settings… | Opens the Settings window. |
| Open log | Shows what the app has done, and any errors. |
| Update available… | Appears when a newer version exists; opens the download page. |
| Remove from this PC… | Uninstalls the app, its settings and its video tools. Your clips are not touched. |
| Quit | Closes the app until you start it again. |

## Updating

Download the new `ClipToDiscord.exe` and run it. It replaces the installed copy and keeps your settings. If you're coming from version 1.1 or earlier, your webhook becomes a destination called "My server".

## Troubleshooting

| Problem | What to check |
|---|---|
| Nothing is posted | Is the icon near the clock, and not paused? Is at least one destination switched on under **Post to**? Is the right capture app ticked in Settings? For NVIDIA, is Instant Replay on in the overlay? Open the log from the tray menu. |
| "Clip not posted everywhere" | One destination refused the clip; the notification names it. HTTP 401 or 404 means its webhook was deleted or the URL is wrong: edit the destination and paste a new one. "Too large" means that server has a lower boost level than the Clips tab says. |
| The video tools download fails | Check your internet connection and click Retry. |
| The game stutters after a clip | The graphics-card encoder wasn't available and the slower fallback was used; the log says so. Update your NVIDIA driver. |
| Antivirus flags the file | Single-file Python apps are sometimes flagged by mistake. Every release is built by GitHub from this source, and the release notes list the file's SHA-256. |

## Advanced settings

The Settings window covers the usual options. A few more live in `%LOCALAPPDATA%\ClipToDiscord\config.json`; quit the app, edit the file, and start it again.

| Key | Default | Meaning |
|---|---|---|
| `audio_kbps` | `96` | Audio bitrate. |
| `watch_dir` | `""` | Folder NVIDIA saves clips to. Empty means your `Videos` folder, NVIDIA's default. |
| `filename_suffix` | `".DVR.mp4"` | What an Instant Replay filename ends with. |
| `ffmpeg_path` | `null` | Use your own ffmpeg instead of the downloaded one. |
| `medal_delete_original` | `false` | Delete Medal's original after a confirmed upload. This leaves a missing clip inside Medal's library. |

The file also holds everything the Settings window sets: `player_name`, `destinations` (a list of `name`, `url` and `enabled`), `clip_seconds`, `max_upload_mb`, `height`, `fps`, `nvidia_enabled`, `medal_enabled`, `medal_dir` and `delete_original_after_upload`.

## Running from source

The app is plain Python. `clip_watcher.py` is the engine and has no dependencies beyond Python 3.11+ and ffmpeg; `app.py` adds the window and tray icon. `CONTEXT.md` defines the words the code and the app use.

Script only, no window:

```
copy config.example.json config.json
pythonw clip_watcher.py
```

Put your webhook URL in `config.json` first (`webhook_url` for one channel, or a `destinations` list). `python clip_watcher.py --once "clip.DVR.mp4" --dry-run` processes one file without uploading or deleting anything.

The full app, and building the `.exe`:

```
pip install -r requirements.txt
python app.py
build.bat
```

Tests: `python -m unittest discover -s tests`.

Releases are built by GitHub Actions: pushing a tag such as `v1.2.1` (matching `__version__` in `app.py`) builds `ClipToDiscord.exe` and attaches it to a release.
