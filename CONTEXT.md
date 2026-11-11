# Clip to Discord

A Windows tray app that takes game clips saved by a capture app and posts them to Discord channels.

## Language

### Clips

**Capture app**:
The program that records gameplay and saves clips to disk: NVIDIA Instant Replay, AMD Instant Replay (part of AMD Software: Adrenalin Edition) or Medal.
_Avoid_: Source, recorder, ReLive (for AMD)

**Manual recording**:
A recording the player started and stopped by hand, rather than a replay save. AMD saves these in the same folder as replays, so they are posted too, but their originals are never deleted when longer than three minutes.
_Avoid_: Recording (alone)

**Original**:
The clip file exactly as the capture app saved it, before trimming or shrinking.
_Avoid_: Source file, raw clip

**Clip**:
The trimmed, shrunk copy of an original that is sent to Discord.
_Avoid_: Video, upload

### Posting

**Destination**:
One Discord channel the app can post to, identified by its webhook, with a name chosen by the user and an on/off state.
_Avoid_: Webhook, server, channel (when meaning the whole entry)

**Post**:
One Discord message carrying one clip to one destination.
_Avoid_: Upload, share

**Player name**:
The name of the person whose install is posting, shown as the sender of each post.
_Avoid_: Username, poster, clipper

**Boost level**:
The Discord server boost tier that sets how large a clip may be.
_Avoid_: Upload limit, max size

### App

**Settings window**:
The app's single window, with a Setup tab (player name, destinations, capture apps, startup and deletion choices) and a Clips tab (clip length, boost level, resolution, frame rate).
_Avoid_: Setup window, config screen
