#!/usr/bin/env python3
"""
video_trimmer_gui.py - GUI for trimming multiple clips out of one video using ffmpeg.

Requires ffmpeg to be installed and available on PATH:
  https://ffmpeg.org/download.html

Run:
  python video_trimmer_gui.py
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

import match_automation

try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False


# Where GUI state is temporarily stashed across a "Reload script" restart, so
# the running process can pick up edits saved to this .py file on disk
# without losing whatever the user has already typed into the GUI.
RELOAD_STATE_PATH = os.path.join(tempfile.gettempdir(), "video_trimmer_gui_reload_state.json")

# After 'Open scorecard', how often and how long to watch the clipboard.
CLIPBOARD_CHECK_MS = 700
CLIPBOARD_WATCH_SECONDS = 15 * 60
VIDEO_SEARCH_RESULTS = 8
BALL_QUEUE_CACHE_DIR = os.path.join(tempfile.gettempdir(), "video_trimmer_ball_queues")


def save_reload_state(state):
    """Write GUI state to disk ahead of a self-restart. Best-effort: swallows
    errors so a failed save never blocks the reload itself."""
    try:
        with open(RELOAD_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def load_and_clear_reload_state():
    """Read back GUI state saved by save_reload_state(), if any, and delete
    the file so a leftover state never gets picked up by an unrelated later
    run. Returns None if there's nothing to restore or it can't be read."""
    if not os.path.isfile(RELOAD_STATE_PATH):
        return None
    try:
        with open(RELOAD_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = None
    try:
        os.remove(RELOAD_STATE_PATH)
    except OSError:
        pass
    return state


# Watermark choices: overlay a fixed white character in the bottom-right
# corner for the entire duration of the trim (instead of a separate intro).
WATERMARK_CHOICES = ["None", "4", "6", "W"]

# A widely-available bold font used to render the watermark text.
_WATERMARK_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
]


def find_watermark_font():
    """Return an absolute path to a usable font file for drawtext, or None to
    let ffmpeg fall back to its own default font (if any is configured on
    the system)."""
    for candidate in _WATERMARK_FONT_CANDIDATES:
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def build_watermark_filter(character, font_filename=None):
    """Build a drawtext filter string that overlays `character` as a large
    white watermark in the top-right corner, with a semi-transparent
    backing box so it stays readable over busy footage.

    `font_filename` should be a bare filename with no directory component
    (see get_watermark_font_and_cwd) so it never contains a drive-letter
    colon or path separators — both of which are special characters in
    ffmpeg's filtergraph syntax and are a common source of "Invalid
    argument" errors on Windows if a full path is embedded directly.
    """
    escaped_char = character.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")

    parts = [f"text='{escaped_char}'", "fontcolor=white", "fontsize=h/4",
              "x=w-text_w-20", "y=20", "box=1", "boxcolor=black@0.4", "boxborderw=10"]
    if font_filename:
        parts.insert(0, f"fontfile={font_filename}")

    return "drawtext=" + ":".join(parts)


def escape_drawtext_text(value):
    """Escape a string for safe use as a drawtext filter's text value."""
    return (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace(":", "\\:")
        .replace(",", "\\,")
    )


def get_script_dir():
    """Directory this script lives in."""
    return os.path.dirname(os.path.abspath(__file__))


# If the Logo field in the Stat section is left blank, look for a file with
# one of these names sitting right next to the script, so a logo only needs
# to be dropped in once rather than browsed to every time.
DEFAULT_LOGO_FILENAMES = ["Cheetahs_Logo.png", "cheetahs_logo.png", "logo.png", "logo.jpg", "logo.jpeg", "team_logo.png"]


def find_default_logo():
    """Return the absolute path to a default logo file next to the script,
    or None if none of the recognized filenames exist there."""
    script_dir = get_script_dir()
    for name in DEFAULT_LOGO_FILENAMES:
        candidate = os.path.join(script_dir, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def build_stat_card_filter(name_text, stat_text, font_filename=None):
    """Build a drawtext filter string for a player stat card: the player's
    name centered in the frame, with a smaller stat line just below it.
    Both lines get a black outline stroke so they stay legible over busy
    backgrounds (e.g. a logo)."""
    esc_name = escape_drawtext_text(name_text)
    esc_stat = escape_drawtext_text(stat_text)

    name_parts = [f"text='{esc_name}'", "fontcolor=white", "bordercolor=black", "borderw=5",
                  "fontsize=110", "x=(w-text_w)/2", "y=(h-text_h)/2-70"]
    stat_parts = [f"text='{esc_stat}'", "fontcolor=white", "bordercolor=black", "borderw=4",
                  "fontsize=70", "x=(w-text_w)/2", "y=(h-text_h)/2+60"]
    if font_filename:
        name_parts.insert(0, f"fontfile={font_filename}")
        stat_parts.insert(0, f"fontfile={font_filename}")

    return "drawtext=" + ":".join(name_parts) + ",drawtext=" + ":".join(stat_parts)


def build_stat_card_filter_complex(name_text, stat_text, font_filename=None):
    """Build a -filter_complex graph for a player stat card with a logo
    composited into the top-right corner (270px tall — 1.5x the size the
    4/6/W watermark uses on trims, i.e. 1.5 * h/4 on this 720px-tall
    canvas), with the name/stat text drawn centered on top. Both lines get
    a black outline stroke so they stay legible over the logo artwork.

    Assumes input 0 is the black background canvas and input 1 is the logo
    image (looped so it persists for the whole clip). Returns the filter
    string; the caller must map its "[out]" pad.
    """
    esc_name = escape_drawtext_text(name_text)
    esc_stat = escape_drawtext_text(stat_text)

    name_parts = [f"text='{esc_name}'", "fontcolor=white", "bordercolor=black", "borderw=5",
                  "fontsize=110", "x=(w-text_w)/2", "y=(h-text_h)/2-70"]
    stat_parts = [f"text='{esc_stat}'", "fontcolor=white", "bordercolor=black", "borderw=4",
                  "fontsize=70", "x=(w-text_w)/2", "y=(h-text_h)/2+60"]
    if font_filename:
        name_parts.insert(0, f"fontfile={font_filename}")
        stat_parts.insert(0, f"fontfile={font_filename}")

    name_filter = "drawtext=" + ":".join(name_parts)
    stat_filter = "drawtext=" + ":".join(stat_parts)

    return (
        "[1:v]scale=-1:270[logo];"
        "[0:v][logo]overlay=W-w-20:20[bg];"
        f"[bg]{name_filter},{stat_filter}[out]"
    )


def get_watermark_font_and_cwd():
    """Return (font_filename, cwd) for use with build_watermark_filter() and
    subprocess.run(). font_filename is a bare filename (safe to embed in a
    filter string) and cwd is the directory ffmpeg should be run from so it
    can find that file; (None, None) if no usable font was found, in which
    case the filter is built without a fontfile and ffmpeg uses its own
    default."""
    font_path = find_watermark_font()
    if not font_path:
        return None, None
    return os.path.basename(font_path), os.path.dirname(font_path)


def render_stat_card(name, stat_text, output_path, logo_path=None):
    """Render one 3-second stat card. Returns ffmpeg's error text, or None if
    the video was created."""
    font_filename, font_dir = get_watermark_font_and_cwd()
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=1280x720:d=3:r=30"]
    if logo_path:
        filt = build_stat_card_filter_complex(name, stat_text, font_filename)
        cmd += [
            "-loop", "1", "-i", logo_path,
            "-filter_complex", filt,
            "-map", "[out]",
            "-t", "3",
        ]
    else:
        filt = build_stat_card_filter(name, stat_text, font_filename)
        cmd += ["-vf", filt]
    cmd += [
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=font_dir)
    return None if result.returncode == 0 else result.stderr[-300:]


def ffprobe_has_audio(path):
    """Return True if the file has an audio stream."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index", "-of", "csv=p=0", path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return bool(result.stdout.strip())


def _ball_queue_cache_path(video_path, kind="queue"):
    """Where a video's scan results are kept. The key changes if the file does."""
    info = os.stat(video_path)
    key = f"v2|{kind}|{os.path.abspath(video_path)}|{info.st_size}|{int(info.st_mtime)}"
    return os.path.join(BALL_QUEUE_CACHE_DIR, hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")


def _load_or_scan(video_path, kind, scan):
    """Return a saved scan of this video, or run `scan()` once and save it."""
    cache_path = _ball_queue_cache_path(video_path, kind)
    try:
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    result = scan()
    os.makedirs(BALL_QUEUE_CACHE_DIR, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(result, f)
    return result


def load_or_scan_ball_queue(video_path, duration, on_progress=None):
    """Return every delivery as [time, sure, score], scanning only the first time."""
    return _load_or_scan(
        video_path, "queue",
        lambda: match_automation.build_ball_queue(video_path, duration, on_progress),
    )


def load_or_scan_scoreboard(video_path, on_progress=None):
    """Return every time the on-screen scoreboard changed, scanning only the first time."""
    return _load_or_scan(
        video_path, "scoreboard",
        lambda: match_automation.scan_scoreboard_changes(video_path, 0.0, None, on_progress),
    )


def find_match_video(teams, date):
    """Search YouTube for this match. Returns (url, title), or None."""
    if not YTDLP_AVAILABLE:
        return None
    options = {"quiet": True, "no_warnings": True, "extract_flat": True}
    query = f"ytsearch{VIDEO_SEARCH_RESULTS}:{match_automation.video_search_text(teams, date)}"
    with yt_dlp.YoutubeDL(options) as ydl:
        results = ydl.extract_info(query, download=False).get("entries") or []
    video = match_automation.pick_match_video(results, teams, date)
    if video is None:
        return None
    url = video.get("url") or f"https://www.youtube.com/watch?v={video.get('id')}"
    return url, video.get("title")


def time_str_to_seconds(time_str):
    """Convert HH:MM:SS, MM:SS, or plain seconds string to a float number of seconds."""
    parts = time_str.strip().split(":")
    parts = [float(p) for p in parts]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    hours, minutes, seconds = parts
    return hours * 3600 + minutes * 60 + seconds


def seconds_to_time_str(total_seconds):
    """Convert a number of seconds to an HH:MM:SS.mmm string for ffmpeg -t."""
    if total_seconds < 0:
        total_seconds = 0
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


def ffprobe_duration(path):
    """Return a clip's duration in seconds via ffprobe, needed to compute
    xfade/acrossfade offsets when stitching clips together."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"ffprobe could not read duration for {path}: {result.stderr[-200:]}")
    return float(result.stdout.strip())


def build_crossfade_stitch_cmd(clip_paths, output_path, transition_seconds=0.2):
    """Build an ffmpeg command that joins clip_paths (in order) into
    output_path, with a short crossfade (video) / audio-crossfade between
    each consecutive pair instead of a hard cut, via chained xfade/
    acrossfade filters. Returns the full ffmpeg argv list.

    Needs each clip's duration up front (via ffprobe) because xfade takes
    an absolute time offset for where the fade should start on the
    running, combined timeline rather than a relative one.
    """
    durations = [ffprobe_duration(p) for p in clip_paths]

    # Clamp the transition so it's never longer than half of the shortest
    # clip — otherwise the offset ffmpeg needs to compute would go
    # negative and the filter graph would be rejected.
    shortest = min(durations)
    transition = min(transition_seconds, max(shortest / 4, 0.05))

    cmd = ["ffmpeg", "-y"]
    for p in clip_paths:
        cmd += ["-i", p]

    filter_parts = []
    current_v = "0:v:0"
    current_a = "0:a:0"
    cumulative = durations[0]
    n = len(clip_paths)

    for idx in range(1, n):
        offset = max(cumulative - transition, 0)
        next_v = f"{idx}:v:0"
        next_a = f"{idx}:a:0"
        is_last = idx == n - 1
        out_v = "outv" if is_last else f"v{idx}"
        out_a = "outa" if is_last else f"a{idx}"

        filter_parts.append(
            f"[{current_v}][{next_v}]xfade=transition=fade:duration={transition:.3f}:offset={offset:.3f}[{out_v}]"
        )
        filter_parts.append(f"[{current_a}][{next_a}]acrossfade=d={transition:.3f}[{out_a}]")

        current_v, current_a = out_v, out_a
        cumulative = cumulative + durations[idx] - transition

    filter_complex = ";".join(filter_parts)
    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-c:a", "aac",
        "-movflags", "+faststart",
        output_path,
    ]
    return cmd


def parse_bulk_trim_text(text):
    """Parse the bulk-entry format:

        Name:
        start_1-end_1
        start_2-end_2-watermark_2
        Name 2:
        start_1-end_1

    A line ending in ':' starts a new named group (the trailing colon is
    stripped to get the name). Any other non-blank line is a '-'-separated
    "start-end" or "start-end-watermark" entry for the current group
    (start/end times can freely contain ':', e.g. "1:55:17-1:55:23", since
    the split is on '-' rather than ':'). The optional third field sets that
    row's Watermark value (e.g. "4", "6", "W", or any other text — it isn't
    limited to WATERMARK_CHOICES); omitting it defaults to "None".

    Returns (rows, errors) where rows is a list of
    (output_filename, start, end, watermark) tuples with filenames like
    "Name_1.mp4", "Name_2.mp4", etc., and errors is a list of human
    readable messages for any lines that couldn't be parsed.
    """
    rows = []
    errors = []
    current_name = None
    counters = {}

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        if line.endswith(":"):
            current_name = line[:-1].strip()
            if not current_name:
                errors.append(f"Line {lineno}: empty name before ':'.")
                current_name = None
            else:
                counters.setdefault(current_name, 0)
            continue

        if current_name is None:
            errors.append(f"Line {lineno}: found a time pair before any 'Name:' line.")
            continue

        parts = [p.strip() for p in line.split("-")]
        if len(parts) == 2:
            start, end = parts
            watermark = "None"
        elif len(parts) == 3:
            start, end, watermark = parts
        else:
            errors.append(f"Line {lineno}: expected 'start-end' or 'start-end-watermark' but found '{line}'.")
            continue

        if not start or not end:
            errors.append(f"Line {lineno}: expected 'start-end' or 'start-end-watermark' but found '{line}'.")
            continue
        watermark = watermark or "None"

        counters[current_name] += 1
        filename = f"{current_name}_{counters[current_name]}.mp4"
        rows.append((filename, start, end, watermark))

    return rows, errors


class TrimRow:
    """One row of trim inputs: output filename, start time, end time, intro
    clip choice, remove button."""

    def __init__(
        self,
        parent,
        on_remove,
        filename="output.mp4",
        start="00:00:00",
        end="00:00:10",
        watermark="None",
    ):
        self.frame = ttk.Frame(parent, relief="groove", borderwidth=1, padding=8)
        self.is_auto = False  # True if created by "Find highlights"

        ttk.Label(self.frame, text="Output filename").grid(row=0, column=0, sticky="w")
        ttk.Label(self.frame, text="Start (HH:MM:SS)").grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Label(self.frame, text="End (HH:MM:SS)").grid(row=0, column=2, sticky="w", padx=(10, 0))
        ttk.Label(self.frame, text="Watermark").grid(row=0, column=3, sticky="w", padx=(10, 0))

        self.filename_var = tk.StringVar(value=filename)
        self.start_var = tk.StringVar(value=start)
        self.end_var = tk.StringVar(value=end)
        self.watermark_var = tk.StringVar(value=watermark or "None")

        ttk.Entry(self.frame, textvariable=self.filename_var, width=24).grid(row=1, column=0, sticky="we")
        ttk.Entry(self.frame, textvariable=self.start_var, width=12).grid(row=1, column=1, sticky="we", padx=(10, 0))
        ttk.Entry(self.frame, textvariable=self.end_var, width=12).grid(row=1, column=2, sticky="we", padx=(10, 0))
        ttk.Combobox(
            self.frame,
            textvariable=self.watermark_var,
            values=WATERMARK_CHOICES,
            width=6,
        ).grid(row=1, column=3, sticky="we", padx=(10, 0))

        remove_btn = ttk.Button(self.frame, text="Remove", command=lambda: on_remove(self))
        remove_btn.grid(row=1, column=4, padx=(10, 0))

    def get_values(self):
        return (
            self.filename_var.get().strip(),
            self.start_var.get().strip(),
            self.end_var.get().strip(),
            self.watermark_var.get().strip(),
        )

    def destroy(self):
        self.frame.destroy()


# In the output folder: what each clip was cut with, what each stat card says,
# and what went into each reel or highlight, so Generate videos redoes only
# what changed.
CUT_RECORD = "trim_record.json"


def _cut_signature(input_path, row):
    """What a Trims row cuts: the source video, its start and end, its watermark."""
    _, _, _, _, watermark, start_sec, end_sec, _ = row
    return [os.path.abspath(input_path), round(start_sec, 3), round(end_sec, 3), watermark]


def load_cut_record(output_dir):
    try:
        with open(os.path.join(output_dir, CUT_RECORD), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_cut_record(output_dir, record):
    try:
        with open(os.path.join(output_dir, CUT_RECORD), "w", encoding="utf-8") as f:
            json.dump(record, f, indent=1)
    except OSError:
        pass


def changed_rows(output_dir, input_path, rows, record):
    """The rows whose clip is missing or was cut with different settings."""
    return [
        row for row in rows
        if record.get(row[1]) != _cut_signature(input_path, row)
        or not os.path.exists(os.path.join(output_dir, row[1]))
    ]


def player_of_clip(filename):
    """'Maniraj Durairaj_3.mp4' -> 'Maniraj Durairaj', or None."""
    match = re.match(r"^(.+)_\d+\.mp4$", filename)
    return match.group(1) if match else None


def describe_clip(clip):
    """Say what the scorecard says happened on this clip's ball."""
    what = {"4": " hit a four", "6": " hit a six", "W": " took a wicket" if clip.group == "Bowling" else " was out"}
    return f"Over {clip.over_label}: {clip.player}{what.get(clip.watermark, '')}"


class BallReviewDialog:
    """Play a clip whose ball wasn't seen clearly and ask whether it's right,
    before it is cut. The result is "yes", "skip" or "stop", and the clip's
    start and end may have been nudged."""

    def __init__(self, root, video_path, clip, number, total, deliveries=()):
        self.video_path = video_path
        self.start, self.end = clip.start, clip.end
        self.deliveries = sorted(deliveries)
        self.result = "skip"
        self._player = None
        self._plays = 0

        self.top = tk.Toplevel(root)
        self.top.title("Check this ball")
        self.top.transient(root)
        self.top.resizable(False, False)
        frame = ttk.Frame(self.top, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"Ball {number} of {total} to check", foreground="#555555").pack(anchor="w")
        ttk.Label(frame, text=describe_clip(clip), font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(4, 6))
        ttk.Label(
            frame,
            wraplength=460,
            justify="left",
            text=(
                "The app found the ball clearly here. "
                if clip.exact else
                "The ball itself wasn't clear here, so this is the app's best guess. "
            ) + "Watch it: is this that ball, from the run-up to the shot?",
        ).pack(anchor="w")
        self.times_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.times_var, foreground="#555555").pack(anchor="w", pady=(6, 8))
        self._show_times()

        nudge = ttk.Frame(frame)
        nudge.pack(fill="x")
        ttk.Button(nudge, text="▶ Play again", command=self.play).pack(side="left")
        if self.deliveries:
            ttk.Button(nudge, text="◀ Previous ball", command=lambda: self.jump(-1)).pack(side="left", padx=(12, 0))
            ttk.Button(nudge, text="Next ball ▶", command=lambda: self.jump(1)).pack(side="left", padx=(6, 0))
        self.status_var = tk.StringVar()
        ttk.Label(nudge, textvariable=self.status_var, foreground="#555555").pack(side="left", padx=(12, 0))

        # Move the whole clip, keeping its length.
        move = ttk.Frame(frame)
        move.pack(fill="x", pady=(8, 0))
        ttk.Label(move, text="Move the cut:").pack(side="left")
        for seconds in (-4, -3, -2, -1, 1, 2, 3, 4):
            ttk.Button(
                move, text=f"{seconds:+d} s".replace("-", "−"), width=5,
                command=lambda s=seconds: self.shift(s),
            ).pack(side="left", padx=(12 if seconds in (-4, 1) else 3, 0))

        answer = ttk.Frame(frame)
        answer.pack(fill="x", pady=(12, 0))
        ttk.Button(answer, text="Yes, cut it", command=lambda: self.close("yes")).pack(side="left")
        ttk.Button(answer, text="No, leave this ball out", command=lambda: self.close("skip")).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(answer, text="Stop asking (cut the rest as found)", command=lambda: self.close("stop")).pack(
            side="right"
        )
        self.top.protocol("WM_DELETE_WINDOW", lambda: self.close("skip"))
        self.top.grab_set()
        self.play()
        root.wait_window(self.top)

    def _show_times(self):
        self.times_var.set(
            f"Clip: {seconds_to_time_str(self.start)[:10]} to {seconds_to_time_str(self.end)[:10]}"
            f"  ({self.end - self.start:g} seconds)"
        )

    def shift(self, seconds):
        length = self.end - self.start
        self.start = max(self.start + seconds, 0)
        self.end = self.start + length
        self._show_times()
        self.play()

    def jump(self, direction):
        """Move the clip to the delivery before or after this one in the video."""
        half = (self.end - self.start) / 2
        centre = self.start + half
        if direction < 0:
            earlier = [t for t in self.deliveries if t < centre - 1]
            target = earlier[-1] if earlier else None
        else:
            later = [t for t in self.deliveries if t > centre + 1]
            target = later[0] if later else None
        if target is None:
            return
        self.start, self.end = max(target - half, 0), target + half
        self._show_times()
        self.play()

    def _stop_player(self):
        if self._player and self._player.poll() is None:
            self._player.terminate()
        self._player = None

    def play(self):
        """Play exactly the clip that would be cut. ffplay can only jump to a
        keyframe (every few seconds in a stream), so the clip is cut first, the
        same way the export cuts it, and that file is played."""
        self._stop_player()
        self._plays += 1
        play_number = self._plays
        start, length = self.start, self.end - self.start
        self.status_var.set("Getting the clip ready...")

        preview = os.path.join(tempfile.gettempdir(), f"video_trimmer_check_ball_{play_number % 2}.mp4")
        cutter = threading.Thread(
            target=subprocess.run,
            args=(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", self.video_path,
                   "-t", f"{length:.3f}", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                   "-c:a", "aac", preview],),
            kwargs={"capture_output": True},
            daemon=True,
        )
        cutter.start()

        def wait_for_cut():
            if cutter.is_alive():
                self.top.after(100, wait_for_cut)
            else:
                self._show_preview(preview, play_number)

        self.top.after(100, wait_for_cut)

    def _show_preview(self, preview, play_number):
        if play_number != self._plays or not self.top.winfo_exists():
            return  # a newer nudge replaced this one, or the dialog closed
        self.status_var.set("")
        if shutil.which("ffplay"):
            self._player = subprocess.Popen(
                ["ffplay", "-loglevel", "error", "-autoexit", "-x", "800", "-y", "450",
                 "-window_title", "Check this ball", preview],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            os.startfile(preview)

    def close(self, result):
        self.result = result
        self._stop_player()
        self.top.grab_release()
        self.top.destroy()


class VideoTrimmerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Video Trimmer")
        self.root.geometry("640x520")  # fallback size if maximizing below doesn't work
        self._maximize_window()

        self.input_path_var = tk.StringVar()
        self.output_dir_var = tk.StringVar()
        self.reencode_var = tk.BooleanVar(value=True)
        self.url_var = tk.StringVar()
        self.download_status_var = tk.StringVar(value="")
        self.stitch_var = tk.BooleanVar(value=False)
        self.stitch_filename_var = tk.StringVar(value="stitched.mp4")
        self.match_status_var = tk.StringVar(value="")
        self._capture_active = False
        self._queue_times = {}  # video path -> every delivery time, for the review popup
        self._capture_deadline = 0
        self._captured_innings = []
        self._clipboard_seen = ""

        self.trim_rows = []
        self._export_in_progress = False

        self._build_ui()
        self._restore_reload_state_or_default()

    def _restore_reload_state_or_default(self):
        """After a self-restart via 'Reload script', repopulate the GUI from
        whatever was saved just before the restart. With nothing to restore,
        just start with the usual single empty trim row."""
        state = load_and_clear_reload_state()
        if not state:
            self.add_trim_row()  # start with one row
            return

        self.input_path_var.set(state.get("input_path", ""))
        self.output_dir_var.set(state.get("output_dir", ""))
        self.reencode_var.set(bool(state.get("reencode", True)))
        self.url_var.set(state.get("url", ""))

        bulk_text = state.get("bulk_text", "")
        if bulk_text:
            self.bulk_text.insert("1.0", bulk_text)

        self.match_batting_text.insert("1.0", state.get("match_batting", ""))
        self.match_bowling_text.insert("1.0", state.get("match_bowling", ""))
        self.match_clock_var.set(state.get("match_clock", ""))
        self.club_highlights_var.set(bool(state.get("club_highlights", False)))
        self.check_all_cuts_var.set(bool(state.get("check_all_cuts", False)))
        self.scorecard_link_var.set(state.get("scorecard_link", ""))
        self.our_team_var.set(state.get("our_team", "Cheetahs"))

        rows = state.get("rows") or []
        if rows:
            for r in rows:
                self.add_trim_row(
                    filename=r.get("filename"),
                    start=r.get("start"),
                    end=r.get("end"),
                    watermark=r.get("watermark"),
                ).is_auto = bool(r.get("auto"))
        else:
            self.add_trim_row()

        stat = state.get("stat") or {}
        self.stat_type_var.set(stat.get("type", "Batting"))
        self.stat_name_var.set(stat.get("name", ""))
        self.stat_batting_runs_var.set(stat.get("batting_runs", ""))
        self.stat_batting_balls_var.set(stat.get("batting_balls", ""))
        self.stat_bowling_overs_var.set(stat.get("bowling_overs", ""))
        self.stat_bowling_maidens_var.set(stat.get("bowling_maidens", ""))
        self.stat_bowling_runs_var.set(stat.get("bowling_runs", ""))
        self.stat_bowling_wickets_var.set(stat.get("bowling_wickets", ""))
        self.stat_logo_path_var.set(stat.get("logo_path", ""))
        self.stat_use_logo_var.set(bool(stat.get("use_logo", False)))
        self._update_stat_field_states()

    def reload_script(self):
        """Restart this script in-place so any edits saved to the .py file
        on disk take effect, while preserving everything currently filled
        into the GUI (input video, output folder, bulk-import text, every
        trim row, and the frame-accurate checkbox)."""
        state = {
            "input_path": self.input_path_var.get(),
            "output_dir": self.output_dir_var.get(),
            "reencode": self.reencode_var.get(),
            "url": self.url_var.get(),
            "bulk_text": self.bulk_text.get("1.0", "end-1c"),
            "match_batting": self.match_batting_text.get("1.0", "end-1c"),
            "match_bowling": self.match_bowling_text.get("1.0", "end-1c"),
            "match_clock": self.match_clock_var.get(),
            "club_highlights": self.club_highlights_var.get(),
            "check_all_cuts": self.check_all_cuts_var.get(),
            "scorecard_link": self.scorecard_link_var.get(),
            "our_team": self.our_team_var.get(),
            "rows": [
                {
                    "filename": filename,
                    "start": start,
                    "end": end,
                    "watermark": watermark,
                    "auto": row.is_auto,
                }
                for row, (filename, start, end, watermark) in (
                    (row, row.get_values()) for row in self.trim_rows
                )
            ],
            "stat": {
                "type": self.stat_type_var.get(),
                "name": self.stat_name_var.get(),
                "batting_runs": self.stat_batting_runs_var.get(),
                "batting_balls": self.stat_batting_balls_var.get(),
                "bowling_overs": self.stat_bowling_overs_var.get(),
                "bowling_maidens": self.stat_bowling_maidens_var.get(),
                "bowling_runs": self.stat_bowling_runs_var.get(),
                "bowling_wickets": self.stat_bowling_wickets_var.get(),
                "logo_path": self.stat_logo_path_var.get(),
                "use_logo": self.stat_use_logo_var.get(),
            },
        }
        save_reload_state(state)

        # Spawn a fresh copy of this same script and then exit the current
        # process, so the new one re-reads the .py file from disk and any
        # saved edits take effect immediately.
        #
        # subprocess.Popen (not os.execv) is used deliberately: on Windows,
        # os.execv is emulated by naively joining the argument list with
        # spaces and re-splitting it in the new process, which corrupts any
        # path containing a space (e.g. "F:\Some Folder\video_trimmer_gui.py"
        # becomes two arguments and fails to open). Popen builds the command
        # line properly (quoting arguments as needed) on every platform.
        python = sys.executable
        script = os.path.abspath(__file__)
        try:
            subprocess.Popen([python, script] + sys.argv[1:])
        except OSError as e:
            messagebox.showerror("Video Trimmer", f"Could not restart the script:\n{e}")
            return

        # Exit immediately rather than tearing down Tk normally — we don't
        # need any further cleanup here, and the new process is already
        # starting up independently.
        os._exit(0)

    def _maximize_window(self):
        """Maximize the window on startup. Tries the platform-appropriate
        approach and falls back gracefully if it's not supported."""
        try:
            # Windows and some Linux window managers.
            self.root.state("zoomed")
            return
        except tk.TclError:
            pass

        try:
            # Most Linux/X11 window managers.
            self.root.attributes("-zoomed", True)
            return
        except tk.TclError:
            pass

        try:
            # Fallback (e.g. macOS, or WMs that support neither above):
            # size the window to the full screen manually.
            width = self.root.winfo_screenwidth()
            height = self.root.winfo_screenheight()
            self.root.geometry(f"{width}x{height}+0+0")
        except tk.TclError:
            pass  # keep the default geometry set in __init__

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        # Top bar: reload the script's code from disk without losing
        # whatever is currently filled into the GUI.
        top_bar = ttk.Frame(self.root)
        top_bar.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Button(top_bar, text="\u21bb Reload script", command=self.reload_script).pack(side="right")

        # Video URL — can be trimmed directly (Export all pulls just the
        # needed seconds per trim row), or pre-downloaded in full first.
        url_frame = ttk.Frame(self.root)
        url_frame.pack(fill="x", **pad)
        ttk.Label(url_frame, text="Video URL (trims can be cut directly from this, or pre-download below):").pack(anchor="w")
        row_url = ttk.Frame(url_frame)
        row_url.pack(fill="x")
        ttk.Entry(row_url, textvariable=self.url_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row_url, text="Pre-download full video", command=self.download_from_url).pack(
            side="left", padx=(6, 0)
        )
        ttk.Label(
            url_frame,
            textvariable=self.download_status_var,
            foreground="#555555",
        ).pack(anchor="w", pady=(2, 0))
        ttk.Label(
            url_frame,
            text=(
                "Only use videos you own or have permission/rights to download. If 'Input video' "
                "below is filled in, it's used instead of this URL."
            ),
            font=("TkDefaultFont", 8),
            foreground="#888888",
        ).pack(anchor="w")

        # Input video picker
        input_frame = ttk.Frame(self.root)
        input_frame.pack(fill="x", **pad)
        ttk.Label(input_frame, text="Input video (local file — leave blank to trim straight from the URL above):").pack(anchor="w")
        row = ttk.Frame(input_frame)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.input_path_var, state="readonly").pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(row, text="Browse", command=self.browse_input).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="Clear", command=lambda: self.input_path_var.set("")).pack(
            side="left", padx=(6, 0)
        )

        # Output folder picker
        output_frame = ttk.Frame(self.root)
        output_frame.pack(fill="x", **pad)
        ttk.Label(output_frame, text="Output folder:").pack(anchor="w")
        row2 = ttk.Frame(output_frame)
        row2.pack(fill="x")
        ttk.Entry(row2, textvariable=self.output_dir_var, state="readonly").pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(row2, text="Browse", command=self.browse_output_dir).pack(side="left", padx=(6, 0))

        # Paste a match's ball-by-ball text to find highlights and build reels
        self._build_match_section()

        # Player stat card generator
        self._build_stat_section()

        # Bulk import
        bulk_frame = ttk.LabelFrame(self.root, text="Bulk import trims")
        bulk_frame.pack(fill="x", padx=10, pady=(6, 0))

        self.bulk_text = tk.Text(bulk_frame, height=6, wrap="none")
        self.bulk_text.pack(fill="x", padx=6, pady=(0, 6))

        bulk_btn_row = ttk.Frame(bulk_frame)
        bulk_btn_row.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(bulk_btn_row, text="Load from .txt file...", command=self.load_bulk_file).pack(
            side="left"
        )
        ttk.Button(
            bulk_btn_row, text="Add rows from text above", command=self.add_rows_from_bulk_text
        ).pack(side="left", padx=(6, 0))

        # Trims label + scrollable area
        ttk.Label(self.root, text="Trims:").pack(anchor="w", padx=10, pady=(10, 0))

        container = ttk.Frame(self.root)
        container.pack(fill="both", expand=True, padx=10, pady=6)

        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self.rows_frame = ttk.Frame(canvas)

        self.rows_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Add trim + Export all buttons, side by side
        add_export_row = ttk.Frame(self.root)
        add_export_row.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Button(add_export_row, text="+ Add trim", command=self.add_trim_row).pack(side="left")
        self.export_button = ttk.Button(add_export_row, text="Export all", command=self.export_all)
        self.export_button.pack(side="right")

        # Frame-accurate toggle
        ttk.Checkbutton(
            self.root,
            text="Frame-accurate (slower, re-encodes video)",
            variable=self.reencode_var,
        ).pack(anchor="w", padx=10, pady=(0, 4))

        # Stitch-together toggle
        stitch_row = ttk.Frame(self.root)
        stitch_row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Checkbutton(
            stitch_row,
            text="Also stitch all trimmed clips into one video:",
            variable=self.stitch_var,
        ).pack(side="left")
        ttk.Entry(stitch_row, textvariable=self.stitch_filename_var, width=22).pack(
            side="left", padx=(6, 0)
        )

    def _build_stat_section(self):
        stat_frame = ttk.LabelFrame(self.root, text="Stat")
        stat_frame.pack(fill="x", padx=10, pady=(6, 0))

        row0 = ttk.Frame(stat_frame)
        row0.pack(fill="x", padx=6, pady=(6, 4))
        ttk.Label(row0, text="Type:").pack(side="left")
        self.stat_type_var = tk.StringVar(value="Batting")
        stat_type_combo = ttk.Combobox(
            row0,
            textvariable=self.stat_type_var,
            values=["Batting", "Bowling", "All Round"],
            state="readonly",
            width=12,
        )
        stat_type_combo.pack(side="left", padx=(6, 20))
        stat_type_combo.bind("<<ComboboxSelected>>", lambda e: self._update_stat_field_states())

        ttk.Label(row0, text="Name:").pack(side="left")
        self.stat_name_var = tk.StringVar()
        ttk.Entry(row0, textvariable=self.stat_name_var, width=24).pack(side="left", padx=(6, 0))

        # Batting fields
        row1 = ttk.Frame(stat_frame)
        row1.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(row1, text="Runs:").pack(side="left")
        self.stat_batting_runs_var = tk.StringVar()
        self.batting_runs_entry = ttk.Entry(row1, textvariable=self.stat_batting_runs_var, width=8)
        self.batting_runs_entry.pack(side="left", padx=(6, 20))

        ttk.Label(row1, text="Balls:").pack(side="left")
        self.stat_batting_balls_var = tk.StringVar()
        self.batting_balls_entry = ttk.Entry(row1, textvariable=self.stat_batting_balls_var, width=8)
        self.batting_balls_entry.pack(side="left", padx=(6, 0))

        # Bowling fields
        row2 = ttk.Frame(stat_frame)
        row2.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(row2, text="Overs:").pack(side="left")
        self.stat_bowling_overs_var = tk.StringVar()
        self.bowling_overs_entry = ttk.Entry(row2, textvariable=self.stat_bowling_overs_var, width=6)
        self.bowling_overs_entry.pack(side="left", padx=(6, 14))

        ttk.Label(row2, text="Maidens:").pack(side="left")
        self.stat_bowling_maidens_var = tk.StringVar()
        self.bowling_maidens_entry = ttk.Entry(row2, textvariable=self.stat_bowling_maidens_var, width=6)
        self.bowling_maidens_entry.pack(side="left", padx=(6, 14))

        ttk.Label(row2, text="Runs:").pack(side="left")
        self.stat_bowling_runs_var = tk.StringVar()
        self.bowling_runs_entry = ttk.Entry(row2, textvariable=self.stat_bowling_runs_var, width=6)
        self.bowling_runs_entry.pack(side="left", padx=(6, 14))

        ttk.Label(row2, text="Wickets:").pack(side="left")
        self.stat_bowling_wickets_var = tk.StringVar()
        self.bowling_wickets_entry = ttk.Entry(row2, textvariable=self.stat_bowling_wickets_var, width=6)
        self.bowling_wickets_entry.pack(side="left", padx=(6, 0))

        row_logo = ttk.Frame(stat_frame)
        row_logo.pack(fill="x", padx=6, pady=(0, 2))
        self.stat_use_logo_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row_logo, text="Use logo:", variable=self.stat_use_logo_var).pack(side="left")
        self.stat_logo_path_var = tk.StringVar()
        ttk.Entry(row_logo, textvariable=self.stat_logo_path_var, width=50, state="readonly").pack(
            side="left", padx=(6, 6)
        )
        ttk.Button(row_logo, text="Browse", command=self.browse_stat_logo).pack(side="left")
        ttk.Button(row_logo, text="Clear", command=lambda: self.stat_logo_path_var.set("")).pack(
            side="left", padx=(6, 0)
        )
        ttk.Label(
            stat_frame,
            text=(
                "Logo is only added when 'Use logo' is checked. Leave the path blank to "
                "auto-use a file named '" + DEFAULT_LOGO_FILENAMES[0] + "' (or "
                + ", ".join(DEFAULT_LOGO_FILENAMES[1:])
                + ") placed next to this script, if one exists."
            ),
            font=("TkDefaultFont", 8),
            foreground="#888888",
        ).pack(anchor="w", padx=6, pady=(0, 6))

        row3 = ttk.Frame(stat_frame)
        row3.pack(fill="x", padx=6, pady=(0, 8))
        ttk.Button(row3, text="Create stat video(s)", command=self.create_stat_videos).pack(side="left")

        self._update_stat_field_states()

    def browse_stat_logo(self):
        path = filedialog.askopenfilename(
            title="Select logo image",
            filetypes=[("Image files", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")],
        )
        if path:
            self.stat_logo_path_var.set(path)

    def _update_stat_field_states(self):
        stat_type = self.stat_type_var.get()
        batting_state = "normal" if stat_type in ("Batting", "All Round") else "disabled"
        bowling_state = "normal" if stat_type in ("Bowling", "All Round") else "disabled"
        self.batting_runs_entry.configure(state=batting_state)
        self.batting_balls_entry.configure(state=batting_state)
        self.bowling_overs_entry.configure(state=bowling_state)
        self.bowling_maidens_entry.configure(state=bowling_state)
        self.bowling_runs_entry.configure(state=bowling_state)
        self.bowling_wickets_entry.configure(state=bowling_state)

    def create_stat_videos(self):
        output_dir = self.output_dir_var.get().strip()
        if not output_dir:
            messagebox.showerror("Video Trimmer", "Please select an output folder.")
            return
        if shutil.which("ffmpeg") is None:
            messagebox.showerror(
                "Video Trimmer",
                "ffmpeg was not found on PATH. Install it from https://ffmpeg.org/download.html",
            )
            return

        name = self.stat_name_var.get().strip()
        if not name:
            messagebox.showerror("Video Trimmer", "Please enter a player name for the stat video.")
            return

        stat_type = self.stat_type_var.get()
        output_dir = os.path.abspath(output_dir)

        jobs = []  # list of (stat_text, output_filename)

        if stat_type in ("Batting", "All Round"):
            runs = self.stat_batting_runs_var.get().strip()
            balls = self.stat_batting_balls_var.get().strip()
            if not runs or not balls:
                messagebox.showerror(
                    "Video Trimmer", "Please enter both Runs and Balls for the batting stat."
                )
                return
            jobs.append((f"{runs}({balls})", f"{name}_batting_stat.mp4"))

        if stat_type in ("Bowling", "All Round"):
            overs = self.stat_bowling_overs_var.get().strip()
            maidens = self.stat_bowling_maidens_var.get().strip()
            bowling_runs = self.stat_bowling_runs_var.get().strip()
            wickets = self.stat_bowling_wickets_var.get().strip()
            if not overs or not maidens or not bowling_runs or not wickets:
                messagebox.showerror(
                    "Video Trimmer",
                    "Please enter Overs, Maidens, Runs, and Wickets for the bowling stat.",
                )
                return
            jobs.append((f"{overs}-{maidens}-{bowling_runs}-{wickets}", f"{name}_bowling_stat.mp4"))

        if not jobs:
            messagebox.showerror("Video Trimmer", "Nothing to create — check the Stat type selection.")
            return

        try:
            logo_path = self._selected_logo_path()
        except FileNotFoundError as e:
            messagebox.showerror("Video Trimmer", str(e))
            return

        successes = []
        errors = []
        for stat_text, filename in jobs:
            error = render_stat_card(name, stat_text, os.path.join(output_dir, filename), logo_path)
            if error is None:
                successes.append(filename)
            else:
                errors.append(f"{filename}: {error}")

        summary = f"Successfully created {len(successes)} stat video(s)."
        if errors:
            summary += "\n\nErrors:\n" + "\n".join(errors)
            messagebox.showwarning("Video Trimmer - Stat video(s) completed with errors", summary)
        else:
            messagebox.showinfo("Video Trimmer", summary)

    def _selected_logo_path(self):
        """Return the logo to use on stat cards, or None if 'Use logo' is off.
        Must be called from the main thread."""
        if not self.stat_use_logo_var.get():
            return None
        logo_path = self.stat_logo_path_var.get().strip()
        if not logo_path:
            return find_default_logo()  # e.g. "logo.png" next to the script, if present
        if not os.path.isfile(logo_path):
            raise FileNotFoundError(f"Logo file not found:\n{logo_path}")
        return os.path.abspath(logo_path)

    def _build_match_section(self):
        match_frame = ttk.LabelFrame(self.root, text="Match automation")
        match_frame.pack(fill="x", padx=10, pady=(6, 0))

        link_row = ttk.Frame(match_frame)
        link_row.pack(fill="x", padx=6, pady=(4, 2))
        ttk.Label(link_row, text="Scorecard link:").pack(side="left")
        self.scorecard_link_var = tk.StringVar()
        ttk.Entry(link_row, textvariable=self.scorecard_link_var).pack(
            side="left", fill="x", expand=True, padx=(6, 0)
        )
        ttk.Button(link_row, text="Open scorecard", command=self.open_scorecard).pack(
            side="left", padx=(6, 0)
        )
        ttk.Label(link_row, text="Our team:").pack(side="left", padx=(12, 0))
        self.our_team_var = tk.StringVar(value="Cheetahs")
        ttk.Entry(link_row, textvariable=self.our_team_var, width=14).pack(side="left", padx=(6, 0))
        ttk.Button(link_row, text="Swap innings", command=self.swap_innings).pack(
            side="left", padx=(6, 0)
        )

        ttk.Label(
            match_frame,
            text=(
                "Click 'Open scorecard', then on each innings press Ctrl+A and Ctrl+C. The app "
                "picks both up by itself. You can also paste them below."
            ),
            font=("TkDefaultFont", 8),
            foreground="#888888",
        ).pack(anchor="w", padx=6, pady=(0, 2))

        boxes = ttk.Frame(match_frame)
        boxes.pack(fill="x", padx=6)
        boxes.columnconfigure(0, weight=1)
        boxes.columnconfigure(1, weight=1)
        ttk.Label(boxes, text="Innings we batted:").grid(row=0, column=0, sticky="w")
        ttk.Label(boxes, text="Innings we bowled:").grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.match_batting_text = tk.Text(boxes, height=3, wrap="none")
        self.match_batting_text.grid(row=1, column=0, sticky="we")
        self.match_bowling_text = tk.Text(boxes, height=3, wrap="none")
        self.match_bowling_text.grid(row=1, column=1, sticky="we", padx=(6, 0))

        buttons = ttk.Frame(match_frame)
        buttons.pack(fill="x", padx=6, pady=6)
        self.find_highlights_button = ttk.Button(
            buttons, text="1. Find highlights", command=self.find_highlights
        )
        self.find_highlights_button.pack(side="left")
        self.generate_button = ttk.Button(
            buttons, text="2. Generate videos", command=self.generate_videos
        )
        self.generate_button.pack(side="left", padx=(6, 0))
        self.club_highlights_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            buttons,
            text="Also make combined match highlights",
            variable=self.club_highlights_var,
        ).pack(side="left", padx=(6, 0))
        self.check_all_cuts_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            buttons,
            text="Check every cut (also the clear ones)",
            variable=self.check_all_cuts_var,
        ).pack(side="left", padx=(16, 0))
        self.match_clock_var = tk.StringVar()  # kept so older saved states still load
        ttk.Label(buttons, textvariable=self.match_status_var, foreground="#555555").pack(
            side="left", padx=(16, 0)
        )

        more_buttons = ttk.Frame(match_frame)
        more_buttons.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(more_buttons, text="Trim every ball", command=self.trim_every_ball).pack(
            side="left"
        )
        ttk.Button(
            more_buttons, text="Ball queue (video only)", command=self.trim_ball_queue
        ).pack(side="left", padx=(24, 0))

    def open_scorecard(self):
        """Open the Ball by Ball page in the user's own browser, then watch the
        clipboard for the two innings. The site blocks scripts, so the page has
        to be opened and copied by the user."""
        try:
            url = match_automation.ball_by_ball_url(self.scorecard_link_var.get())
        except ValueError as e:
            messagebox.showerror("Video Trimmer", str(e))
            return
        webbrowser.open(url)
        try:
            self._clipboard_seen = self.root.clipboard_get()
        except tk.TclError:
            self._clipboard_seen = ""
        self._captured_innings = []
        self._capture_deadline = time.time() + CLIPBOARD_WATCH_SECONDS
        self.match_status_var.set("On the scorecard page, press Ctrl+A then Ctrl+C.")
        if not self._capture_active:
            self._capture_active = True
            self.root.after(CLIPBOARD_CHECK_MS, self._watch_clipboard)

    def _watch_clipboard(self):
        if time.time() > self._capture_deadline:
            self._capture_active = False
            self.match_status_var.set("Stopped waiting for the scorecard. Click 'Open scorecard' to retry.")
            return
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            text = ""
        if text != self._clipboard_seen:
            self._clipboard_seen = text
            self._take_copied_innings(text)
        if self._capture_active:
            self.root.after(CLIPBOARD_CHECK_MS, self._watch_clipboard)

    def _take_copied_innings(self, text):
        balls = match_automation.parse_ball_by_ball(text).balls
        if not balls:
            if len(text) > 300:  # a page copy, not a stray word or link
                self.match_status_var.set(
                    "That copy had no balls in it. Show the Ball by Ball tab, then Ctrl+A and Ctrl+C."
                )
            return
        if self._captured_innings:
            first = match_automation.parse_ball_by_ball(self._captured_innings[0]).balls[0]
            if first.minute_of_day == balls[0].minute_of_day:
                self.match_status_var.set("That's the same innings. Switch to the other one and copy it.")
                return
        self._captured_innings.append(text)
        if len(self._captured_innings) == 1:
            self.match_status_var.set("Got it. Switch to the other innings, then Ctrl+A and Ctrl+C.")
            return
        self._capture_active = False
        self._finish_scorecard()

    def _finish_scorecard(self):
        batting, bowling, problem = match_automation.assign_innings(
            self._captured_innings[0], self._captured_innings[1], self.our_team_var.get().strip()
        )
        for box, text in ((self.match_batting_text, batting), (self.match_bowling_text, bowling)):
            box.delete("1.0", "end")
            box.insert("1.0", text)
        self.match_status_var.set("Both innings loaded.")
        if problem:
            messagebox.showwarning("Video Trimmer", problem)
        video_path = self.input_path_var.get().strip()
        mismatch = self._wrong_match_message(video_path) if video_path else None
        if mismatch:
            messagebox.showwarning(
                "Video Trimmer - Wrong match?",
                mismatch + "\n\nPick this match's video as Input video, or clear Input "
                "video so the app looks this match's video up.",
            )
        self._get_match_video()

    def _get_match_video(self):
        """Use the local video if there is one, otherwise the Video URL, and
        otherwise look the match's video up from the scorecard."""
        if self.input_path_var.get().strip() or self.url_var.get().strip():
            self._download_unless_local()
            return
        first, second = self._captured_innings
        rosters = match_automation.parse_rosters(first) or match_automation.parse_rosters(second)
        date = match_automation.parse_match_date(first) or match_automation.parse_match_date(second)
        if len(rosters) != 2 or date is None:
            self._on_match_video_missing()
            return
        self.match_status_var.set("Looking for the match video...")
        threading.Thread(
            target=self._find_match_video_worker, args=(list(rosters), date), daemon=True
        ).start()

    def _empty_innings_message(self):
        if self._capture_active:
            return (
                "The app hasn't got the innings yet.\n\n"
                "On the scorecard page in your browser, show the first innings and press "
                "Ctrl+A then Ctrl+C. Then show the other innings and do the same. "
                "Both boxes fill in by themselves."
            )
        return (
            "Both innings boxes are empty.\n\n"
            "Click 'Open scorecard', then on that page press Ctrl+A then Ctrl+C for "
            "each innings. Or paste each innings into the boxes yourself."
        )

    def _find_match_video_worker(self, teams, date):
        try:
            found = find_match_video(teams, date)
        except Exception:
            found = None
        if found:
            self.root.after(0, lambda: self._on_match_video_found(*found))
        else:
            self.root.after(0, self._on_match_video_missing)

    def _on_match_video_found(self, url, title):
        self.url_var.set(url)
        self.match_status_var.set(f"Found: {title}")
        self._download_unless_local()

    def _on_match_video_missing(self):
        if self.input_path_var.get().strip():
            self.match_status_var.set(
                "Using your local video. Type its start time, or paste its Video URL."
            )
            return
        self.match_status_var.set("")
        messagebox.showinfo(
            "Video Trimmer",
            "Couldn't find this match's video. Paste its link into Video URL, "
            "or pick a local Input video.",
        )

    def _download_unless_local(self):
        if self.input_path_var.get().strip():
            self.match_status_var.set("Using your local video.")
        else:
            self.download_from_url()

    def swap_innings(self):
        batting = self.match_batting_text.get("1.0", "end-1c")
        bowling = self.match_bowling_text.get("1.0", "end-1c")
        self.match_batting_text.delete("1.0", "end")
        self.match_batting_text.insert("1.0", bowling)
        self.match_bowling_text.delete("1.0", "end")
        self.match_bowling_text.insert("1.0", batting)

    def _parse_match_boxes(self):
        """Read both paste boxes. Returns (batting, bowling, warnings); an
        innings is None if its box is empty. Must run on the main thread."""
        batting_text = self.match_batting_text.get("1.0", "end").strip()
        bowling_text = self.match_bowling_text.get("1.0", "end").strip()
        batting = match_automation.parse_ball_by_ball(batting_text) if batting_text else None
        bowling = match_automation.parse_ball_by_ball(bowling_text) if bowling_text else None
        warnings = []
        if batting:
            warnings += [f"Batting innings: {w}" for w in batting.warnings]
        if bowling:
            warnings += [f"Bowling innings: {w}" for w in bowling.warnings]
        if batting and bowling and batting.balls and bowling.balls:
            if batting.balls[0].minute_of_day == bowling.balls[0].minute_of_day:
                warnings.append("Both boxes look like the same innings.")
        return batting, bowling, warnings

    def _read_match_inputs(self):
        """Check what ball-finding needs. Returns (video_path, batting, bowling,
        warnings), or None after showing what's missing."""
        video_path = self.input_path_var.get().strip()
        if shutil.which("ffmpeg") is None:
            messagebox.showerror("Video Trimmer", "ffmpeg was not found on PATH.")
            return None
        if not video_path:
            messagebox.showerror(
                "Video Trimmer", "This needs the video on disk. Click 'Pre-download full video' first."
            )
            return None
        batting, bowling, warnings = self._parse_match_boxes()
        if not batting and not bowling:
            messagebox.showerror("Video Trimmer", self._empty_innings_message())
            return None
        mismatch = self._wrong_match_message(video_path)
        if mismatch and not messagebox.askyesno(
            "Video Trimmer - Wrong match?",
            mismatch + "\n\nClips cut from the wrong match land on the wrong balls. "
            "Carry on anyway?",
            icon="warning", default="no",
        ):
            return None
        return video_path, batting, bowling, warnings

    def _wrong_match_message(self, video_path):
        """Explain how the video's file name disagrees with the scorecard in
        the boxes, or return None if it agrees or the name doesn't say."""
        text = "\n".join(
            box.get("1.0", "end") for box in (self.match_batting_text, self.match_bowling_text)
        )
        teams = match_automation.parse_match_teams(text, self.scorecard_link_var.get())
        date = match_automation.parse_match_date(text)
        problems = match_automation.video_name_mismatch(video_path, teams, date)
        if not problems:
            return None
        what = " and ".join({"teams": "the teams", "date": "the date"}[p] for p in problems)
        scorecard = " vs ".join(teams) if teams else "this match"
        if date:
            scorecard += f", {date.strftime('%b')} {date.day}, {date.year}"
        return (
            f"The video and the scorecard look like different matches: {what} don't match.\n\n"
            f"Scorecard: {scorecard}\n"
            f"Video: {os.path.basename(video_path)}"
        )

    def find_highlights(self):
        inputs = self._read_match_inputs()
        if inputs is None:
            return
        self.find_highlights_button.configure(state="disabled")
        self.match_status_var.set("Reading the video details...")
        threading.Thread(target=self._find_highlights_worker, args=inputs, daemon=True).start()

    def _ball_queue(self, video_path):
        """Return (deliveries, duration): every delivery seen in the video.
        Scanned once per video and kept, since a full match takes a while.
        Safe on a background thread."""
        duration = ffprobe_duration(video_path)

        def progress(done, total):
            percent = min(100, int(100 * done / total))
            self.root.after(
                0, lambda: self.match_status_var.set(f"Watching the video for balls... {percent}%")
            )

        return load_or_scan_ball_queue(video_path, duration, progress), duration

    def _overs_moments(self, video_path, balls, changes, duration):
        """Which scoreboard change belongs to each ball, read from the overs
        number. Returns (None, ()) if the overs can't be read in this video,
        and the ball is then paired by time instead."""
        readings = _load_or_scan(
            video_path, "overs",
            lambda: [
                [when, width, list(bitmap) if bitmap else None]
                for when, width, bitmap in match_automation.read_overs_box(video_path)
            ],
        )
        readings = [(when, width, bytes(bitmap) if bitmap else None) for when, width, bitmap in readings]
        runs = match_automation.overs_runs(readings)
        legal = sum(1 for ball in balls if ball.legal)
        found, _ = match_automation.align_overs(balls, runs)
        if len(found) < legal * match_automation.MIN_OVERS_READ:
            return None, ()
        return match_automation.overs_moments(balls, runs, changes)

    def _locate_clips(self, video_path, batting, bowling, requests):
        """Find when each requested ball happened. Safe on a background thread.

        The on-screen scoreboard changes once after every ball, wherever the
        camera points, so it anchors each ball; the ball's path or the run-up
        and shot then place the clip inside that stretch. A video with no
        scoreboard falls back to the ball queue, found from movement alone."""
        def status(text):
            self.root.after(0, lambda: self.match_status_var.set(text))

        balls = match_automation.innings_in_match_order(batting, bowling)
        if not balls:
            raise ValueError("No balls could be read from the pasted text.")
        duration = ffprobe_duration(video_path)

        def board_progress(seconds_done):
            status(f"Reading the scoreboard... {min(100, int(100 * seconds_done / duration))}%")

        changes = load_or_scan_scoreboard(video_path, board_progress)
        if match_automation.has_scoreboard(balls, changes):
            status("Reading the overs on the scoreboard...")
            moments, unsure = self._overs_moments(video_path, balls, changes, duration)
            status("Matching balls with the scorecard...")
            clock_start = match_automation.fit_scoreboard_clock(balls, changes, duration)
            # For the review popup's Previous/Next ball: about when each ball was bowled.
            self._queue_times[video_path] = [
                change - match_automation.TYPICAL_BOARD_DELAY for change in changes
            ]

            def ball_finder(start, length):
                return match_automation.find_ball_candidates(video_path, start, length)

            def rise_finder(start, length):
                return match_automation.find_motion_rises(video_path, start, length)

            return match_automation.locate_clips(
                requests, balls, clock_start, changes, ball_finder, rise_finder,
                lambda done, total: status(f"Finding the ball... {done} of {total}"),
                moments, unsure,
            )

        queue, duration = self._ball_queue(video_path)
        self._queue_times[video_path] = [entry[0] for entry in queue]
        self.root.after(0, lambda: self.match_status_var.set("Matching balls with the scorecard..."))
        clock_start = match_automation.fit_queue_to_scorecard(balls, queue, duration)
        return match_automation.locate_queue_clips(requests, balls, queue, clock_start)

    def _find_highlights_worker(self, video_path, batting, bowling, warnings):
        try:
            highlights = self._locate_clips(
                video_path, batting, bowling,
                match_automation.highlight_requests(batting, bowling),
            )
        except Exception as e:
            message = str(e) or type(e).__name__
            self.root.after(0, lambda: self._on_highlights_failed(message))
            return
        self.root.after(0, lambda: self._on_highlights_found(highlights, warnings, video_path))

    def _on_highlights_failed(self, message):
        self.find_highlights_button.configure(state="normal")
        self.match_status_var.set("")
        messagebox.showerror("Video Trimmer", f"Could not find highlights:\n{message}")

    def _review_unsure(self, video_path, clips):
        """Show each clip whose ball wasn't seen and ask before keeping it, or
        every clip when 'Check every cut' is ticked. Returns the clips to cut;
        ones confirmed are marked exact."""
        check_all = self.check_all_cuts_var.get()
        total = sum(1 for clip in clips if check_all or not clip.exact)
        kept = []
        asking = True
        number = 0
        for clip in clips:
            if (clip.exact and not check_all) or not asking:
                kept.append(clip)
                continue
            number += 1
            self.match_status_var.set(f"Checking ball {number} of {total}...")
            dialog = BallReviewDialog(
                self.root, video_path, clip, number, total, self._queue_times.get(video_path, ())
            )
            if dialog.result == "skip":
                continue
            if dialog.result == "yes":
                clip.start, clip.end, clip.exact = dialog.start, dialog.end, True
            else:
                asking = False
            kept.append(clip)
        self.match_status_var.set("")
        return kept

    def _on_highlights_found(self, highlights, warnings, video_path):
        self.find_highlights_button.configure(state="normal")
        self.match_status_var.set("")
        # Number every highlight before the review, so leaving a ball out
        # doesn't rename the clips after it onto files that already exist.
        names = {}
        counters = {}
        for item in sorted(highlights, key=lambda h: h.player):
            counters[item.player] = counters.get(item.player, 0) + 1
            names[id(item)] = f"{item.player}_{counters[item.player]}.mp4"
        highlights = self._review_unsure(video_path, highlights)

        # Replace the last Find highlights' rows, and any row (say, one
        # restored by Reload script) that has a name this run uses.
        new_names = set(names.values())
        for row in [
            r for r in self.trim_rows
            if r.is_auto or r.get_values()[0] in new_names or r.get_values()[0] == "output.mp4"
        ]:
            row.destroy()
            self.trim_rows.remove(row)

        unsure = []
        for item in sorted(highlights, key=lambda h: h.player):
            filename = names[id(item)]
            self.add_trim_row(
                filename=filename,
                start=seconds_to_time_str(item.start),
                end=seconds_to_time_str(item.end),
                watermark=item.watermark,
            ).is_auto = True
            if not item.exact:
                unsure.append(f"{filename} (over {item.over_label})")
        if not self.trim_rows:
            self.add_trim_row()

        summary = f"Found {len(highlights)} highlight(s). Review the trims, then export the reels."
        if unsure:
            summary += (
                "\n\nThe ball wasn't clear in these clips, so check "
                "their Start and End:\n" + "\n".join(unsure)
            )
        if warnings:
            summary += "\n\nCheck these:\n" + "\n".join(warnings)
            messagebox.showwarning("Video Trimmer - Highlights", summary)
        else:
            messagebox.showinfo("Video Trimmer", summary)

    def generate_videos(self):
        """Make every video from the Trims rows: stat cards, one reel per
        player, the batting and bowling highlights, and the combined match
        highlights if ticked. Anything already made from the same rows and
        times is kept, so after editing a row only that clip and the videos
        using it are redone."""
        input_path = self.input_path_var.get().strip()
        output_dir = self.output_dir_var.get().strip()
        if not input_path or not output_dir:
            messagebox.showerror("Video Trimmer", "Please select the input video and output folder.")
            return
        if self._export_in_progress:
            messagebox.showinfo("Video Trimmer", "Videos are already being made.")
            return
        if self._refuse_duplicates():
            return

        batting, bowling, warnings = self._parse_match_boxes()
        if not batting and not bowling:
            messagebox.showerror("Video Trimmer", self._empty_innings_message())
            return
        if not any(row.is_auto for row in self.trim_rows):
            messagebox.showerror(
                "Video Trimmer",
                "There are no highlight clips yet.\n\nClick '1. Find highlights' first. "
                "It fills the trim rows the videos are made from. Check them, "
                "then click '2. Generate videos'.",
            )
            return
        try:
            logo_path = self._selected_logo_path()
        except FileNotFoundError as e:
            messagebox.showerror("Video Trimmer", str(e))
            return

        cards = {}
        if batting:
            for player, s in match_automation.batting_stats(batting).items():
                cards.setdefault(player, []).append(
                    (f"{s['runs']}({s['balls']})", f"{player}_batting_stat.mp4")
                )
        if bowling:
            for player, s in match_automation.bowling_stats(bowling).items():
                cards.setdefault(player, []).append(
                    (
                        f"{s['overs']}-{s['maidens']}-{s['runs']}-{s['wickets']}",
                        f"{player}_bowling_stat.mp4",
                    )
                )

        valid_rows, errors = self._collect_valid_rows()
        valid_rows = [row for row in valid_rows if row[1] != "output.mp4"]
        self._export_in_progress = True
        self.generate_button.configure(state="disabled")
        self.match_status_var.set("Getting the videos ready...")
        threading.Thread(
            target=self._generate_videos_worker,
            args=(
                input_path, os.path.abspath(output_dir), valid_rows, cards, logo_path,
                self.club_highlights_var.get(), errors + warnings,
            ),
            daemon=True,
        ).start()

    def _generate_videos_worker(self, input_path, output_dir, valid_rows, cards, logo_path, combined, errors):
        def status(text):
            self.root.after(0, lambda: self.match_status_var.set(text))

        def path(name):
            return os.path.join(output_dir, name)

        record = load_cut_record(output_dir)
        made, kept, recut = [], [], []
        try:
            # Clips: only the ones that are new or whose times changed.
            changed = changed_rows(output_dir, input_path, valid_rows, record)
            if changed:
                status(f"Cutting {len(changed)} clip(s)...")
                successes, trim_errors = self._run_trim_jobs(input_path, output_dir, changed, True)
                errors += trim_errors
                for row in changed:
                    if row[1] in successes:
                        record[row[1]] = _cut_signature(input_path, row)
                        recut.append(row[1])

            # Stat cards: only the ones that are new or say something different.
            for player, player_cards in cards.items():
                for stat_text, filename in player_cards:
                    signature = [player, stat_text, logo_path or ""]
                    if record.get("card:" + filename) == signature and os.path.exists(path(filename)):
                        continue
                    status(f"Creating {filename}...")
                    error = render_stat_card(player, stat_text, path(filename), logo_path)
                    if error is None:
                        record["card:" + filename] = signature
                    else:
                        errors.append(f"{filename}: {error}")

            def join(name, parts):
                """Join these files into `name`, unless it was already made
                from exactly the same pieces cut the same way."""
                parts = [p for p in dict.fromkeys(parts) if os.path.exists(path(p))]
                if not parts:
                    return
                signature = [[p, record.get(p) or record.get("card:" + p)] for p in parts]
                if record.get("join:" + name) == signature and os.path.exists(path(name)):
                    kept.append(name)
                    return
                status(f"Making {name}...")
                error = self._join_clips([path(p) for p in parts], path(name))
                if error is None:
                    record["join:" + name] = signature
                    made.append(name)
                else:
                    errors.append(f"{name}: {error}")

            # One reel per player: their clips in Trims order, then their stat cards.
            for player in dict.fromkeys(player_of_clip(row[1]) for row in valid_rows):
                if player is None:
                    continue
                clips = [row[1] for row in valid_rows if player_of_clip(row[1]) == player]
                join(f"{player}_reel.mp4", clips + [name for _, name in cards.get(player, [])])

            # Team highlights in the order things happened. W rows are bowling.
            in_order = [row[1] for row in sorted(valid_rows, key=lambda row: row[5])]
            mark = {row[1]: row[4] for row in valid_rows}
            join("Batting_highlights.mp4", [n for n in in_order if mark[n] != "W"])
            join("Bowling_highlights.mp4", [n for n in in_order if mark[n] == "W"])
            if combined:
                join("Match_highlights.mp4", in_order)
        except Exception as e:
            errors.append(str(e) or type(e).__name__)
        finally:
            write_cut_record(output_dir, record)

        summary = f"Made {len(made)} video(s)"
        summary += f", and {len(kept)} were already up to date." if kept else "."
        if made:
            summary += "\n\nMade:\n" + "\n".join(made)
        if recut:
            summary += "\n\nClips cut: " + ", ".join(recut)

        def done():
            self.generate_button.configure(state="normal")
            self._on_long_job_done(summary, errors)

        self.root.after(0, done)

    def _join_clips(self, paths, output_path):
        """Join clips into one video. Returns ffmpeg's error text, or None."""
        segments = [
            {"path": p, "duration": ffprobe_duration(p), "has_audio": ffprobe_has_audio(p)}
            for p in paths
        ]
        cmd = match_automation.build_reel_cmd(segments, output_path)
        result = subprocess.run(cmd, capture_output=True, text=True)
        return None if result.returncode == 0 else result.stderr[-300:]

    def _on_long_job_done(self, summary, problems):
        self._export_in_progress = False
        self.match_status_var.set("")
        if problems:
            summary += "\n\nCheck these:\n" + "\n".join(problems)
            messagebox.showwarning("Video Trimmer", summary)
        else:
            messagebox.showinfo("Video Trimmer", summary)

    def trim_every_ball(self):
        output_dir = self.output_dir_var.get().strip()
        if not output_dir:
            messagebox.showerror("Video Trimmer", "Please select an output folder.")
            return
        if self._export_in_progress:
            messagebox.showinfo("Video Trimmer", "An export is already running.")
            return
        inputs = self._read_match_inputs()
        if inputs is None:
            return
        self._export_in_progress = True
        self.match_status_var.set("Reading the video details...")
        threading.Thread(
            target=self._trim_every_ball_worker,
            args=inputs + (os.path.abspath(output_dir),),
            daemon=True,
        ).start()

    def _trim_every_ball_worker(self, video_path, batting, bowling, warnings, output_dir):
        try:
            clips = self._locate_clips(
                video_path, batting, bowling,
                match_automation.every_ball_requests(batting, bowling),
            )
        except Exception as e:
            message = str(e) or type(e).__name__
            self.root.after(0, lambda: self._on_long_job_done("No ball clips were made.", [message]))
            return
        self.root.after(0, lambda: self._review_then_cut_every_ball(video_path, clips, warnings, output_dir))

    def _review_then_cut_every_ball(self, video_path, clips, warnings, output_dir):
        clips = self._review_unsure(video_path, clips)
        threading.Thread(
            target=self._cut_every_ball_worker,
            args=(video_path, clips, warnings, output_dir),
            daemon=True,
        ).start()

    def _cut_every_ball_worker(self, video_path, clips, warnings, output_dir):
        problems = list(warnings)
        created = 0
        unclear = []
        try:
            numbers = {}
            for done, clip in enumerate(clips, start=1):
                self.root.after(
                    0,
                    lambda d=done: self.match_status_var.set(f"Cutting ball {d} of {len(clips)}..."),
                )
                folder = os.path.join(output_dir, f"All balls - {clip.group}")
                os.makedirs(folder, exist_ok=True)
                numbers[clip.group] = numbers.get(clip.group, 0) + 1
                filename = f"{numbers[clip.group]:03d} Over {clip.over_label} {clip.player}.mp4"
                row = (
                    done, filename, seconds_to_time_str(clip.start), seconds_to_time_str(clip.end),
                    clip.watermark, clip.start, clip.end, clip.end - clip.start,
                )
                successes, errors = self._run_trim_jobs(video_path, folder, [row], True)
                created += len(successes)
                problems += errors
                if not clip.exact:
                    unclear.append(f"{clip.group}\\{filename}")

            if unclear:
                list_path = os.path.join(output_dir, "All balls - check these.txt")
                with open(list_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(unclear) + "\n")
        except Exception as e:
            problems.append(str(e) or type(e).__name__)

        summary = f"Created {created} ball clip(s)."
        if unclear:
            summary += f"\n\nThe ball wasn't clear in {len(unclear)}. They're listed in 'All balls - check these.txt'."
        self.root.after(0, lambda: self._on_long_job_done(summary, problems))

    def trim_ball_queue(self):
        """Cut every delivery seen in the video, 2 seconds either side, in
        order, without the scorecard."""
        video_path = self.input_path_var.get().strip()
        output_dir = self.output_dir_var.get().strip()
        if not video_path or not output_dir:
            messagebox.showerror("Video Trimmer", "Please select the input video and output folder.")
            return
        if self._export_in_progress:
            messagebox.showinfo("Video Trimmer", "An export is already running.")
            return
        self._export_in_progress = True
        self.match_status_var.set("Reading the video...")
        threading.Thread(
            target=self._trim_ball_queue_worker,
            args=(video_path, os.path.abspath(output_dir)),
            daemon=True,
        ).start()

    def _trim_ball_queue_worker(self, video_path, output_dir):
        problems = []
        created = 0
        deliveries = []
        folder = os.path.join(output_dir, "Ball queue")
        try:
            deliveries, _ = self._ball_queue(video_path)
            os.makedirs(folder, exist_ok=True)
            before, after = match_automation.BALL_WINDOW
            for number, (moment, sure, *_) in enumerate(deliveries, start=1):
                self.root.after(
                    0,
                    lambda n=number: self.match_status_var.set(
                        f"Cutting ball {n} of {len(deliveries)}..."
                    ),
                )
                start = max(moment - before, 0)
                found_by = "" if sure else " (movement only)"
                filename = f"{number:03d} at {seconds_to_time_str(moment)[:8].replace(':', '-')}{found_by}.mp4"
                row = (
                    number, filename, seconds_to_time_str(start),
                    seconds_to_time_str(moment + after), "None", start, moment + after,
                    moment + after - start,
                )
                successes, errors = self._run_trim_jobs(video_path, folder, [row], True)
                created += len(successes)
                problems += errors
        except Exception as e:
            problems.append(str(e) or type(e).__name__)
        summary = (
            f"Found {len(deliveries)} balls in the video and cut {created} clip(s) into "
            f"'{folder}'.\n\nFind highlights and Trim every ball now match these "
            "with the scorecard."
        )
        self.root.after(0, lambda: self._on_long_job_done(summary, problems))

    def browse_input(self):
        path = filedialog.askopenfilename(
            title="Select input video",
            filetypes=[("Video files", "*.mp4 *.mov *.mkv *.avi *.webm"), ("All files", "*.*")],
        )
        if path:
            self.input_path_var.set(path)

    def browse_output_dir(self):
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.output_dir_var.set(path)

    def download_from_url(self):
        """Pre-download the *entire* video from the URL field and fill it
        into the Input video field, for people who'd rather trim from a
        local copy. Not required — Export all can trim straight from the
        URL instead (see _export_all_from_url)."""
        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("Video Trimmer", "Please paste a video URL first.")
            return

        if not YTDLP_AVAILABLE:
            messagebox.showerror(
                "Video Trimmer",
                "yt-dlp is not installed.\n\nInstall it with:\npip install -U yt-dlp",
            )
            return

        if shutil.which("ffmpeg") is None:
            messagebox.showerror(
                "Video Trimmer",
                "ffmpeg was not found on PATH. Install it from https://ffmpeg.org/download.html",
            )
            return

        # Default download location: the output folder if set, else a "downloads" subfolder.
        download_dir = self.output_dir_var.get().strip() or os.path.join(os.getcwd(), "downloads")
        os.makedirs(download_dir, exist_ok=True)

        self.download_status_var.set("Downloading... this may take a while.")
        threading.Thread(
            target=self._download_worker, args=(url, download_dir), daemon=True
        ).start()

    def _download_worker(self, url, download_dir):
        result_path = {"path": None}

        def hook(d):
            if d.get("status") == "downloading":
                percent = d.get("_percent_str", "").strip()
                self.root.after(0, lambda: self.download_status_var.set(f"Downloading... {percent}"))
            elif d.get("status") == "finished":
                self.root.after(0, lambda: self.download_status_var.set("Processing download..."))

        ydl_opts = {
            "format": "bestvideo+bestaudio/best",
            "outtmpl": os.path.join(download_dir, "%(title)s.%(ext)s"),
            "merge_output_format": "mp4",
            "noplaylist": True,
            "progress_hooks": [hook],
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                result_path["path"] = ydl.prepare_filename(info)
                # prepare_filename may report the pre-merge extension; fix to .mp4 if merged.
                base, _ext = os.path.splitext(result_path["path"])
                merged_path = base + ".mp4"
                if os.path.exists(merged_path):
                    result_path["path"] = merged_path
        except Exception as e:
            self.root.after(0, lambda: self._on_download_error(str(e)))
            return

        self.root.after(0, lambda: self._on_download_success(result_path["path"]))

    def _on_download_success(self, path):
        self.download_status_var.set(f"Downloaded: {os.path.basename(path)}")
        self.input_path_var.set(path)
        messagebox.showinfo("Video Trimmer", f"Download complete:\n{path}")

    def _on_download_error(self, error_message):
        self.download_status_var.set("Download failed.")
        messagebox.showerror("Video Trimmer", f"Download failed:\n{error_message[-400:]}")

    def add_trim_row(self, filename=None, start=None, end=None, watermark=None):
        kwargs = {}
        if filename is not None:
            kwargs["filename"] = filename
        if start is not None:
            kwargs["start"] = start
        if end is not None:
            kwargs["end"] = end
        if watermark is not None:
            kwargs["watermark"] = watermark
        row = TrimRow(self.rows_frame, self.remove_trim_row, **kwargs)
        row.frame.pack(fill="x", pady=4)
        self.trim_rows.append(row)
        return row

    def load_bulk_file(self):
        path = filedialog.askopenfilename(
            title="Select trims text file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            messagebox.showerror("Video Trimmer", f"Could not read file:\n{e}")
            return
        self.bulk_text.delete("1.0", "end")
        self.bulk_text.insert("1.0", content)

    def add_rows_from_bulk_text(self):
        text = self.bulk_text.get("1.0", "end")
        rows, errors = parse_bulk_trim_text(text)

        for filename, start, end, watermark in rows:
            self.add_trim_row(filename=filename, start=start, end=end, watermark=watermark)

        summary = f"Added {len(rows)} trim row(s) from the bulk text."
        if errors:
            summary += "\n\nSome lines couldn't be parsed:\n" + "\n".join(errors)
            messagebox.showwarning("Video Trimmer - Bulk import", summary)
        elif rows:
            messagebox.showinfo("Video Trimmer", summary)
        else:
            messagebox.showerror(
                "Video Trimmer", "No rows were parsed. Check the format and try again."
            )

    def remove_trim_row(self, row):
        if len(self.trim_rows) == 1:
            messagebox.showinfo("Video Trimmer", "At least one trim row is required.")
            return
        row.destroy()
        self.trim_rows.remove(row)

    def _duplicate_names(self):
        """Filenames used by more than one Trims row. Two rows with one name
        cut over each other's file, so exports refuse to run until fixed."""
        seen, repeated = set(), []
        for row in self.trim_rows:
            name = row.get_values()[0]
            if name in seen and name not in repeated:
                repeated.append(name)
            seen.add(name)
        return repeated

    def _refuse_duplicates(self):
        # Rows that repeat another row exactly (same name, times and
        # watermark) are just extra copies: drop them quietly.
        seen = set()
        for row in list(self.trim_rows):
            values = row.get_values()
            if values in seen and len(self.trim_rows) > 1:
                row.destroy()
                self.trim_rows.remove(row)
            seen.add(values)
        repeated = self._duplicate_names()
        if not repeated:
            return False
        messagebox.showerror(
            "Video Trimmer",
            "These names are used by more than one Trims row, so their clips would "
            "overwrite each other:\n\n" + "\n".join(repeated) +
            "\n\nRemove the extra rows, or click '1. Find highlights' again to "
            "replace them.",
        )
        return True

    def _collect_valid_rows(self):
        """Read every trim row's fields, validate them, and return
        (valid_rows, errors). valid_rows is a list of
        (filename, start, end, watermark_choice, start_sec, end_sec,
        duration_sec) tuples. Must be called from the main thread, since it
        touches Tk StringVars."""
        valid_rows = []
        errors = []
        for i, row in enumerate(self.trim_rows, start=1):
            filename, start, end, watermark_choice = row.get_values()
            if not filename or not start or not end:
                errors.append(f"Trim {i}: missing filename, start, or end.")
                continue
            try:
                start_sec = time_str_to_seconds(start)
                end_sec = time_str_to_seconds(end)
            except ValueError:
                errors.append(f"Trim {i} ({filename}): could not parse start/end time.")
                continue
            duration_sec = end_sec - start_sec
            if duration_sec <= 0:
                errors.append(f"Trim {i} ({filename}): end time must be after start time.")
                continue
            valid_rows.append((i, filename, start, end, watermark_choice, start_sec, end_sec, duration_sec))
        return valid_rows, errors

    def export_all(self):
        if self._export_in_progress:
            messagebox.showinfo("Video Trimmer", "An export is already running — please wait for it to finish.")
            return
        if self._refuse_duplicates():
            return

        input_path = self.input_path_var.get().strip()
        url = self.url_var.get().strip()
        output_dir = self.output_dir_var.get().strip()

        if not output_dir:
            messagebox.showerror("Video Trimmer", "Please select an output folder.")
            return
        if shutil.which("ffmpeg") is None:
            messagebox.showerror(
                "Video Trimmer",
                "ffmpeg was not found on PATH. Install it from https://ffmpeg.org/download.html",
            )
            return

        if input_path:
            self._export_all_from_local_file(input_path, output_dir)
        elif url:
            if not YTDLP_AVAILABLE:
                messagebox.showerror(
                    "Video Trimmer",
                    "yt-dlp is not installed, so trims can't be cut directly from a URL.\n\n"
                    "Install it with:\npip install -U yt-dlp",
                )
                return
            self._export_all_from_url(url, output_dir)
        else:
            messagebox.showerror(
                "Video Trimmer", "Please select an input video, or paste a video URL to trim from."
            )

    def _run_trim_jobs(self, input_path, output_dir, valid_rows, reencode):
        """Run ffmpeg once per row in valid_rows, trimming input_path into
        output_dir. Pure logic, no Tkinter calls — safe to call from a
        background thread. Returns (successes, errors), lists of filenames
        and human-readable error strings respectively."""
        errors = []
        successes = []
        input_path = os.path.abspath(input_path)
        output_dir = os.path.abspath(output_dir)
        font_filename, font_dir = get_watermark_font_and_cwd()

        for i, filename, start, end, watermark_choice, start_sec, end_sec, duration_sec in valid_rows:
            output_path = os.path.join(output_dir, filename)
            use_watermark = bool(watermark_choice) and watermark_choice != "None"

            # Using -t (duration) instead of -to avoids a well-known ffmpeg quirk
            # where -to is sometimes measured from the seek point rather than
            # the original timeline, producing an unexpectedly long/short clip.
            cmd = ["ffmpeg", "-y", "-ss", start, "-i", input_path, "-t", seconds_to_time_str(duration_sec)]
            run_cwd = None
            if use_watermark:
                # A watermark requires decoding/re-encoding every frame, so
                # -c copy isn't an option here regardless of the checkbox.
                # Running with cwd set to the font's own folder lets us pass
                # ffmpeg a bare filename for fontfile, avoiding drive-letter
                # colons and backslashes inside the filter string entirely
                # (both are special characters in ffmpeg's filtergraph syntax
                # and are a common source of cryptic "Invalid argument"
                # errors on Windows if a full path is embedded there instead).
                cmd += ["-vf", build_watermark_filter(watermark_choice, font_filename), "-c:v", "libx264", "-c:a", "aac"]
                run_cwd = font_dir
            elif reencode:
                cmd += ["-c:v", "libx264", "-c:a", "aac"]
            else:
                cmd += ["-c", "copy"]
            # Move the mp4 index (moov atom) to the front of the file. This
            # is always safe (doesn't affect duration/content, doesn't
            # require re-encoding) and improves compatibility with some
            # stricter players.
            cmd += ["-movflags", "+faststart"]
            cmd.append(output_path)

            result = subprocess.run(cmd, capture_output=True, text=True, cwd=run_cwd)
            if result.returncode == 0:
                successes.append(filename)
            else:
                errors.append(f"Trim {i} ({filename}): {result.stderr[-300:]}")

        return successes, errors

    def _stitch_if_requested(self, output_dir, successes):
        """If the stitch checkbox is on and there are at least 2 successful
        clips, concatenate them (in trim-row order, with a short crossfade
        between each) into one combined file. Returns a one-line status
        string to append to the summary, or None if stitching wasn't
        requested. Pure logic — safe to call from a background thread."""
        if not self.stitch_var.get():
            return None

        if len(successes) < 2:
            return "Stitching skipped: need at least 2 successfully created clips."

        stitch_filename = self.stitch_filename_var.get().strip() or "stitched.mp4"
        if not os.path.splitext(stitch_filename)[1]:
            stitch_filename += ".mp4"

        output_dir = os.path.abspath(output_dir)
        clip_paths = [os.path.join(output_dir, f) for f in successes]
        stitched_path = os.path.join(output_dir, stitch_filename)

        try:
            cmd = build_crossfade_stitch_cmd(clip_paths, stitched_path, transition_seconds=0.2)
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                return f"Stitched video created: {stitch_filename}"
            return f"Stitching failed: {result.stderr[-300:]}"
        except Exception as e:
            return f"Stitching failed: {e}"

    def _export_all_from_local_file(self, input_path, output_dir):
        reencode = self.reencode_var.get()
        valid_rows, errors = self._collect_valid_rows()

        successes, trim_errors = self._run_trim_jobs(input_path, output_dir, valid_rows, reencode)
        errors = errors + trim_errors

        stitch_message = self._stitch_if_requested(output_dir, successes)

        summary = f"Successfully created {len(successes)} file(s)."
        if stitch_message:
            summary += f"\n\n{stitch_message}"
        if errors:
            summary += "\n\nErrors:\n" + "\n".join(errors)
            messagebox.showwarning("Video Trimmer - Completed with errors", summary)
        else:
            messagebox.showinfo("Video Trimmer", summary)

    def _export_all_from_url(self, url, output_dir):
        """Download the full source video from the URL once to a temp file,
        then trim every row out of that local copy using the same reliable
        pipeline as a local file, and finally delete the temp file.

        (An earlier version tried to download only each clip's range
        directly via yt-dlp's download_ranges/force_keyframes_at_cuts. That
        relies on yt-dlp running its own separate ffmpeg process to pre-cut
        keyframes, which proved to crash unpredictably on some systems.
        Downloading once and reusing the proven local-trim code is slower
        for a single export but far more reliable.)"""
        valid_rows, errors = self._collect_valid_rows()
        if not valid_rows and not errors:
            messagebox.showerror("Video Trimmer", "Add at least one trim row first.")
            return

        reencode = self.reencode_var.get()
        output_dir = os.path.abspath(output_dir)

        self._export_in_progress = True
        self.export_button.configure(state="disabled")
        self.download_status_var.set("Downloading source video...")
        threading.Thread(
            target=self._export_from_url_worker,
            args=(url, output_dir, valid_rows, list(errors), reencode),
            daemon=True,
        ).start()

    def _export_from_url_worker(self, url, output_dir, valid_rows, errors, reencode):
        temp_dir = tempfile.mkdtemp(prefix="video_trimmer_")

        def hook(d):
            if d.get("status") == "downloading":
                percent = d.get("_percent_str", "").strip()
                self.root.after(
                    0, lambda: self.download_status_var.set(f"Downloading source video... {percent}")
                )
            elif d.get("status") == "finished":
                self.root.after(0, lambda: self.download_status_var.set("Processing download..."))

        # A custom logger instead of quiet/no_warnings: those options
        # suppress yt-dlp's console output entirely, which meant a failed
        # download surfaced only as a bare, unhelpful exception (sometimes
        # literally the string "None"). Capturing warning()/error() here
        # instead gets us yt-dlp's actual diagnostic message to show the user.
        log_messages = []

        class _YdlLogger:
            def debug(self, msg):
                pass

            def info(self, msg):
                pass

            def warning(self, msg):
                log_messages.append(f"WARNING: {msg}")

            def error(self, msg):
                log_messages.append(f"ERROR: {msg}")

        ydl_opts = {
            "format": "bestvideo+bestaudio/best",
            "outtmpl": os.path.join(temp_dir, "%(title)s.%(ext)s"),
            "merge_output_format": "mp4",
            "noplaylist": True,
            "progress_hooks": [hook],
            "logger": _YdlLogger(),
        }

        temp_video_path = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                temp_video_path = ydl.prepare_filename(info)
                base, _ext = os.path.splitext(temp_video_path)
                merged_path = base + ".mp4"
                if os.path.exists(merged_path):
                    temp_video_path = merged_path
        except Exception as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            detail = str(e).strip()
            if not detail or detail == "None":
                detail = "\n".join(log_messages[-5:]) or f"{type(e).__name__} (no further detail available)"
            self.root.after(
                0,
                lambda: self._on_export_from_url_done(
                    [], errors + [f"Could not download source video:\n{detail}"]
                ),
            )
            return

        self.root.after(0, lambda: self.download_status_var.set("Trimming clips..."))

        successes, trim_errors = self._run_trim_jobs(temp_video_path, output_dir, valid_rows, reencode)
        all_errors = errors + trim_errors

        shutil.rmtree(temp_dir, ignore_errors=True)

        stitch_message = self._stitch_if_requested(output_dir, successes)

        self.root.after(0, lambda: self._on_export_from_url_done(successes, all_errors, stitch_message))

    def _on_export_from_url_done(self, successes, errors, stitch_message=None):
        self._export_in_progress = False
        self.export_button.configure(state="normal")
        self.download_status_var.set("")
        summary = f"Successfully created {len(successes)} file(s) from the URL."
        if stitch_message:
            summary += f"\n\n{stitch_message}"
        if errors:
            summary += "\n\nErrors:\n" + "\n".join(errors)
            messagebox.showwarning("Video Trimmer - Completed with errors", summary)
        else:
            messagebox.showinfo("Video Trimmer", summary)


def main():
    root = tk.Tk()
    app = VideoTrimmerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()