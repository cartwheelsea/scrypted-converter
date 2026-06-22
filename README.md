# Scrypted Converter

A macOS desktop app that converts Scrypted NVR camera recordings (`.rtsp`) to standard MP4 files.

> **Note:** This is an independent project and is not affiliated with or endorsed by Scrypted.

## Download

A pre-built app for Apple Silicon Macs is available on the [Releases](../../releases) page.

## Requirements

- Apple Silicon Mac (M1 or later)
- [ffmpeg](https://ffmpeg.org/) — install via Homebrew:
  ```
  brew install ffmpeg
  ```

## Features

- Browse to your Scrypted recordings folder and convert all sessions in one click
- Skips recordings already converted
- Parallel conversion
- Organises output into `events/` and `recordings/` subfolders per camera
- Live progress bar, per-session log, and elapsed timer

## Usage

1. Open the app and click **Browse…** next to Recordings folder
2. Select your Scrypted recordings directory — this is typically the folder containing your `scrypted-*` camera folders
3. Choose an output folder (defaults to an `MP4` subfolder inside the recordings folder)
4. Click **Convert All Recordings**

**Run from source:**
```
python3 convert_gui.py
```

**Build a standalone app:**
```
pip install pyinstaller
pyinstaller "Scrypted Converter.spec"
```
The app will be in `dist/Scrypted Converter.app`.

## How it works

Each Scrypted recording folder contains numbered `.rtsp` segment files. The app spins up a local RTSP server, streams the segments through it, and passes the URL to ffmpeg which muxes the output to MP4.
