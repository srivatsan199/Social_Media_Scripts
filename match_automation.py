#!/usr/bin/env python3
"""
match_automation.py - match-level logic behind the Video Trimmer's automation.

Turns text copied from a CricClubs "Ball by Ball" page into player stats and a
list of highlights. Contains no Tkinter code, so it can be tested on its own.
"""

import bisect
import operator
import os
import re
import subprocess
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# The part of the frame holding the score and overs on the CricClubs Live
# scoreboard, as fractions of the frame: (left, top, width, height). The frames
# are scaled to a fixed size, so one threshold works at any video resolution.
SCOREBOARD_REGION = (0.41, 0.875, 0.18, 0.115)
SCAN_SIZE = (92, 36)
SCAN_FPS = 2
PIXEL_DIFFERENCE = 40  # how far a pixel's brightness must move to count
MIN_CHANGED_PIXELS = 12  # how many pixels must move to count as a change
MIN_SECONDS_BETWEEN_CHANGES = 4

# Boundary clips are 4 seconds, 2 either side of the ball travelling down the
# pitch. Wicket clips are 6 seconds, 3 either side.
CLOCK_SLACK = 120  # seconds a ball's logged minute may sit outside the video

# The overs number on the CricClubs overlay, right of the word OVERS. It changes
# exactly once per legal ball, so it says which ball each scoreboard change is.
OVERS_REGION = (0.515, 0.935, 0.085, 0.035)  # left, top, width, height: "OVERS 12.3"
OVERS_SIZE = (136, 28)
OVERS_LABEL_LETTERS = 5  # the word OVERS comes first
# The overlay's digits, each squeezed to 8 x 12 (rows separated by spaces).
# Taken from the Nov 7 2026 match; several drawings per digit because the
# text lands on slightly different pixels.
OVERS_DIGITS = {
    "0": ["..#####. ..#####. .####### .##...## ###....# ###....# ###....# ###....# ###....# .##...## .####### ..######",
          "..####.. .######. .####### ###...## ##....## ##....## ##....## ##....## ##....## ##....## .####### .######."],
    "1": ["######## ######## ######## ..###### ..###### ..###### ..###### ..###### ..###### ..###### ..###### ....####",
          "######## ######## ######## ....#### ....#### ....#### ....#### ....#### ....#### ....#### ....#### ....####",
          "..###### ######## ..###### ...##### ...##### ...##### ...##### ...##### ...##### ...##### ...##### ...#####",
          "..###### #######. #######. ...####. ...####. ...####. ...####. ...####. ...####. ...####. ...####. ...####."],
    "2": ["..#####. ..#####. .####### .##...## ......## .....### ....###. ...####. ..##.... .###.... .###.... .#######",
          "..####.. .######. ######## ##....## ......## ....#### ....###. ..####.. .###.... ###..... ###..... ########"],
    "3": ["..####.. .######. ######## ##....## ......## ....###. ....###. ....#### ......## ##....## ######## .######."],
    "4": [".....##. ....###. ....###. ...####. ..#####. ..##.##. .###.##. .###.##. ######## ######## .....##. .....##.",
          ".....### .....### ....#### ....#### ...##### ..##.### ..##.### .###.##. ######## .####### .....##. .....###"],
    "5": [".####### ######## #######. ###..... ######.. ######## ###..### ......## ......## ......## ######## .######.",
          "...###.. .####### .###..#. .##..... .#####.. .####### ###...## ......## ......## ......## .####### .#######"],
    "6": ["....###. ....##.. ....#... ..###... ..##.... ..#####. .####### .###.### ###...## ###...## .####### ..#####."],
    "7": ["######## ######## ..###### .....### .....##. ....###. ....###. ....##.. ...##... ...##... ..##.... ..##....",
          ".###.... ######## ##..#### ......## .....### ....###. ....##.. ....##.. ...##... ..###... ..##.... ..##...."],
    "8": ["..#####. ..#####. .####### .##...## .##..### ..#####. ..#####. .####### ###...## ###...## .####### .######.",
          "..####.. .####### ######## ###...## ###...## ..#####. .######. ######## ##....## ##....## ######## .#######"],
    "9": ["..#####. ..#####. .####### ###...## ###...## .####### ..###### ...####. ....###. ....###. ....#... ...##...",
          "..####.. .####### ######## ##....## ##....## ######## ..###### ..###### ....###. ....##.. ...###.. ..###..."],
}
OVERS_DIGIT_MATCH = 16  # most of the 96 squeezed pixels a digit may differ by
MIN_OVERS_READ = 0.5  # share of legal balls the overs must be read for, to be used
OVERS_CONTRAST = 80  # the white text must stand this far above the box
OVERS_DARK = 130  # the box is a dark colour (red, black...) behind white text
OVERS_KEYFRAME_GUESS = 5  # seconds between keyframes
OVERS_SAME_PIXELS = 30  # bitmaps differing by fewer pixels show the same value
OVERS_GLYPH_MATCH = 0.18  # share of pixels two drawings of one digit may differ by

# The ball queue: every delivery found from the video alone, then matched with
# the scorecard in order.
QUEUE_CHUNK = 20  # seconds of video read at a time
QUEUE_OVERLAP = 1  # so a ball on the join between pieces isn't lost
QUEUE_MERGE = 6  # paths closer together than this are the same delivery
QUEUE_MAX_GAP = 75  # furthest a delivery can be from its ball's logged minute
# Leaving a ball unpaired must cost less than pairing every later ball with
# the next delivery along (about 38 s each), or one missed ball shifts the rest.
QUEUE_SKIP_COST = 30
QUEUE_STRAY_COST = 4  # a path that wasn't a delivery (a throw, a warm-up)
QUEUE_WINDOW_SLACK = 8  # seconds the scorer's clock may be off at a ball
# Each delivery is a sequence: the bowler's run-up, the ball, then the batter
# playing it or the ball going past. Movement is measured in strips across the
# pitch area, and the sequence is learnt from the balls seen clearly.
SEQUENCE_FPS = 5
SEQUENCE_ZONES = 8
SEQUENCE_BEFORE = 15  # frames of run-up before the ball (3 s)
SEQUENCE_AFTER = 10  # frames of the shot after it (2 s)
SEQUENCE_MIN_GAP = 15  # seconds; deliveries are never closer than this
SEQUENCE_MIN_EXAMPLES = 8
SEQUENCE_SNAP = 3  # frames either side of a ball path to look for its sequence
SEQUENCE_JOIN = 4  # seconds; a sequence this close to a ball path is that ball
SEQUENCE_SURE = 0.45  # a ball path with movement this close to the pattern is sure
SEQUENCE_FOUND = 0.4  # movement this close to the pattern counts as a delivery
# On the Nov 1 match the sequence found 209 of 212 balls at 0.4, against 149 from
# the ball's path alone, with some extra movement the matching skips over.
QUEUE_WEAK_COST = 15  # pairing a ball with a weak sequence costs up to this
FIT_TOLERANCE = 40
BOARD_FIT_STEP = 10
FIT_STEP = 5
MIN_QUEUE_MATCH = 0.2  # share of balls that must line up for the right video
BALL_WINDOW = (2, 2)
WICKET_BALL_WINDOW = (3, 3)
# How far before the scoreboard update to search for the ball. Scorers took up
# to 16.5 seconds to log a boundary, and longer for a dismissal.
BOUNDARY_BALL_SEARCH = 18
WICKET_BALL_SEARCH = 34
BALL_SEARCH_END = 3  # stop searching this long before the update
TYPICAL_BOARD_DELAY = 9  # seconds from a ball being bowled to the scoreboard updating

# The ball is found as a small bright dot on a straight, falling path.
BALL_AREA = (0.2, 0.3, 0.6, 0.45)  # left, top, width, height, as fractions
BALL_SCAN_WIDTH = 640
BALL_SCAN_FPS = 30
BALL_BRIGHTNESS = 165
BALL_FRAME_GAP = 2  # compare with frames this far away, so it can't self-overlap
BALL_MIN_DOTS = 5  # paths this long count as the ball on their own
BALL_WEAK_DOTS = 3  # shorter paths also need movement on the field to back them up
BALL_CLEAR_DOTS = 7  # paths this long teach the match's usual ball path
BALL_PATH_TOLERANCE = (60, 45)  # pixels from the usual path, across and down

# A weak path counts only near the crease area, clear of the previous ball, and
# with a burst of movement on the field at about the same time.
WEAK_BALL_ACROSS = 190  # pixels either side of the usual path
WEAK_BALL_BELOW = 35  # pixels below the usual path
WEAK_BALL_SETTLE = 4  # seconds after the previous scoreboard change
WEAK_BALL_RISE_GAP = 3  # seconds between the path and a burst of movement
MOTION_RISES = 4  # how many bursts of movement to consider

# With no ball, the clip follows the strongest burst of movement, which can
# start during the run-up. With no signal at all, it uses a typical gap before
# the scoreboard update. Both are flagged for checking.
RISE_WINDOW = (1, 3)
WICKET_RISE_WINDOW = (2, 4)
NO_SIGNAL_WINDOW = (12, -8)
WICKET_NO_SIGNAL_WINDOW = (13, -7)

OVER_LINE_RE = re.compile(r"^\d+\.\d$")
TIME_LINE_RE = re.compile(r"^(\d{1,2}):(\d{2}) ?([AP]M)$")
TOKEN_RE = re.compile(r"^(\d+)?(wd|lb|nb|b|W)?$")
DELIVERY_RE = re.compile(
    r"^(?P<bowler>.+?) to (?P<batter>.+?)"
    r"(?P<rest>, \d+ runs?.*| \d+ WIDES?.*| WIDE.*| OUT!.*| NO BALL.*)$"
)
RUNS_IN_TEXT_RE = re.compile(r"(\d+) (?:runs?|WIDES?)")
INTRO_RE = re.compile(r"^(?P<name>.+?) \(\d+\), comes (?P<role>to the crease|into the attack)\.")
OVER_SUMMARY_SCORE_RE = re.compile(r"^(\d+)/(\d+)$")
RETIRED_RE = re.compile(r"OUT! RETIRED")

# These dismissals are not credited to the bowler.
NON_BOWLER_DISMISSALS = ("RUN OUT", "RETIRED", "OBSTRUCT", "TIMED OUT")


@dataclass
class Ball:
    over_label: str
    over_index: int
    bowler: str
    batter: str
    total_runs: int
    batter_runs: int
    bowler_runs: int
    faced: bool  # counts as a ball faced by the batter
    legal: bool  # counts towards the bowler's over
    is_four: bool
    is_six: bool
    is_wicket: bool
    bowler_wicket: bool
    how_out: str
    minute_of_day: int


@dataclass
class BallCandidate:
    time: float
    dots: int
    x: float
    y: float


@dataclass
class Innings:
    balls: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def _short_name(full_name):
    """'Srivatsan Srinivasan' -> 'Srivatsan S', as CricClubs abbreviates it."""
    parts = full_name.split()
    if len(parts) < 2:
        return full_name
    return f"{parts[0]} {parts[-1][0]}"


def _minute_of_day(hour, minute, meridiem):
    hour = hour % 12
    if meridiem == "PM":
        hour += 12
    return hour * 60 + minute


def _next_time(lines, start):
    """Find the first time line at or after `start`, within a few lines."""
    for line in lines[start:start + 4]:
        match = TIME_LINE_RE.match(line)
        if match:
            return _minute_of_day(int(match.group(1)), int(match.group(2)), match.group(3))
    return None


def parse_ball_by_ball(text):
    """Parse text copied from one innings of a CricClubs ball-by-ball page."""
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    innings = Innings()
    batters = {}
    bowlers = {}
    expected_scores = []
    running_runs = 0
    running_wickets = 0

    for i, line in enumerate(lines):
        intro = INTRO_RE.match(line)
        if intro:
            role_map = batters if intro.group("role") == "to the crease" else bowlers
            role_map[_short_name(intro.group("name"))] = intro.group("name")
            if RETIRED_RE.search(line):
                running_wickets += 1
            continue

        if line == "Over" and i + 1 < len(lines):
            for candidate in lines[i + 2:i + 6]:
                score = OVER_SUMMARY_SCORE_RE.match(candidate)
                if score:
                    expected_scores.append(
                        (lines[i + 1], int(score.group(1)), int(score.group(2)), running_runs, running_wickets)
                    )
                    break
            continue

        if not OVER_LINE_RE.match(line) or i + 1 >= len(lines):
            continue
        delivery = DELIVERY_RE.match(lines[i + 1])
        if not delivery:
            if " to " in lines[i + 1]:
                innings.warnings.append(f"Could not read ball {line}: {lines[i + 1]}")
            continue

        token = lines[i - 1] if i else ""
        token_match = TOKEN_RE.match(token)
        rest = delivery.group("rest")
        extra = token_match.group(2) if token_match else None
        if token_match and token_match.group(1):
            total = int(token_match.group(1))
        else:
            in_text = RUNS_IN_TEXT_RE.search(rest)
            total = int(in_text.group(1)) if in_text else (1 if "WIDE" in rest else 0)

        wide = "WIDE" in rest or extra == "wd"
        no_ball = "NO BALL" in rest or extra == "nb"
        bye = "BYE" in rest or extra in ("lb", "b")
        is_wicket = "OUT!" in rest
        how_out = rest.split("OUT!", 1)[1].strip() if is_wicket else ""
        if no_ball:
            innings.warnings.append(f"Ball {line} is a no-ball: check that player's stat by hand.")

        if wide or bye:
            batter_runs = 0
        elif no_ball:
            batter_runs = max(total - 1, 0)
        else:
            batter_runs = total

        bowler_short = delivery.group("bowler")
        batter_short = delivery.group("batter")
        minute = _next_time(lines, i + 2)
        if minute is None:
            innings.warnings.append(f"Ball {line} has no time, so it was skipped.")
            continue

        innings.balls.append(
            Ball(
                over_label=line,
                over_index=int(line.split(".")[0]),
                bowler=bowlers.get(bowler_short, bowler_short),
                batter=batters.get(batter_short, batter_short),
                total_runs=total,
                batter_runs=batter_runs,
                bowler_runs=0 if bye else total,
                faced=not wide,
                legal=not wide and not no_ball,
                is_four="FOUR" in rest,
                is_six="SIX" in rest,
                is_wicket=is_wicket,
                bowler_wicket=is_wicket and not any(k in how_out for k in NON_BOWLER_DISMISSALS),
                how_out=how_out,
                minute_of_day=minute,
            )
        )
        running_runs += total
        running_wickets += 1 if is_wicket else 0

    for over, runs, wickets, parsed_runs, parsed_wickets in expected_scores:
        if (runs, wickets) != (parsed_runs, parsed_wickets):
            innings.warnings.append(
                f"After over {over} the page says {runs}/{wickets} but the parsed balls "
                f"add up to {parsed_runs}/{parsed_wickets}."
            )
    if not innings.balls:
        innings.warnings.append("No balls found. Copy the whole Ball by Ball page and paste again.")
    return innings


def scan_scoreboard_changes(video_path, start=0.0, duration=None, on_progress=None):
    """Return the video times, in seconds, at which the score box changed.

    ffmpeg streams small greyscale crops of the scoreboard at SCAN_FPS, and
    neighbouring frames are compared here, so no OCR is needed.
    """
    left, top, width, height = SCOREBOARD_REGION
    size = SCAN_SIZE[0] * SCAN_SIZE[1]
    video_filter = (
        f"fps={SCAN_FPS},crop=iw*{width}:ih*{height}:iw*{left}:ih*{top},"
        f"scale={SCAN_SIZE[0]}:{SCAN_SIZE[1]}:flags=area,format=gray"
    )
    cmd = ["ffmpeg", "-v", "error", "-ss", str(start)]
    if duration is not None:
        cmd += ["-t", str(duration)]
    cmd += ["-i", video_path, "-map", "0:v:0", "-vf", video_filter, "-f", "rawvideo", "-"]

    changes = []
    previous = None
    frame_index = 0
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            frame = process.stdout.read(size)
            if len(frame) < size:
                break
            if previous is not None:
                moved = sum(1 for a, b in zip(frame, previous) if abs(a - b) > PIXEL_DIFFERENCE)
                if moved >= MIN_CHANGED_PIXELS:
                    moment = start + frame_index / SCAN_FPS
                    if not changes or moment - changes[-1] >= MIN_SECONDS_BETWEEN_CHANGES:
                        changes.append(moment)
            previous = frame
            frame_index += 1
            if on_progress and frame_index % 600 == 0:
                on_progress(frame_index / SCAN_FPS)
    finally:
        process.stdout.close()
        process.wait()
    if process.returncode != 0:
        raise RuntimeError("ffmpeg could not read the video for the scoreboard scan.")
    return changes


def read_overs_box(video_path, on_progress=None, duration=None):
    """Return (time, text_columns, bitmap) for every keyframe, from the overs
    number on the CricClubs overlay ("OVERS 2.2"). Only keyframes are decoded,
    so a whole match takes a minute or two. `bitmap` is None when the overlay
    isn't showing; otherwise it marks the white text pixels."""
    left, top, width, height = OVERS_REGION
    columns, rows = OVERS_SIZE
    with tempfile.TemporaryDirectory() as folder:
        info_path = os.path.join(folder, "frames.txt")
        cmd = [
            "ffmpeg", "-v", "info", "-skip_frame", "nokey", "-i", video_path, "-map", "0:v:0",
            "-vf", f"crop=iw*{width}:ih*{height}:iw*{left}:ih*{top},"
                   f"scale={columns}:{rows}:flags=area,format=gray,showinfo",
            "-fps_mode", "passthrough", "-f", "rawvideo", "-",
        ]
        with open(info_path, "w", encoding="utf-8", errors="replace") as info:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=info)
            frames = []
            size = columns * rows
            try:
                while True:
                    frame = process.stdout.read(size)
                    if len(frame) < size:
                        break
                    frames.append(frame)
                    if on_progress and duration and len(frames) % 50 == 0:
                        on_progress(len(frames) * OVERS_KEYFRAME_GUESS, duration)
            finally:
                process.stdout.close()
                process.wait()
        with open(info_path, encoding="utf-8", errors="replace") as info:
            times = [float(m.group(1)) for m in re.finditer(r"pts_time:\s*([\d.]+)", info.read())]
    readings = []
    for when, frame in zip(times, frames):
        background = sum(1 for value in frame if value <= OVERS_DARK)
        if background < len(frame) * 0.35:
            readings.append((when, 0, None))
            continue
        # Halfway between the box and the brightest text, so blurry small
        # videos keep their letters whole.
        ordered = sorted(frame)
        dark, bright = ordered[len(ordered) // 5], ordered[-len(ordered) // 50]
        if bright - dark < OVERS_CONTRAST:
            readings.append((when, 0, None))
            continue
        cutoff = (dark + bright) / 2
        white = bytes(1 if value >= cutoff else 0 for value in frame)
        used = sum(1 for x in range(columns) if any(white[y * columns + x] for y in range(rows)))
        readings.append((when, used, white))
    if readings and on_progress and duration:
        on_progress(duration, duration)
    return readings


def overs_runs(readings):
    """Group keyframe readings into runs of one displayed overs value.
    Returns [(start_time, previous_keyframe_time, bitmap)] in order. A value
    must show on two keyframes in a row to count, so a passing animation
    doesn't, and a value that flicks back to the one before is ignored."""
    runs = []
    candidate = None  # (start, previous, width, bitmap, count)
    previous_time = None
    for when, width, bitmap in readings:
        if bitmap is None or width == 0:
            previous_time = when
            continue
        if runs and _same_text(bitmap, runs[-1][3]):
            candidate = None
        elif candidate and _same_text(bitmap, candidate[3]):
            count = candidate[4] + 1
            candidate = candidate[:4] + (count,)
            if count == 2:
                if len(runs) >= 2 and _same_text(candidate[3], runs[-2][3]):
                    runs.pop()  # A, B, A: B was a flicker
                else:
                    runs.append(candidate[:4])
                candidate = None
        else:
            candidate = (when, previous_time, width, bitmap, 1)
        previous_time = when
    return [(start, before, bitmap) for start, before, _, bitmap in runs]


def _same_text(a, b):
    return sum(1 for x, y in zip(a, b) if x != y) <= OVERS_SAME_PIXELS


def _overs_display(ball):
    """What the overs box shows after this legal ball: "2.3", or "3" after
    the sixth ball of an over."""
    over, number = ball.over_label.split(".")
    return str(int(over) + 1) if int(number) >= 6 else f"{int(over)}.{int(number)}"


def _glyphs(bitmap):
    """Split the overs text into characters at blank columns. Each glyph is
    (width, height, pixels) cropped to its own ink, plus how low it sits."""
    columns, rows = OVERS_SIZE
    used = [any(bitmap[y * columns + x] for y in range(rows)) for x in range(columns)]
    glyphs = []
    x = 0
    while x < columns:
        if not used[x]:
            x += 1
            continue
        start = x
        while x < columns and used[x]:
            x += 1
        if x >= columns:
            break  # touches the right edge: the overlay's white panel, not text
        ink_rows = [y for y in range(rows) if any(bitmap[y * columns + c] for c in range(start, x))]
        top, bottom = ink_rows[0], ink_rows[-1]
        pixels = tuple(
            bitmap[y * columns + c] for y in range(top, bottom + 1) for c in range(start, x)
        )
        glyphs.append((x - start, bottom - top + 1, pixels))
    return glyphs


def _is_dot(glyph, tallest):
    return glyph[1] <= tallest * 0.4


def _glyph_distance(a, b):
    """How differently two glyphs are drawn, 0 (same) to 1, or 1 if their
    sizes differ by more than a pixel."""
    if abs(a[0] - b[0]) > 1 or abs(a[1] - b[1]) > 1:
        return 1.0
    width, height = min(a[0], b[0]), min(a[1], b[1])
    differ = sum(
        a[2][y * a[0] + x] != b[2][y * b[0] + x] for y in range(height) for x in range(width)
    )
    return differ / (width * height)


def _squeeze(glyph, width=8, height=12):
    """Resize a glyph to a fixed grid so any video size compares alike."""
    w, h, pixels = glyph
    return tuple(
        pixels[min(h - 1, y * h // height) * w + min(w - 1, x * w // width)]
        for y in range(height) for x in range(width)
    )


_DIGIT_TEMPLATES = [
    (digit, tuple(1 if c == "#" else 0 for c in drawing.replace(" ", "")))
    for digit, drawings in OVERS_DIGITS.items() for drawing in drawings
]


def read_digit(glyph):
    """Return the digit this glyph shows, or None if it isn't clearly one."""
    squeezed = _squeeze(glyph)
    distance, digit = min(
        (sum(a != b for a, b in zip(squeezed, template)), digit)
        for digit, template in _DIGIT_TEMPLATES
    )
    return digit if distance <= OVERS_DIGIT_MATCH else None


def read_overs_text(bitmap):
    """Read the overs value ("2.3", "11") from one overs-box bitmap, or None."""
    glyphs = _glyphs(bitmap)[OVERS_LABEL_LETTERS:]
    if not glyphs:
        return None
    tallest = max(g[1] for g in glyphs)
    text = ""
    for glyph in glyphs:
        if _is_dot(glyph, tallest):
            text += "."
            continue
        digit = read_digit(glyph)
        if digit is None:
            return None
        text += digit
    return text if re.fullmatch(r"\d{1,2}(\.[1-5])?", text) else None


def read_overs_values(runs):
    """Read each run's overs value as text, or None where it isn't clear."""
    return [read_overs_text(bitmap) for _, _, bitmap in runs]


def _innings_of(balls):
    """Number each ball's innings: the overs count starts again at 0.1."""
    innings, numbers, last = 0, [], None
    for ball in balls:
        over = float(ball.over_label)
        if last is not None and over < last - 1:
            innings += 1
        numbers.append(innings)
        last = over
    return numbers


def align_overs(balls, runs):
    """Pair each legal ball with the overs-box run that first showed its
    value. Returns {ball index: run}. The value says exactly which ball it is,
    even when the stream skipped minutes, so no clock is needed."""
    values = read_overs_values(runs)
    # Split the runs into innings where the count drops back.
    run_innings, innings, last = [], 0, None
    for value in values:
        if value is not None:
            number = float(value)
            if last is not None and number < last - 1:
                innings += 1
            last = number
        run_innings.append(innings)
    first_run, shown = {}, {}
    for run, value, number in zip(runs, values, run_innings):
        if value is not None:
            first_run.setdefault((number, value), run)
            shown[(number, value)] = shown.get((number, value), 0) + 1
    pairs, corrected = {}, set()
    for index, (ball, number) in enumerate(zip(balls, _innings_of(balls))):
        if ball.legal:
            key = (number, _overs_display(ball))
            if key in first_run:
                pairs[index] = first_run[key]
                if shown[key] > 1:
                    corrected.add(index)  # the scorer undid and re-entered it
    return pairs, corrected


def overs_moments(balls, runs, changes):
    """Return (moments, unsure): the scoreboard-change time for every ball,
    using the overs box to say which ball each change belongs to, and the
    indexes of balls whose time is only estimated.

    A legal ball's change is the last scoreboard change before its overs value
    first showed. Wides and no-balls don't move the overs, so they take the
    scoreboard changes between their neighbouring legal balls, in order. A
    ball the overlay never showed is placed from its neighbours' logged times."""
    paired, unsure = align_overs(balls, runs)
    unsure = set(unsure)
    moments = [None] * len(balls)
    for index, (start, before, _) in paired.items():
        low = (before if before is not None else start - OVERS_KEYFRAME_GUESS) - 1
        inside = [c for c in changes if low < c <= start + 0.5]
        moments[index] = inside[-1] if inside else start - OVERS_KEYFRAME_GUESS / 2
    index = 0
    while index < len(balls):
        if moments[index] is not None:
            index += 1
            continue
        end = index
        while end < len(balls) and moments[end] is None:
            end += 1
        after = moments[end] if end < len(balls) else None
        before = moments[index - 1] if index > 0 else None
        if before is not None and after is not None:
            between = [c for c in changes if before < c < after]
            for offset, ball_index in enumerate(range(index, end)):
                if offset < len(between):
                    moments[ball_index] = between[offset]
        index = end

    known = [i for i, moment in enumerate(moments) if moment is not None]
    if not known:
        return moments, unsure
    for index, moment in enumerate(moments):
        if moment is not None:
            continue
        nearest = min(known, key=lambda k: abs(k - index))
        logged_gap = (balls[index].minute_of_day - balls[nearest].minute_of_day) * 60
        moments[index] = max(moments[nearest] + logged_gap, 0.0)
        unsure.add(index)
    return moments, unsure


def _read_ball_frames(video_path, start, length):
    """Return (width, frames) for the ball search area as greyscale bytes."""
    left, top, width, height = BALL_AREA
    scan_height = int(round(BALL_SCAN_WIDTH * (height * 9) / (width * 16) / 2)) * 2
    cmd = [
        "ffmpeg", "-v", "error", "-ss", str(start), "-t", str(length), "-i", video_path,
        "-map", "0:v:0", "-vf",
        f"fps={BALL_SCAN_FPS},crop=iw*{width}:ih*{height}:iw*{left}:ih*{top},"
        f"scale={BALL_SCAN_WIDTH}:{scan_height}:flags=bicubic,format=gray",
        "-f", "rawvideo", "-",
    ]
    raw = subprocess.run(cmd, capture_output=True).stdout
    size = BALL_SCAN_WIDTH * scan_height
    return BALL_SCAN_WIDTH, [raw[i:i + size] for i in range(0, len(raw) - size + 1, size)]


def _find_ball_dots(moving_pixels, width):
    """Group moving bright pixels and keep the small, round, isolated ones."""
    hits = set()
    index = moving_pixels.find(1)
    while index >= 0:
        hits.add(index)
        index = moving_pixels.find(1, index + 1)

    groups = []
    while hits:
        stack = [hits.pop()]
        group = []
        while stack:
            pixel = stack.pop()
            group.append(pixel)
            for other in (
                pixel - 1, pixel + 1, pixel - width, pixel + width,
                pixel - width - 1, pixel - width + 1, pixel + width - 1, pixel + width + 1,
            ):
                if other in hits:
                    hits.remove(other)
                    stack.append(other)
        groups.append(group)

    centres = [
        (sum(p % width for p in g) / len(g), sum(p // width for p in g) / len(g)) for g in groups
    ]
    dots = []
    for index, group in enumerate(groups):
        if not 5 <= len(group) <= 110:
            continue
        xs = [p % width for p in group]
        ys = [p // width for p in group]
        box_width, box_height = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
        longest, shortest = max(box_width, box_height), min(box_width, box_height)
        if longest > 14 or longest > 2.2 * shortest or len(group) < 0.45 * box_width * box_height:
            continue
        x, y = centres[index]
        if all(i == index or abs(x - c[0]) + abs(y - c[1]) > 16 for i, c in enumerate(centres)):
            dots.append((x, y))
    return dots


def _follow_ball_path(dots_per_frame, frame, dot, next_frame, next_dot):
    """Extend a pair of dots frame by frame, allowing one missed frame."""
    path = [(frame, dot), (next_frame, next_dot)]
    gap = next_frame - frame
    dx, dy = (next_dot[0] - dot[0]) / gap, (next_dot[1] - dot[1]) / gap
    step = (dx * dx + dy * dy) ** 0.5
    x, y, current = next_dot[0], next_dot[1], next_frame
    while True:
        found = None
        for gap in (1, 2):
            if current + gap >= len(dots_per_frame):
                continue
            want_x, want_y = x + dx * gap, y + dy * gap
            near = [
                d for d in dots_per_frame[current + gap]
                if abs(d[0] - want_x) <= step * 0.45 + 2 and abs(d[1] - want_y) <= step * 0.45 + 2
            ]
            if near:
                found = gap, min(near, key=lambda d: abs(d[0] - want_x) + abs(d[1] - want_y))
                break
        if found is None:
            return path
        gap, dot = found
        dx, dy = (dot[0] - x) / gap, (dot[1] - y) / gap
        x, y, current = dot[0], dot[1], current + gap
        path.append((current, dot))


def _is_ball_path(path, width):
    """A ball flies fairly straight, covers real distance and falls on camera."""
    if not BALL_WEAK_DOTS <= len(path) <= 24:
        return False
    points = [point for _, point in path]
    travelled = sum(
        ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 for a, b in zip(points, points[1:])
    )
    across = points[-1][0] - points[0][0]
    drop = points[-1][1] - points[0][1]
    distance = (across ** 2 + drop ** 2) ** 0.5
    return distance >= width * 0.08 and distance / travelled >= 0.93 and drop >= abs(across) * 0.5


def pick_ball_path(dots_per_frame, width):
    """Return the longest ball-like path of (frame, dot) pairs, or None."""
    paths = [p for p in find_ball_paths(dots_per_frame, width) if len(p) >= BALL_MIN_DOTS]
    return max(paths, key=len) if paths else None


def find_ball_paths(dots_per_frame, width):
    """Return every ball-like path as a list of (frame, dot) pairs."""
    slowest, fastest = width * 0.012, width * 0.045
    paths = []
    for frame in range(len(dots_per_frame) - 1):
        for dot in dots_per_frame[frame]:
            for gap in (1, 2):
                if frame + gap >= len(dots_per_frame):
                    continue
                for next_dot in dots_per_frame[frame + gap]:
                    dx, dy = (next_dot[0] - dot[0]) / gap, (next_dot[1] - dot[1]) / gap
                    if dy <= 0 or not slowest <= (dx * dx + dy * dy) ** 0.5 <= fastest:
                        continue
                    path = _follow_ball_path(dots_per_frame, frame, dot, frame + gap, next_dot)
                    if _is_ball_path(path, width):
                        paths.append(path)
    return paths


def find_ball_candidates(video_path, start, length):
    """Return a BallCandidate for each possible ball in this stretch of video.
    Bright pixels are compared as big integers, which is fast enough to check
    every frame without any extra Python packages."""
    width, frames = _read_ball_frames(video_path, start, length)
    if len(frames) <= BALL_FRAME_GAP * 2:
        return []
    size = len(frames[0])
    is_bright = bytes(1 if value >= BALL_BRIGHTNESS else 0 for value in range(256))
    all_ones = int.from_bytes(b"\x01" * size, "little")
    bright = [int.from_bytes(frame.translate(is_bright), "little") for frame in frames]
    dots_per_frame = [[] for _ in frames]
    for i in range(BALL_FRAME_GAP, len(frames) - BALL_FRAME_GAP):
        moving = (
            bright[i]
            & (bright[i - BALL_FRAME_GAP] ^ all_ones)
            & (bright[i + BALL_FRAME_GAP] ^ all_ones)
        )
        dots_per_frame[i] = _find_ball_dots(moving.to_bytes(size, "little"), width)
    candidates = []
    for path in find_ball_paths(dots_per_frame, width):
        points = [point for _, point in path]
        candidates.append(
            BallCandidate(
                time=start + (path[0][0] + path[-1][0]) / 2 / BALL_SCAN_FPS,
                dots=len(path),
                x=sum(p[0] for p in points) / len(points),
                y=sum(p[1] for p in points) / len(points),
            )
        )
    return candidates


def pick_ball_candidate(candidates, centre):
    """Return the strongest candidate on the match's usual ball path, or None."""
    on_path = [
        c for c in candidates
        if c.dots >= BALL_MIN_DOTS and (
            centre is None
            or (abs(c.x - centre[0]) <= BALL_PATH_TOLERANCE[0] and abs(c.y - centre[1]) <= BALL_PATH_TOLERANCE[1])
        )
    ]
    return max(on_path, key=lambda c: c.dots) if on_path else None


def pick_weak_ball_candidate(candidates, rises, centre, previous_change):
    """Return the earliest faint ball path backed by a burst of movement."""
    if centre is None:
        return None
    earliest = previous_change + WEAK_BALL_SETTLE if previous_change is not None else None
    backed = [
        c for c in candidates
        if abs(c.x - centre[0]) <= WEAK_BALL_ACROSS
        and c.y - centre[1] <= WEAK_BALL_BELOW
        and (earliest is None or c.time >= earliest)
        and any(abs(rise - c.time) <= WEAK_BALL_RISE_GAP for rise in rises)
    ]
    return min(backed, key=lambda c: c.time) if backed else None


def find_ball_path_centre(candidate_lists):
    """Return the middle of the usual ball path, from the clearest balls in the
    match. The camera doesn't move, so real deliveries nearly all pass through
    the same spot. Returns None if too few balls were clear."""
    strongest = [max(c, key=lambda item: item.dots) for c in candidate_lists if c]
    clear = [c for c in strongest if c.dots >= BALL_CLEAR_DOTS]
    if len(clear) < 3:
        return None
    return (
        sorted(c.x for c in clear)[len(clear) // 2],
        sorted(c.y for c in clear)[len(clear) // 2],
    )


def scan_ball_queue(video_path, duration, on_progress=None, ball_finder=None):
    """Return the video time of every delivery seen in the whole video, in
    order, found only from the ball moving down the pitch.

    The video is read in short overlapping pieces to keep memory low.
    `ball_finder(start, length)` can stand in for find_ball_candidates."""
    finder = ball_finder or (lambda start, length: find_ball_candidates(video_path, start, length))
    candidates = []
    start = 0.0
    while start < duration:
        candidates += finder(start, min(QUEUE_CHUNK + QUEUE_OVERLAP, duration - start))
        start += QUEUE_CHUNK
        if on_progress:
            on_progress(min(start, duration), duration)
    return pick_deliveries(candidates)


def measure_zone_motion(video_path, duration=None, on_progress=None):
    """Return how much moved in each of SEQUENCE_ZONES strips across the
    pitch area, for every frame at SEQUENCE_FPS, as one flat list.

    ffmpeg does the work (frame differences, threshold, averaging into
    strips), so a whole match takes a minute or two."""
    left, top, width, height = BALL_AREA
    video_filter = (
        f"fps={SEQUENCE_FPS},crop=iw*{width}:ih*{height}:iw*{left}:ih*{top},"
        "scale=128:48,format=gray,tblend=all_mode=difference,"
        "lutyuv=y='if(gt(val,14),255,0)',"
        f"scale={SEQUENCE_ZONES}:1:flags=area"
    )
    cmd = ["ffmpeg", "-v", "error", "-i", video_path, "-map", "0:v:0", "-vf", video_filter,
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    values = bytearray()
    try:
        while True:
            block = process.stdout.read(SEQUENCE_ZONES * SEQUENCE_FPS * 60)
            if not block:
                break
            values += block
            if on_progress and duration:
                on_progress(len(values) / SEQUENCE_ZONES / SEQUENCE_FPS, duration)
    finally:
        process.stdout.close()
        process.wait()
    if process.returncode != 0:
        raise RuntimeError("ffmpeg could not read the video for the movement scan.")
    # tblend drops the first frame, so frame i of the result is video frame i + 1.
    return [0] * SEQUENCE_ZONES + list(values)


def _motion_window(motion, frame):
    """The movement from the run-up to the shot around this frame, or None."""
    start = (frame - SEQUENCE_BEFORE) * SEQUENCE_ZONES
    end = (frame + SEQUENCE_AFTER) * SEQUENCE_ZONES
    if start < 0 or end > len(motion):
        return None
    return motion[start:end]


def _normalise(window):
    count = len(window)
    mean = sum(window) / count
    centred = [value - mean for value in window]
    size = sum(value * value for value in centred) ** 0.5
    return [value / size for value in centred] if size else None


def _mirror(pattern):
    """The same sequence bowled from the other end: the strips reversed."""
    frames = [pattern[i:i + SEQUENCE_ZONES] for i in range(0, len(pattern), SEQUENCE_ZONES)]
    return [value for frame in frames for value in reversed(frame)]


def learn_delivery_patterns(motion, ball_times):
    """Learn this match's run-up, ball, shot movement from balls seen clearly.
    Bowlers change ends every over, so two patterns are learnt, one per end.
    Returns a list of patterns, empty if too few balls were clear."""
    windows = []
    for moment in ball_times:
        window = _motion_window(motion, int(round(moment * SEQUENCE_FPS)))
        normalised = _normalise(window) if window else None
        if normalised:
            windows.append(normalised)
    if len(windows) < SEQUENCE_MIN_EXAMPLES:
        return []

    def average(group):
        return _normalise([sum(values) / len(group) for values in zip(*group)])

    first = average(windows)
    patterns = [first, _normalise(_mirror(first))]
    for _ in range(4):
        groups = [[], []]
        for window in windows:
            scores = [sum(map(operator.mul, window, p)) if p else -2 for p in patterns]
            groups[scores.index(max(scores))].append(window)
        patterns = [average(group) if len(group) >= 3 else None for group in groups]
        patterns = [p for p in patterns if p] or [first]
        if len(patterns) == 1:
            break
    return patterns


def sequence_score(motion, frame, patterns):
    """How closely the movement around this frame matches a delivery, -1 to 1."""
    window = _motion_window(motion, frame)
    if window is None:
        return 0.0
    count = len(window)
    total = sum(window)
    spread = sum(value * value for value in window) - total * total / count
    if spread <= 0:
        return 0.0
    return max(sum(map(operator.mul, window, p)) for p in patterns) / spread ** 0.5


def find_sequence_deliveries(motion, patterns, threshold):
    """Return the times of every movement sequence that looks like a delivery,
    strongest within each SEQUENCE_MIN_GAP, as (time, score) pairs."""
    frames = len(motion) // SEQUENCE_ZONES
    scores = [sequence_score(motion, frame, patterns) for frame in range(frames)]
    peaks = []
    reach = int(SEQUENCE_MIN_GAP * SEQUENCE_FPS / 2)
    for frame, score in enumerate(scores):
        if score < threshold:
            continue
        nearby = scores[max(0, frame - reach):frame + reach + 1]
        if score >= max(nearby):
            peaks.append((frame / SEQUENCE_FPS, score))
    return peaks


def build_ball_queue(video_path, duration, on_progress=None, ball_finder=None, motion=None):
    """Return every delivery in the video as [time, sure] pairs, in order.

    Each delivery is the sequence of run-up, ball and shot. It is sure when the
    ball's path was seen and the movement around it matches; when the ball was
    hidden (someone in the way), the movement alone places it, marked unsure."""
    def share(offset, size):
        if not on_progress:
            return None
        return lambda done, total: on_progress(offset + size * done / total, 1.0)

    ball_times = scan_ball_queue(video_path, duration, share(0.0, 0.85), ball_finder)
    if motion is None:
        motion = measure_zone_motion(video_path, duration, share(0.85, 0.15))
    return combine_deliveries(ball_times, motion)


def _best_score_near(motion, moment, patterns):
    frame = int(round(moment * SEQUENCE_FPS))
    return max(
        sequence_score(motion, f, patterns)
        for f in range(frame - SEQUENCE_SNAP, frame + SEQUENCE_SNAP + 1)
    )


def combine_deliveries(ball_times, motion):
    """Join the movement sequences with the ball paths into one queue of
    [time, sure, score]. Every sequence is a possible delivery. If the ball's
    path was seen in it, the time is the ball's and, with a strong sequence,
    it is sure. A path with no sequence around it (a throw) is left out."""
    patterns = learn_delivery_patterns(motion, ball_times)
    if not patterns:
        return [[moment, True, 1.0] for moment in ball_times]
    queue = []
    for moment, score in find_sequence_deliveries(motion, patterns, SEQUENCE_FOUND):
        index = bisect.bisect_left(ball_times, moment - SEQUENCE_JOIN)
        near = [t for t in ball_times[index:index + 3] if abs(t - moment) <= SEQUENCE_JOIN]
        if near:
            ball = min(near, key=lambda t: abs(t - moment))
            queue.append([ball, score >= SEQUENCE_SURE, round(score, 3)])
        else:
            queue.append([moment, False, round(score, 3)])
    return queue


def pick_deliveries(candidates):
    """Keep the clear paths on the match's usual ball path, one per delivery."""
    clear = [c for c in candidates if c.dots >= BALL_CLEAR_DOTS]
    centre = None
    if len(clear) >= 3:
        centre = (
            sorted(c.x for c in clear)[len(clear) // 2],
            sorted(c.y for c in clear)[len(clear) // 2],
        )
    kept = sorted(
        (c for c in candidates if pick_ball_candidate([c], centre) is not None),
        key=lambda c: c.time,
    )
    deliveries = []
    for candidate in kept:
        if deliveries and candidate.time - deliveries[-1].time < QUEUE_MERGE:
            if candidate.dots > deliveries[-1].dots:
                deliveries[-1] = candidate
            continue
        deliveries.append(candidate)
    return [round(c.time, 2) for c in deliveries]


def _queue_hits(deliveries, expected, clock_start):
    """How many balls have a delivery near their logged minute."""
    hits = 0
    for when in expected:
        target = when - clock_start
        index = bisect.bisect_left(deliveries, target - FIT_TOLERANCE)
        if index < len(deliveries) and deliveries[index] <= target + FIT_TOLERANCE:
            hits += 1
    return hits


def _minute_window(ball, clock_start):
    """The stretch of video a ball logged at this minute must fall in."""
    start = ball.minute_of_day * 60 - clock_start
    return start - QUEUE_WINDOW_SLACK, start + 60 + QUEUE_WINDOW_SLACK


def align_queue(balls, deliveries, clock_start, scores=None):
    """Pair balls with deliveries, in order, each inside its logged minute.
    Returns (cost, moments), with None for a ball no delivery fits.

    Inside the minute the exact spot doesn't matter much, since the times are
    cut to the minute; pairing as many balls as possible is what counts, and
    stronger sequences (`scores`, 0 to 1) are preferred over weak ones."""
    windows = [_minute_window(ball, clock_start) for ball in balls]
    weakness = [QUEUE_WEAK_COST * max(0.0, 1 - s) for s in scores] if scores else None
    n, m = len(balls), len(deliveries)
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    step = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        best[i][0], step[i][0] = i * QUEUE_SKIP_COST, "ball"
    for j in range(1, m + 1):
        best[0][j], step[0][j] = j * QUEUE_STRAY_COST, "delivery"
    for i in range(1, n + 1):
        low, high = windows[i - 1]
        centre = (low + high) / 2
        for j in range(1, m + 1):
            option = (best[i - 1][j] + QUEUE_SKIP_COST, "ball")
            other = best[i][j - 1] + QUEUE_STRAY_COST
            if other < option[0]:
                option = (other, "delivery")
            moment = deliveries[j - 1]
            if low <= moment <= high:
                paired = best[i - 1][j - 1] + 0.05 * abs(moment - centre)
                if weakness:
                    paired += weakness[j - 1]
                if paired < option[0]:
                    option = (paired, "pair")
            best[i][j], step[i][j] = option

    moments = [None] * n
    i, j = n, m
    while i > 0 or j > 0:
        action = step[i][j]
        if action == "pair":
            moments[i - 1] = deliveries[j - 1]
            i, j = i - 1, j - 1
        elif action == "ball":
            i -= 1
        else:
            j -= 1
    return best[n][m], moments


def _queue_parts(queue):
    """Split queue entries ([time, sure] or [time, sure, score]) into lists."""
    times = [entry[0] for entry in queue]
    sure = [bool(entry[1]) for entry in queue]
    scores = [entry[2] if len(entry) > 2 else 1.0 for entry in queue]
    return times, sure, scores


def fit_queue_to_scorecard(balls, queue, video_duration):
    """Return the scorer's clock reading at the video's first frame, worked out
    by sliding the scorecard along the queue until the two line up best. Only
    the gaps between the scorecard's times are used, never the stream start."""
    if not queue:
        raise ValueError("No deliveries were seen in the video.")
    deliveries, _, scores = _queue_parts(queue)
    expected = [ball.minute_of_day * 60 + 30 for ball in balls]
    low = expected[-1] - video_duration - CLOCK_SLACK
    high = expected[0] + CLOCK_SLACK
    low, high = min(low, high), max(low, high)

    # Coarse pass: count balls with a delivery nearby. The innings break and
    # the ends of the match make wrong offsets score low.
    scored = []
    clock = low
    while clock <= high:
        scored.append((_queue_hits(deliveries, expected, clock), clock))
        clock += FIT_STEP
    top = max(hits for hits, _ in scored)
    if top < len(balls) * MIN_QUEUE_MATCH:
        raise ValueError(
            "Too few deliveries in the video line up with the scorecard. Is it the right match?"
        )
    shortlist = [clock for hits, clock in scored if hits >= top * 0.95]

    # Fine pass: the full in-order pairing decides between close offsets.
    best = None
    for coarse in shortlist[:: max(1, len(shortlist) // 12)]:
        for shift in range(-FIT_STEP * 2, FIT_STEP * 2 + 1, 2):
            clock = coarse + shift
            cost = align_queue(balls, deliveries, clock, scores)[0]
            if best is None or cost < best[0]:
                best = (cost, clock)
    return best[1]


def queue_moments(balls, queue, clock_start):
    """Pair each ball with a delivery from the queue ([time, sure, score]), in
    order. Returns (moments, exact): a video time for each ball, and whether
    its ball was seen and its pairing is certain.

    A ball with no delivery is placed between its neighbours' deliveries, or
    at its logged minute if it has none, and is marked not exact."""
    times, sure, scores = _queue_parts(queue)
    sure_at = dict(zip(times, sure))
    _, paired = align_queue(balls, times, clock_start, scores)
    exact = [moment is not None and sure_at[moment] for moment in paired]
    # A pairing is only sure if no unpaired neighbour could have had that
    # delivery instead, which happens when a minute has more balls than seen.
    windows = [_minute_window(ball, clock_start) for ball in balls]
    for index, moment in enumerate(paired):
        if moment is None:
            continue
        for other in (index - 1, index + 1):
            if 0 <= other < len(balls) and paired[other] is None:
                low, high = windows[other]
                if low <= moment <= high:
                    exact[index] = False
    moments = list(paired)
    for index, moment in enumerate(moments):
        if moment is not None:
            continue
        before = next((i for i in range(index - 1, -1, -1) if paired[i] is not None), None)
        after = next((i for i in range(index + 1, len(paired)) if paired[i] is not None), None)
        logged = balls[index].minute_of_day * 60 + 30 - clock_start
        if before is not None and after is not None and paired[after] - paired[before] <= 300:
            share = (index - before) / (after - before)
            moments[index] = paired[before] + (paired[after] - paired[before]) * share
        else:
            moments[index] = logged
    return moments, exact


def locate_queue_clips(requests, balls, queue, clock_start):
    """Return a Highlight for each ClipRequest, timed from the ball queue."""
    moments, exact = queue_moments(balls, queue, clock_start)
    by_ball = {id(ball): (moment, seen) for ball, moment, seen in zip(balls, moments, exact)}
    highlights = []
    for request in requests:
        moment, seen = by_ball[id(request.ball)]
        window = WICKET_BALL_WINDOW if request.long_clip else BALL_WINDOW
        highlights.append(
            Highlight(
                request.player, request.watermark, request.ball.over_label,
                max(moment - window[0], 0), moment + window[1], seen, request.group,
            )
        )
    return highlights


def pick_motion_rises(motion_per_second, count=MOTION_RISES):
    """Return the seconds with the sharpest rises in movement, strongest first,
    compared with the three seconds before each. Play is still until the ball
    is bowled, then the bowler, batters and fielders move at once."""
    rises = []
    for second in range(3, len(motion_per_second)):
        recent = motion_per_second[second - 3:second]
        rises.append((motion_per_second[second] - sum(recent) / len(recent), second))
    return [second for _, second in sorted(rises, reverse=True)[:count]]


def find_motion_rises(video_path, start, length):
    """Return the video times of the strongest bursts of movement."""
    return [start + s + 0.5 for s in pick_motion_rises(measure_motion(video_path, start, length))]


def measure_motion(video_path, start, length):
    """Return how much of the field moved during each second of the video."""
    width, height, fps = 64, 24, 10
    cmd = [
        "ffmpeg", "-v", "error", "-ss", str(start), "-t", str(length), "-i", video_path,
        "-map", "0:v:0", "-vf",
        f"fps={fps},crop=iw:ih*0.45:0:ih*0.3,scale={width}:{height}:flags=area,format=gray",
        "-f", "rawvideo", "-",
    ]
    raw = subprocess.run(cmd, capture_output=True).stdout
    size = width * height
    frames = [raw[i:i + size] for i in range(0, len(raw) - size + 1, size)]
    moved = [
        sum(1 for a, b in zip(frames[i], frames[i - 1]) if abs(a - b) > 14)
        for i in range(1, len(frames))
    ]
    return [sum(moved[i:i + fps]) for i in range(0, len(moved), fps)]


def find_clock_start(stream_start_utc, first_minute, last_minute, video_duration):
    """Return the scorer's clock reading, in seconds since midnight, at the
    video's first frame.

    The page shows times in the match's local time and YouTube gives UTC. The
    PC's timezone can differ from the ground's, so the UTC offset is worked out
    by finding the whole-hour offset that puts every ball inside the video.
    """
    utc_seconds = stream_start_utc % 86400
    fits = []
    for hours in range(-12, 15):
        clock_start = (utc_seconds + hours * 3600) % 86400
        first = first_minute * 60 - clock_start
        last = last_minute * 60 - clock_start
        # Times are cut to the minute and the scorer's clock can drift, so a
        # ball logged as 12:48 can come seconds after a 12:48:08 stream start.
        if first >= -CLOCK_SLACK and last <= video_duration + CLOCK_SLACK:
            fits.append((first, clock_start))
    if not fits:
        raise ValueError("The ball times don't fit inside this video. Is it the right match?")
    return min(fits)[1]


def _board_cost(balls, changes, clock_start):
    paired = align_balls(balls, changes, clock_start)
    return sum(
        120 if moment is None else abs(moment - (ball.minute_of_day * 60 + 30 - clock_start))
        for ball, moment in zip(balls, paired)
    )


def fit_scoreboard_clock(balls, changes, video_duration):
    """Return the scorer's clock reading at the video's first frame, by sliding
    the scorecard along the scoreboard's changes until they line up best.

    This doesn't need the stream's start time, and copes with a scorer whose
    clock runs a few minutes off or who logged a few balls late."""
    if not changes:
        raise ValueError("The scoreboard never changed in this video.")
    expected = [ball.minute_of_day * 60 + 30 for ball in balls]
    low = expected[0] - video_duration
    high = expected[-1]
    scored = []
    clock = low
    while clock <= high:
        hits = _queue_hits(changes, expected, clock)
        scored.append((hits, clock))
        clock += BOARD_FIT_STEP
    top = max(hits for hits, _ in scored)
    if top < len(balls) * MIN_QUEUE_MATCH:
        raise ValueError("The scoreboard doesn't line up with the scorecard. Is it the right match?")
    shortlist = [clock for hits, clock in scored if hits >= top * 0.9]
    best = None
    for coarse in shortlist[:: max(1, len(shortlist) // 15)]:
        for shift in range(-BOARD_FIT_STEP, BOARD_FIT_STEP + 1, 2):
            cost = _board_cost(balls, changes, coarse + shift)
            if best is None or cost < best[0]:
                best = (cost, coarse + shift)
    return best[1]


def has_scoreboard(balls, changes):
    """Whether the video shows a scoreboard that changes with each ball."""
    return len(changes) >= len(balls) * 0.6


def align_balls(balls, changes, clock_start, max_gap=100, skip_ball_cost=120, skip_change_cost=4):
    """Pair each ball, in order, with one scoreboard change.

    The page's times are only to the minute, so each ball is paired with the
    nearby change that keeps the whole sequence in order. Returns a list the
    same length as `balls`, holding a video time or None if nothing fitted.
    """
    expected = [ball.minute_of_day * 60 + 30 - clock_start for ball in balls]
    n, m = len(balls), len(changes)
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    step = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        best[i][0] = i * skip_ball_cost
        step[i][0] = "ball"
    for j in range(1, m + 1):
        best[0][j] = j * skip_change_cost
        step[0][j] = "change"

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            options = [
                (best[i - 1][j] + skip_ball_cost, "ball"),
                (best[i][j - 1] + skip_change_cost, "change"),
            ]
            gap = abs(changes[j - 1] - expected[i - 1])
            if gap <= max_gap:
                options.append((best[i - 1][j - 1] + gap, "pair"))
            best[i][j], step[i][j] = min(options)

    moments = [None] * n
    i, j = n, m
    while i > 0 or j > 0:
        action = step[i][j]
        if action == "pair":
            moments[i - 1] = changes[j - 1]
            i, j = i - 1, j - 1
        elif action == "ball":
            i -= 1
        else:
            j -= 1
    return moments


@dataclass
class Highlight:
    player: str
    watermark: str
    over_label: str
    start: float
    end: float
    exact: bool  # True if the ball itself was found in the video
    group: str = ""


@dataclass
class ClipRequest:
    ball: Ball
    player: str
    watermark: str
    long_clip: bool  # True for the 6-second bowling length
    group: str = ""


def innings_in_match_order(batting, bowling):
    """Return every ball in the order it was bowled, across both innings."""
    present = [innings for innings in (batting, bowling) if innings and innings.balls]
    present.sort(key=lambda innings: innings.balls[0].minute_of_day)
    return [ball for innings in present for ball in innings.balls]


def highlight_requests(batting, bowling):
    """Ask for our 4s and 6s from `batting` and our wickets from `bowling`."""
    requests = []
    if batting:
        for ball in batting.balls:
            if ball.is_four or ball.is_six:
                requests.append(ClipRequest(ball, ball.batter, "6" if ball.is_six else "4", False, "Batting"))
    if bowling:
        for ball in bowling.balls:
            if ball.bowler_wicket:
                requests.append(ClipRequest(ball, ball.bowler, "W", True, "Bowling"))
    return requests


def every_ball_requests(batting, bowling):
    """Ask for every delivery of both innings, watermarked where it counts."""
    def watermark(ball):
        if ball.is_wicket:
            return "W"
        if ball.is_six:
            return "6"
        return "4" if ball.is_four else "None"

    requests = []
    if batting:
        requests += [ClipRequest(b, b.batter, watermark(b), False, "Batting") for b in batting.balls]
    if bowling:
        requests += [ClipRequest(b, b.bowler, watermark(b), True, "Bowling") for b in bowling.balls]
    return requests


def locate_clips(requests, balls, clock_start, changes, ball_finder=None, rise_finder=None,
                 on_progress=None, moments=None, unsure=()):
    """Return a Highlight for each ClipRequest. `balls` is every ball in match
    order, because pairing balls with scoreboard changes needs the whole list.
    `moments` (each ball's scoreboard change, from the overs box) replaces the
    pairing by time when given; balls whose index is in `unsure` are always
    marked for checking.

    `ball_finder(start, length)` returns BallCandidates, and a clear one on the
    match's usual ball path is marked exact. Otherwise the clip uses a faint path
    backed by `rise_finder(start, length)`, then the strongest burst of movement,
    then a typical gap. Those are marked not exact, for checking.
    """
    if moments is None:
        moments = align_balls(balls, changes, clock_start)
    moment_of = {id(ball): moment for ball, moment in zip(balls, moments)}
    unsure_ids = {id(balls[index]) for index in unsure}

    searched = []
    for index, request in enumerate(requests, start=1):
        moment = moment_of[id(request.ball)]
        previous_change = None
        search_start = search_length = None
        candidates = []
        if moment is not None:
            earlier = [change for change in changes if change < moment]
            previous_change = earlier[-1] if earlier else None
            search = WICKET_BALL_SEARCH if request.ball.is_wicket else BOUNDARY_BALL_SEARCH
            search_start = moment - search
            if previous_change is not None:
                search_start = max(search_start, previous_change)
            search_length = moment - BALL_SEARCH_END - search_start
            if ball_finder and search_length > 0:
                candidates = ball_finder(search_start, search_length)
        searched.append((request, moment, previous_change, search_start, search_length, candidates))
        if on_progress:
            on_progress(index, len(requests))

    centre = find_ball_path_centre([item[-1] for item in searched])
    highlights = []
    for request, moment, previous_change, search_start, search_length, candidates in searched:
        exact = False
        ball_window = WICKET_BALL_WINDOW if request.long_clip else BALL_WINDOW
        window = WICKET_NO_SIGNAL_WINDOW if request.long_clip else NO_SIGNAL_WINDOW
        if moment is None:
            moment = request.ball.minute_of_day * 60 + 30 - clock_start
        else:
            found = pick_ball_candidate(candidates, centre)
            if found is not None:
                moment, window, exact = found.time, ball_window, True
            elif rise_finder and search_length > 0:
                rises = rise_finder(search_start, search_length)
                weak = pick_weak_ball_candidate(candidates, rises, centre, previous_change)
                if weak is not None:
                    moment, window = weak.time, ball_window
                elif rises:
                    moment = rises[0]
                    window = WICKET_RISE_WINDOW if request.long_clip else RISE_WINDOW
        highlights.append(
            Highlight(
                request.player, request.watermark, request.ball.over_label,
                max(moment - window[0], 0), moment + window[1],
                exact and id(request.ball) not in unsure_ids, request.group,
            )
        )
    return highlights


def build_highlights(batting, bowling, clock_start, changes, ball_finder=None, rise_finder=None):
    """Return the clips for our 4s, 6s and wickets."""
    balls = innings_in_match_order(batting, bowling)
    return locate_clips(
        highlight_requests(batting, bowling), balls, clock_start, changes, ball_finder, rise_finder
    )


def build_reel_cmd(segments, output_path, transition=0.2, size=(1280, 720), fps=30):
    """Build the ffmpeg command that joins `segments` into one video.

    Each segment is a dict with 'path', 'duration' and 'has_audio'. Stat cards
    have no sound and clips may differ in size, so every segment is brought to
    one format first, because xfade rejects mismatched inputs.
    """
    width, height = size
    cmd = ["ffmpeg", "-y"]
    filters = []
    for index, segment in enumerate(segments):
        cmd += ["-i", segment["path"]]
        filters.append(
            f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p,"
            f"settb=AVTB[v{index}]"
        )
        if segment["has_audio"]:
            source = f"[{index}:a]aformat=sample_rates=44100:channel_layouts=stereo"
        else:
            source = (
                "anullsrc=r=44100:cl=stereo,"
                f"atrim=duration={segment['duration']:.3f}"
            )
        filters.append(f"{source},asetpts=PTS-STARTPTS[a{index}]")

    shortest = min(segment["duration"] for segment in segments)
    transition = min(transition, max(shortest / 4, 0.05))
    video_label, audio_label = "v0", "a0"
    timeline = segments[0]["duration"]
    for index in range(1, len(segments)):
        offset = max(timeline - transition, 0)
        filters.append(
            f"[{video_label}][v{index}]xfade=transition=fade:duration={transition:.3f}:"
            f"offset={offset:.3f}[xv{index}]"
        )
        filters.append(f"[{audio_label}][a{index}]acrossfade=d={transition:.3f}[xa{index}]")
        video_label, audio_label = f"xv{index}", f"xa{index}"
        timeline += segments[index]["duration"] - transition

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", f"[{video_label}]", "-map", f"[{audio_label}]",
        # xfade can output 4:4:4, which Windows' players and many sites reject.
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
        output_path,
    ]
    return cmd


def ball_by_ball_url(link):
    """Return a scorecard link pointing at its Ball by Ball tab."""
    parts = urlsplit(link.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("That doesn't look like a scorecard link.")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "tab"]
    query.append(("tab", "ball_by_ball"))
    return urlunsplit(parts._replace(query=urlencode(query)))


def parse_rosters(text):
    """Return {team: set of player names} from a page's 'Team Players:' lines."""
    rosters = {}
    for line in text.splitlines():
        match = re.match(r"^(.+?) Players: (.+)$", line.strip())
        if match:
            names = {name.strip().rstrip("*") for name in match.group(2).split(",")}
            rosters[match.group(1)] = names
    return rosters


def parse_match_date(text):
    """Return the match date from copied scorecard text, or None."""
    for found in re.findall(r"\b[A-Z][a-z]{2,8} \d{1,2}, \d{4}\b", text):
        for pattern in ("%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(found, pattern).date()
            except ValueError:
                pass
    return None


def parse_match_teams(text, link=""):
    """Return the two teams in a scorecard, from the copied page (the two
    names just above 'League'), its squads, or the link's 'asfd=A+vs+B' part.
    Returns [] if none of those say."""
    lines = [line.strip() for line in text.splitlines()]
    if "League" in lines:
        above = [line for line in lines[:lines.index("League")] if line][-2:]
        if len(above) == 2 and not any("/" in line for line in above):
            return above
    rosters = parse_rosters(text)
    if len(rosters) == 2:
        return list(rosters)
    asfd = dict(parse_qsl(urlsplit(link).query)).get("asfd", "")
    if " vs " in asfd:
        return [team.strip() for team in asfd.split(" vs ", 1)]
    return []


def _name_words(text):
    return " " + " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split()) + " "


def video_name_mismatch(video_path, teams, date):
    """Say how a video's file name disagrees with the scorecard's match:
    a list holding "teams" and/or "date", or [] if it agrees or the name
    doesn't say. YTDown.com and yt-dlp names carry the stream title, such as
    '...Cheetahs-Alpha-vs-Cougars-Black-11-01-2026...'."""
    name = os.path.splitext(os.path.basename(video_path))[0]
    words = _name_words(name)
    problems = []
    if teams and " vs " in words:
        if any(_name_words(team) not in words for team in teams):
            problems.append("teams")
    if date:
        found = set()
        for month, day, year in re.findall(r"(?<!\d)(\d{1,2})[-_./](\d{1,2})[-_./](\d{4})(?!\d)", name):
            try:
                found.add(datetime(int(year), int(month), int(day)).date())
            except ValueError:
                pass
        if found and date not in found:
            problems.append("date")
    return problems


def video_search_text(teams, date):
    """Return the text to search for a match's video."""
    return f"{' vs '.join(teams)} {date.month:02d}/{date.day:02d}/{date.year}"


def _title_date_forms(date):
    """Return the ways a video title might write this date."""
    forms = set()
    for month in (f"{date.month:02d}", str(date.month)):
        for day in (f"{date.day:02d}", str(date.day)):
            for year in (str(date.year), f"{date.year % 100:02d}"):
                forms.add(f"{month}/{day}/{year}")
                forms.add(f"{month}-{day}-{year}")
    for month in (date.strftime("%b"), date.strftime("%B")):
        forms.add(f"{month} {date.day}, {date.year}")
        forms.add(f"{month} {date.day} {date.year}")
    return {form.lower() for form in forms}


def is_match_video(title, teams, date):
    """True only if a title has both teams and this exact date, because the same
    teams can play each other more than once."""
    title = title.lower()
    if not all(team.lower() in title for team in teams):
        return False
    return any(
        re.search(rf"(?<![\d/-]){re.escape(form)}(?![\d/-])", title)
        for form in _title_date_forms(date)
    )


def pick_match_video(results, teams, date):
    """Return the longest result matching this match, as the full recording
    rather than a short highlight, or None. `results` holds yt-dlp entries."""
    matches = [r for r in results if r and is_match_video(r.get("title") or "", teams, date)]
    return max(matches, key=lambda r: r.get("duration") or 0, default=None)


def _batting_team(batters, rosters):
    """Return the roster team holding the most of these batters, or None."""
    best = max(rosters, key=lambda team: len(rosters[team] & batters), default=None)
    return best if best and rosters[best] & batters else None


def assign_innings(first_text, second_text, our_team):
    """Work out which copied innings we batted in. Returns (batting_text,
    bowling_text, problem). `problem` is None, or says why the order is a guess
    and the two boxes may need swapping.

    The page only lists both squads on the first innings, so the batters in
    that innings identify its team, and the other innings is the other team.
    """
    rosters = parse_rosters(first_text) or parse_rosters(second_text)
    if not rosters:
        return first_text, second_text, "The page didn't list the squads, so check the two boxes."

    for text, other in ((first_text, second_text), (second_text, first_text)):
        if not parse_rosters(text):
            continue
        batters = set(batting_stats(parse_ball_by_ball(text)))
        team = _batting_team(batters, rosters)
        if team is None:
            break
        if our_team.lower() in team.lower():
            return text, other, None
        if any(our_team.lower() in name.lower() for name in rosters):
            return other, text, None
        return first_text, second_text, f"No team matches '{our_team}', so check the two boxes."
    return first_text, second_text, "Couldn't tell the teams apart, so check the two boxes."


def batting_stats(innings):
    """Return {full name: {'runs', 'balls', 'fours', 'sixes'}} in batting order."""
    stats = OrderedDict()
    for ball in innings.balls:
        entry = stats.setdefault(ball.batter, {"runs": 0, "balls": 0, "fours": 0, "sixes": 0})
        entry["runs"] += ball.batter_runs
        entry["balls"] += 1 if ball.faced else 0
        entry["fours"] += 1 if ball.is_four else 0
        entry["sixes"] += 1 if ball.is_six else 0
    return stats


def bowling_stats(innings):
    """Return {full name: {'overs', 'maidens', 'runs', 'wickets'}}. 'overs' is
    a string, because 1.5 means one over and five balls."""
    legal = OrderedDict()
    runs = {}
    wickets = {}
    by_over = {}
    for ball in innings.balls:
        legal[ball.bowler] = legal.get(ball.bowler, 0) + (1 if ball.legal else 0)
        runs[ball.bowler] = runs.get(ball.bowler, 0) + ball.bowler_runs
        wickets[ball.bowler] = wickets.get(ball.bowler, 0) + (1 if ball.bowler_wicket else 0)
        over = by_over.setdefault((ball.bowler, ball.over_index), [0, 0])
        over[0] += 1 if ball.legal else 0
        over[1] += ball.bowler_runs

    stats = OrderedDict()
    for bowler, balls in legal.items():
        overs, extra_balls = divmod(balls, 6)
        maidens = sum(
            1 for (name, _), (count, conceded) in by_over.items()
            if name == bowler and count == 6 and conceded == 0
        )
        stats[bowler] = {
            "overs": str(overs) if not extra_balls else f"{overs}.{extra_balls}",
            "maidens": maidens,
            "runs": runs[bowler],
            "wickets": wickets[bowler],
        }
    return stats
