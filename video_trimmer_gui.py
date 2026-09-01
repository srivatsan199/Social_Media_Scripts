#!/usr/bin/env python3
"""
video_trimmer_gui.py - GUI for trimming multiple clips out of one video using ffmpeg.

Requires ffmpeg to be installed and available on PATH:
  https://ffmpeg.org/download.html

Run:
  python video_trimmer_gui.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False


# Where GUI state is temporarily stashed across a "Reload script" restart, so
# the running process can pick up edits saved to this .py file on disk
# without losing whatever the user has already typed into the GUI.
RELOAD_STATE_PATH = os.path.join(tempfile.gettempdir(), "video_trimmer_gui_reload_state.json")


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


def parse_bulk_trim_text(text):
    """Parse the bulk-entry format:

        Name:
        start_1-end_1
        start_2-end_2
        Name 2:
        start_1-end_1

    A line ending in ':' starts a new named group (the trailing colon is
    stripped to get the name). Any other non-blank line is treated as a
    "start-end" pair for the current group, split on the first '-'
    (start/end times can freely contain ':', e.g. "1:55:17-1:55:23").

    Returns (rows, errors) where rows is a list of
    (output_filename, start, end) tuples with filenames like
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

        if "-" not in line:
            errors.append(f"Line {lineno}: expected 'start-end' but found '{line}'.")
            continue

        start, end = line.split("-", 1)
        start = start.strip()
        end = end.strip()
        if not start or not end:
            errors.append(f"Line {lineno}: expected 'start-end' but found '{line}'.")
            continue

        counters[current_name] += 1
        filename = f"{current_name}_{counters[current_name]}.mp4"
        rows.append((filename, start, end))

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

        ttk.Label(self.frame, text="Output filename").grid(row=0, column=0, sticky="w")
        ttk.Label(self.frame, text="Start (HH:MM:SS)").grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Label(self.frame, text="End (HH:MM:SS)").grid(row=0, column=2, sticky="w", padx=(10, 0))
        ttk.Label(self.frame, text="Watermark").grid(row=0, column=3, sticky="w", padx=(10, 0))

        self.filename_var = tk.StringVar(value=filename)
        self.start_var = tk.StringVar(value=start)
        self.end_var = tk.StringVar(value=end)
        self.watermark_var = tk.StringVar(value=watermark if watermark in WATERMARK_CHOICES else "None")

        ttk.Entry(self.frame, textvariable=self.filename_var, width=24).grid(row=1, column=0, sticky="we")
        ttk.Entry(self.frame, textvariable=self.start_var, width=12).grid(row=1, column=1, sticky="we", padx=(10, 0))
        ttk.Entry(self.frame, textvariable=self.end_var, width=12).grid(row=1, column=2, sticky="we", padx=(10, 0))
        ttk.Combobox(
            self.frame,
            textvariable=self.watermark_var,
            values=WATERMARK_CHOICES,
            state="readonly",
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

        self.trim_rows = []

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

        rows = state.get("rows") or []
        if rows:
            for r in rows:
                self.add_trim_row(
                    filename=r.get("filename"),
                    start=r.get("start"),
                    end=r.get("end"),
                    watermark=r.get("watermark"),
                )
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
            "rows": [
                {
                    "filename": filename,
                    "start": start,
                    "end": end,
                    "watermark": watermark,
                }
                for filename, start, end, watermark in (row.get_values() for row in self.trim_rows)
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

        # Video URL downloader
        url_frame = ttk.Frame(self.root)
        url_frame.pack(fill="x", **pad)
        ttk.Label(url_frame, text="Video URL (optional):").pack(anchor="w")
        row_url = ttk.Frame(url_frame)
        row_url.pack(fill="x")
        ttk.Entry(row_url, textvariable=self.url_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row_url, text="Download", command=self.download_from_url).pack(
            side="left", padx=(6, 0)
        )
        ttk.Label(
            url_frame,
            textvariable=self.download_status_var,
            foreground="#555555",
        ).pack(anchor="w", pady=(2, 0))
        ttk.Label(
            url_frame,
            text="Only download videos you own or have permission/rights to download.",
            font=("TkDefaultFont", 8),
            foreground="#888888",
        ).pack(anchor="w")

        # Input video picker
        input_frame = ttk.Frame(self.root)
        input_frame.pack(fill="x", **pad)
        ttk.Label(input_frame, text="Input video:").pack(anchor="w")
        row = ttk.Frame(input_frame)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.input_path_var, state="readonly").pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(row, text="Browse", command=self.browse_input).pack(side="left", padx=(6, 0))

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
        ttk.Button(add_export_row, text="Export all", command=self.export_all).pack(side="right")

        # Frame-accurate toggle
        ttk.Checkbutton(
            self.root,
            text="Frame-accurate (slower, re-encodes video)",
            variable=self.reencode_var,
        ).pack(anchor="w", padx=10, pady=(0, 10))

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
        font_filename, font_dir = get_watermark_font_and_cwd()

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

        logo_path = None
        if self.stat_use_logo_var.get():
            logo_path = self.stat_logo_path_var.get().strip()
            if logo_path and not os.path.isfile(logo_path):
                messagebox.showerror("Video Trimmer", f"Logo file not found:\n{logo_path}")
                return
            if logo_path:
                logo_path = os.path.abspath(logo_path)
            else:
                logo_path = find_default_logo()  # e.g. "logo.png" next to the script, if present

        successes = []
        errors = []
        for stat_text, filename in jobs:
            output_path = os.path.join(output_dir, filename)
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
            if result.returncode == 0:
                successes.append(filename)
            else:
                errors.append(f"{filename}: {result.stderr[-300:]}")

        summary = f"Successfully created {len(successes)} stat video(s)."
        if errors:
            summary += "\n\nErrors:\n" + "\n".join(errors)
            messagebox.showwarning("Video Trimmer - Stat video(s) completed with errors", summary)
        else:
            messagebox.showinfo("Video Trimmer", summary)

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

        for filename, start, end in rows:
            self.add_trim_row(filename=filename, start=start, end=end)

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

    def export_all(self):
        input_path = self.input_path_var.get().strip()
        output_dir = self.output_dir_var.get().strip()

        if not input_path:
            messagebox.showerror("Video Trimmer", "Please select an input video.")
            return
        if not output_dir:
            messagebox.showerror("Video Trimmer", "Please select an output folder.")
            return
        if shutil.which("ffmpeg") is None:
            messagebox.showerror(
                "Video Trimmer",
                "ffmpeg was not found on PATH. Install it from https://ffmpeg.org/download.html",
            )
            return

        reencode = self.reencode_var.get()
        errors = []
        successes = []
        input_path = os.path.abspath(input_path)
        output_dir = os.path.abspath(output_dir)
        font_filename, font_dir = get_watermark_font_and_cwd()

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

        summary = f"Successfully created {len(successes)} file(s)."
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