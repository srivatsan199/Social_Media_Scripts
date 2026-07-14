#!/usr/bin/env python3
"""
video_trimmer_gui.py - GUI for trimming multiple clips out of one video using ffmpeg.

Requires ffmpeg to be installed and available on PATH:
  https://ffmpeg.org/download.html

Run:
  python video_trimmer_gui.py
"""

import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False


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


class TrimRow:
    """One row of trim inputs: output filename, start time, end time, remove button."""

    def __init__(self, parent, on_remove):
        self.frame = ttk.Frame(parent, relief="groove", borderwidth=1, padding=8)

        ttk.Label(self.frame, text="Output filename").grid(row=0, column=0, sticky="w")
        ttk.Label(self.frame, text="Start (HH:MM:SS)").grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Label(self.frame, text="End (HH:MM:SS)").grid(row=0, column=2, sticky="w", padx=(10, 0))

        self.filename_var = tk.StringVar(value="output.mp4")
        self.start_var = tk.StringVar(value="00:00:00")
        self.end_var = tk.StringVar(value="00:00:10")

        ttk.Entry(self.frame, textvariable=self.filename_var, width=24).grid(row=1, column=0, sticky="we")
        ttk.Entry(self.frame, textvariable=self.start_var, width=12).grid(row=1, column=1, sticky="we", padx=(10, 0))
        ttk.Entry(self.frame, textvariable=self.end_var, width=12).grid(row=1, column=2, sticky="we", padx=(10, 0))

        remove_btn = ttk.Button(self.frame, text="Remove", command=lambda: on_remove(self))
        remove_btn.grid(row=1, column=3, padx=(10, 0))

    def get_values(self):
        return self.filename_var.get().strip(), self.start_var.get().strip(), self.end_var.get().strip()

    def destroy(self):
        self.frame.destroy()


class VideoTrimmerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Video Trimmer")
        self.root.geometry("640x520")

        self.input_path_var = tk.StringVar()
        self.output_dir_var = tk.StringVar()
        self.reencode_var = tk.BooleanVar(value=False)
        self.url_var = tk.StringVar()
        self.download_status_var = tk.StringVar(value="")

        self.trim_rows = []

        self._build_ui()
        self.add_trim_row()  # start with one row

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

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

        # Add trim button
        ttk.Button(self.root, text="+ Add trim", command=self.add_trim_row).pack(
            anchor="w", padx=10, pady=(0, 6)
        )

        # Frame-accurate toggle
        ttk.Checkbutton(
            self.root,
            text="Frame-accurate (slower, re-encodes video)",
            variable=self.reencode_var,
        ).pack(anchor="w", padx=10, pady=(0, 6))

        # Export button
        ttk.Button(self.root, text="Export all", command=self.export_all).pack(
            anchor="e", padx=10, pady=10
        )

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

    def add_trim_row(self):
        row = TrimRow(self.rows_frame, self.remove_trim_row)
        row.frame.pack(fill="x", pady=4)
        self.trim_rows.append(row)

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

        for i, row in enumerate(self.trim_rows, start=1):
            filename, start, end = row.get_values()
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

            # Using -t (duration) instead of -to avoids a well-known ffmpeg quirk
            # where -to is sometimes measured from the seek point rather than
            # the original timeline, producing an unexpectedly long/short clip.
            cmd = ["ffmpeg", "-y", "-ss", start, "-i", input_path, "-t", seconds_to_time_str(duration_sec)]
            if reencode:
                cmd += ["-c:v", "libx264", "-c:a", "aac"]
            else:
                cmd += ["-c", "copy"]
            cmd.append(output_path)

            result = subprocess.run(cmd, capture_output=True, text=True)
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
