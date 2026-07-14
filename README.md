# Video Trimmer GUI

A simple desktop app for cutting multiple clips out of one video file, using ffmpeg.

---

## 1. Requirements

- **Python 3.8+** (Windows usually needs this installed manually — [python.org/downloads](https://www.python.org/downloads/))
  - `tkinter` comes bundled with Python on Windows/Mac. On Linux you may need `sudo apt install python3-tk`.
- **ffmpeg** — a separate program the script calls to do the actual video trimming (and merging downloaded streams). It does **not** come with Python and must be installed on its own (see Step 2).
- **yt-dlp** — only needed if you want to use the **Video URL / Download** feature to pull a video in directly from a link, instead of picking a local file. Install with:
  ```
  pip install -U yt-dlp
  ```
  If it's not installed, the rest of the app (browsing/trimming local files) still works fine — you'll just get a clear error if you try to use the Download button.

> ⚠️ **Use the Download feature responsibly.** Only download videos you own, have explicit permission to save, or that are licensed for reuse (e.g. Creative Commons). Downloading and/or redistributing copyrighted content without permission can violate a platform's Terms of Service and copyright law.

---

## 2. Install ffmpeg (Windows)

1. Download a build from **[gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/)** — pick the "release essentials" zip.
2. Extract the zip somewhere permanent, e.g. `C:\ffmpeg`.
   - After extracting, you'll usually see a nested folder like:
     `C:\ffmpeg\ffmpeg-<version>\bin\ffmpeg.exe`
   - **Note the full path to that `bin` folder** — you'll need it in the next step.
3. Add that `bin` folder to your PATH:
   - Press the **Windows key**, type `env`, open **Edit the system environment variables**
   - Click **Environment Variables**
   - Under **System variables** (or **User variables**), select `Path` → **Edit** → **New**
   - Paste the full path to the `bin` folder, e.g.:
     ```
     C:\ffmpeg\ffmpeg-2026-07-09-git-8de8405796-full_build\bin
     ```
   - Click **OK** on all open windows.
4. **Close any open Command Prompt / terminal windows completely** and open a new one — PATH changes don't apply to windows that were already open.
5. Verify it worked:
   ```
   ffmpeg -version
   ```
   You should see version info printed. If you instead see `'ffmpeg' is not recognized...`, double check the exact folder path you added (it must be the `bin` folder that directly contains `ffmpeg.exe`, not a parent folder).

### Mac / Linux
```
brew install ffmpeg          # Mac (Homebrew)
sudo apt install ffmpeg      # Linux (Debian/Ubuntu)
```

---

## 3. Run the app

From a terminal, in the folder containing the script:
```
python video_trimmer_gui.py
```

If double-clicking the `.py` file doesn't open the GUI, always run it from a terminal first — this way you'll see any error messages instead of a window that silently closes.

---

## 4. Using the app

1. **Video URL (optional)** — paste a link and click **Download** to pull a video in directly instead of picking a local file. A status label shows progress; when finished, the **Input video** field is filled in automatically. Requires `yt-dlp` (see Step 1) and only works on videos you have the right to download.
2. **Input video** — click **Browse** and select the video file you want to trim (skip this if you just used Download above).
3. **Output folder** — click **Browse** and choose where trimmed clips (and any downloads) should be saved.
4. **Trims** — each row defines one clip to cut out:
   - **Output filename** — e.g. `intro_clip.mp4`
   - **Start (HH:MM:SS)** — when the clip should begin
   - **End (HH:MM:SS)** — when the clip should end
   - Click **Remove** to delete a row.
5. Click **+ Add trim** to add more rows — you can queue up as many clips as you like from the same source video.
6. **Frame-accurate (slower, re-encodes video)** checkbox:
   - **Unchecked (default):** fast, no quality loss, but cuts snap to the nearest keyframe (may be off by a second or two).
   - **Checked:** exact cuts at the precise time you enter, but slower and slightly re-compresses the video.
7. Click **Export all**. A popup will confirm how many files were created, and list any errors.

---

## 5. Troubleshooting

| Problem | Fix |
|---|---|
| `ffmpeg was not found on PATH` | ffmpeg isn't installed or PATH wasn't set correctly — redo Step 2, and make sure you opened a **new** terminal/GUI session afterward. |
| `yt-dlp is not installed` (when clicking Download) | Run `pip install -U yt-dlp`, then restart the app. |
| Download fails with a format/extraction error | The site may have changed something that broke the current yt-dlp version. Update it: `pip install -U yt-dlp`. |
| Download seems stuck | Large videos or slow connections can take a while — check the status label for a percentage. If it's not moving at all after a minute, cancel and retry with a smaller `--quality`-equivalent source, or check your connection. |
| `'ffmpeg' is not recognized...` when testing in terminal | You added the wrong folder to PATH. It must be the exact `bin` folder containing `ffmpeg.exe`, not the parent `ffmpeg` folder. Run `dir /s ffmpeg.exe` (Windows) inside your ffmpeg folder to find the exact path. |
| Output video length doesn't match Start/End | Update to the latest version of the script — it now computes exact duration internally, which fixed a known ffmpeg timing quirk. |
| GUI won't open / closes immediately | Run it from a terminal (`python video_trimmer_gui.py`) instead of double-clicking, so you can read the error message. |
| "At least one trim row is required" | You tried to remove the last remaining row — leave at least one row, or just edit its values instead. |

---

## 6. Notes

- Output files are named exactly as typed in the **Output filename** field — make sure to include the extension (e.g. `.mp4`).
- If a filename already exists in the output folder, it will be **overwritten** without warning.
- Start/End times must be in `HH:MM:SS` format (e.g. `00:01:30` for 1 minute 30 seconds).
